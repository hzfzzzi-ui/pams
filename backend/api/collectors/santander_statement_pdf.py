"""Santander Mexico monthly PDF: extract and reconcile only the cheques section."""
from __future__ import annotations

import csv
import os
import re
import shutil
import subprocess
import time
import unicodedata
from datetime import date
from decimal import Decimal
from io import BytesIO, StringIO

import pdfplumber
from PIL import ImageOps

from collectors.bank_statement_parser import ParsedStatement, _row_uid, _SPANISH_MONTHS


_DATE = r"\d{2}\s*[-./]\s*[A-Z]{3}\s*[-./]\s*\d{4}"
_PERIOD = re.compile(rf"PERIODO DEL\s+({_DATE})\s+AL\s+({_DATE})")
_MONEY = re.compile(r"(?<![\w.,])[-−]?\$?\s*(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}(?![\w.,%])")
_ACCOUNT = re.compile(r"SUPERCUENTA\s+CHEQUES\s*[-–]\s*SALDO\s+PROM\s+(\d{2})\s*-\s*(\d{8})\s*-\s*(\d)")
_HEADER = re.compile(r"^FECHA\b.*\bFOLIO\b.*\bDESCRIPCION\b.*\bDEPOSITO\b.*\bRETIRO\b.*\bSALDO\b")
_ROW = re.compile(rf"^\W*({_DATE})[\s|()[\]]+(?:([0-9]{{5,12}})\b[\s|()[\]]*)?(.*)$")


def _normalized(line):
    return ''.join(c for c in unicodedata.normalize('NFKD', line.upper())
                   if not unicodedata.combining(c))


def _day(value):
    day, month, year = re.split(r"\s*[-./]\s*", value)
    return date(int(year), _SPANISH_MONTHS[month.lower()], int(day))


def _amounts(text):
    return [(m.start(), Decimal(m.group().strip().replace('$', '').replace(',', '').replace('−', '-').replace(' ', '')))
            for m in _MONEY.finditer(text)]


def _tesseract(image, deadline, *options):
    executable = shutil.which("tesseract")
    if not executable:
        raise ValueError("缺少本地 OCR，暂不能读取 Santander 图像 PDF")
    timeout = min(deadline - time.monotonic(), 12)
    if timeout <= 0:
        raise ValueError("Santander PDF 识别超时，请改用银行 CSV")
    content = BytesIO()
    image.save(content, format="PNG")
    try:
        result = subprocess.run(
            [executable, "stdin", "stdout", "-l", "eng", *options],
            input=content.getvalue(), capture_output=True, timeout=timeout, check=True,
            env={**os.environ, "OMP_THREAD_LIMIT": "1"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("Santander PDF 本地 OCR 未完成；可改用银行导出的 CSV") from exc
    if len(result.stdout) > 250_000:
        raise ValueError("Santander PDF 识别文本超过限制")
    return result.stdout.decode("utf-8")


def _ocr_page(page, deadline, resolution=280):
    if not 0 < page.width <= 1000 or not 0 < page.height <= 1000:
        raise ValueError("Santander PDF 页面尺寸不受支持")
    image = page.to_image(resolution=resolution).original
    tsv = _tesseract(image, deadline, "--psm", "6", "tsv")
    grouped = {}
    for word in csv.DictReader(StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE):
        if word["level"] == "5" and word["text"].strip():
            key = tuple(word[k] for k in ("block_num", "par_num", "line_num"))
            grouped.setdefault(key, []).append(word)
    lines = []
    retries = 0
    for words in grouped.values():
        tokens = [word["text"] for word in words]
        line = " ".join(tokens)
        # Page-wide OCR can omit the decimal point where a rule crosses the balance.
        # Re-read only that cell; the normal running balance and total checks still apply.
        if _ROW.match(_normalized(line)) and not _MONEY.fullmatch(tokens[-1]):
            x, y, width, height = (int(words[-1][key]) for key in ("left", "top", "width", "height"))
            if x >= image.width * 0.85 and retries < 8:
                retries += 1
                right = x + width
                # A box that reaches the paper edge can contain the border rule.
                if right >= image.width * 0.98 and width > 60:
                    right -= 30
                else:
                    right += 8
                bounds = (max(0, x - 15), max(0, y - 5), min(image.width, right), min(image.height, y + height + 5))
                cell = ImageOps.expand(ImageOps.grayscale(image.crop(bounds)), border=12, fill=255)
                cell = cell.resize((2 * cell.width, 2 * cell.height))
                value = _tesseract(cell, deadline, "--psm", "7", "-c",
                                   "tessedit_char_whitelist=0123456789,.-$").strip()
                if _MONEY.fullmatch(value):
                    line = " ".join([*tokens[:-1], value])
        lines.append(line)
    return "\n".join(lines)


def _parse_pages(pages, path, profile, since=None):
    expected_account = re.sub(r"[-\s]", "", str(profile.get("pdf_account_number") or ""))
    tail = str(profile.get("account_tail") or "")
    if (expected_account and not re.fullmatch(r"\d{11}", expected_account)) or (
            not expected_account and not re.fullmatch(r"\d{4}", tail)):
        raise ValueError("请先配置 Santander 支票账户号或尾号")
    period = None
    opening = None
    prior_balance = None
    rows = []
    found_account = None
    reported_balances = None
    in_table = False
    totals = None
    credits = debits = Decimal(0)
    for page_number, text, ocr in pages:
        for line_number, raw in enumerate(text.splitlines(), 1):
            line = _normalized(raw).strip()
            if period_match := _PERIOD.search(line):
                reported_period = tuple(_day(value) for value in period_match.groups())
                if period and period != reported_period:
                    raise ValueError("Santander 月结单页间账期不一致")
                period = reported_period
            if account_match := _ACCOUNT.search(line):
                account = ''.join(account_match.groups())
                if (expected_account and account != expected_account) or (not expected_account and not account.endswith(tail)) or (
                        found_account and account != found_account):
                    raise ValueError("Santander 月结单账户号与所选账户不符")
                found_account = account
                # First-page balance summary has previous/current amounts separated
                # by percentages, unlike the rate and interest summary above it.
                summary = line[account_match.end():]
                if page_number == 1 and summary.count("%") == 2:
                    values = [value for _, value in _amounts(summary)]
                    if len(values) == 2:
                        reported_balances = tuple(values)
            if not found_account:
                continue
            if line.startswith("SALDO FINAL DEL PERIODO ANTERIOR") and opening is None:
                amounts = _amounts(line)
                if len(amounts) != 1:
                    raise ValueError("无法确认 Santander 支票账户期初余额")
                opening = prior_balance = amounts[0][1]
                continue
            if opening is None:
                continue
            if _HEADER.match(line):
                in_table = True
                continue
            if not in_table:
                continue
            if re.match(r"^\W*TOTAL\b", line):
                totals = [amount for _, amount in _amounts(line)]
                break
            if "MOVIMIENTOS DINERO CRECIENTE" in line:
                raise ValueError("Santander 支票账户流水缺少结尾小计")
            if not re.match(r"^\W*\d{2}\s*[-./]\s*[A-Z0-9]{3}\s*[-./]", line):
                continue
            row = _ROW.match(line)
            if not row:
                raise ValueError("Santander PDF 有无法识别的日期或流水编号")
            date_text, folio, details = row.groups()
            amounts = _amounts(details)
            if len(amounts) < 2:
                raise ValueError("Santander PDF 有无法识别的发生额或余额")
            (amount_start, reported_amount), (_, balance) = amounts[-2:]
            amount = balance - prior_balance
            day = _day(date_text)
            if reported_amount == 0 and amount == 0:
                continue
            if reported_amount < 0 or abs(amount) != reported_amount:
                raise ValueError("Santander PDF 发生额与逐笔余额不一致，未生成流水草稿")
            prior_balance = balance
            if amount > 0:
                credits += amount
            else:
                debits -= amount
            # Keep source order, page and folio. The folio alone is not a global identifier.
            # Keep original casing and characters when normalization kept offsets.
            offset = row.start(3)
            summary = (raw.strip()[offset:offset + amount_start] if len(raw.strip()) == len(line)
                       else details[:amount_start]).strip(" |")
            rows.append({
                "source_uid": _row_uid(profile["account_code"], day, folio, amount, balance, page_number, line_number),
                "transaction_date": day, "posting_date": None, "currency": "MXN",
                "amount": amount, "balance": balance, "original_currency": "MXN", "original_amount": amount,
                "summary": summary, "description": summary, "counterparty": None,
                "raw": {"page": page_number, "line_idx": line_number, "line": raw, "folio": folio,
                        "source_file": path.name, "statement_template": "santander_mx_account_pdf",
                        "parse_method": "ocr" if ocr else "template"},
            })
        if totals is not None:
            break
    if not period or not in_table or totals != [credits, debits] or period[0] > period[1]:
        raise ValueError("Santander 支票账户账期、表头或存取小计未能核对，未生成流水草稿")
    if reported_balances is not None and reported_balances != (opening, prior_balance):
        raise ValueError("Santander PDF 流水与账单期初或期末余额不一致，未生成流水草稿")
    if any(not period[0] <= row["transaction_date"] <= period[1] for row in rows):
        raise ValueError("Santander 交易日期不在账期内")
    # The printed totals and row balances are independent checks; OCR still requires review.
    return ParsedStatement(
        profile_code=profile["profile_code"], source_file=path.name, bank=profile.get("bank", "Santander Mexico"),
        account_code=profile["account_code"], account_kind="debit", account_tail=profile.get("account_tail"),
        statement_start=period[0], statement_end=period[1], statement_balance=prior_balance,
        rows=[row for row in rows if not since or row["transaction_date"] >= since],
    )


def parse_santander_pdf(path, profile, since=None):
    deadline = time.monotonic() + 45
    try:
        with pdfplumber.open(path, password=profile.get("pdf_password") or None) as pdf:
            if not 1 <= len(pdf.pages) <= 12:
                raise ValueError("Santander PDF 请一次上传一期月结单（最多 12 页）")

            def pages(resolution):
                for index, page in enumerate(pdf.pages, 1):
                    text = page.extract_text(layout=True) or ""
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ValueError("Santander PDF 识别超时，请改用银行 CSV")
                    if text.strip():
                        yield index, text, False
                    else:
                        yield index, _ocr_page(page, deadline, resolution), True

            last_error = None
            # Each rendering must independently pass the account and totals checks.
            for resolution in (280, 300, 250, 320):
                try:
                    return _parse_pages(pages(resolution), path, profile, since)
                except ValueError as exc:
                    if '账户号' in str(exc) or 'OCR' in str(exc) or time.monotonic() >= deadline:
                        raise
                    last_error = exc
            raise last_error
    except (KeyError, OverflowError) as exc:
        raise ValueError("Santander PDF 的日期无法核对") from exc
