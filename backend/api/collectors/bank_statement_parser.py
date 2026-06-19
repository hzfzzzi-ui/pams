"""Bank statement PDF parsers used by the reconciliation staging API."""

from __future__ import annotations

import hashlib
import csv
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pdfplumber
import pandas as pd


_CURRENCY_ALIASES = {
    "人民币": "CNY",
    "CNY": "CNY",
    "RMB": "CNY",
    "美元": "USD",
    "USD": "USD",
    "日元": "JPY",
    "墨西哥比索": "MXN",
    "其他货币": "OTHER",
    "MXN": "MXN",
    "JPY": "JPY",
    "HKD": "HKD",
}

_SPANISH_MONTHS = {
    "ene": 1,
    "feb": 2,
    "mar": 3,
    "abr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "ago": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dic": 12,
}


@dataclass(frozen=True)
class ParsedStatement:
    profile_code: str
    source_file: str
    bank: str
    account_code: str
    account_kind: str
    account_tail: str | None
    statement_start: date | None
    statement_end: date | None
    statement_balance: Decimal | None
    rows: list[dict[str, Any]]


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _parse_date_yyyymmdd(value: Any) -> date | None:
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{8}", text):
        return None
    return datetime.strptime(text, "%Y%m%d").date()


def _parse_date_yyyy_slash(value: Any) -> date | None:
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{4}/\d{2}/\d{2}", text):
        return None
    return datetime.strptime(text, "%Y/%m/%d").date()


def _parse_flexible_date(value: Any) -> date | None:
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _parse_flexible_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    parsed_date = _parse_flexible_date(text)
    return datetime.combine(parsed_date, datetime.min.time()) if parsed_date else None


def _parse_santander_date(value: Any) -> date | None:
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d{2})/([A-Za-zÁÉÍÓÚÜÑáéíóúüñ]{3})/(\d{4})", text)
    if not match:
        return None
    month_key = match.group(2).lower()
    month = _SPANISH_MONTHS.get(month_key)
    if month is None:
        return None
    return date(int(match.group(3)), month, int(match.group(1)))


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\n", " ")).strip()


def _row_uid(*parts: Any) -> str:
    raw = "|".join(str(p or "") for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _parse_currency_amount(value: Any) -> tuple[str | None, Decimal | None]:
    text = _clean_text(value)
    match = re.match(r"([^/]+)/([+-]?[0-9][0-9,]*(?:\.[0-9]+)?)", text)
    if not match:
        return None, None
    ccy = _CURRENCY_ALIASES.get(match.group(1).strip(), match.group(1).strip())
    return ccy, _to_decimal(match.group(2))


def _parse_santander_money(value: Any) -> Decimal | None:
    text = _clean_text(value)
    if not text or text == "-":
        return None
    text = text.replace("$", "").replace(",", "").strip()
    return _to_decimal(text)


def _decode_statement_bytes(data: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8-sig", errors="replace")


def _extract_last_decimal(value: Any) -> Decimal | None:
    text = _clean_text(value).replace(",", "")
    matches = re.findall(r"[+-]?\d+(?:\.\d+)?", text)
    if not matches:
        return None
    return _to_decimal(matches[-1])


def _clean_icbc_label(value: Any) -> str:
    text = _clean_text(value)
    if "（空）" in text:
        return ""
    if re.search(r"[\u4e00-\u9fff]", text):
        text = re.sub(r"[A-Za-z0-9:：\\-]+", "", text)
        return _clean_text(text)
    return text


def _parse_icbc_datetime(value: Any) -> tuple[date | None, str | None]:
    text = _clean_text(value)
    date_match = re.search(r"(\d{4}-\d{2}-\d{2})", text)
    time_match = re.search(r"(\d{2}:\d{2}:\d{2})", text)
    if not date_match:
        return None, time_match.group(1) if time_match else None
    return datetime.strptime(date_match.group(1), "%Y-%m-%d").date(), time_match.group(1) if time_match else None


def _parse_icbc_currency(value: Any) -> str | None:
    text = _clean_text(value)
    if "人民币" in text:
        return "CNY"
    if "美元" in text or ("美" in text and "元" in text):
        return "USD"
    return _CURRENCY_ALIASES.get(text)


def _digits_only(value: Any) -> str:
    return "".join(re.findall(r"\d+", str(value or "")))


def _parse_icbc_debit_row(row: list[str], *, page_idx: int, row_idx: int, path: Path, profile: dict[str, Any]) -> dict[str, Any] | None:
    """Parse one ICBC debit-account history detail row extracted from a PDF table."""
    if len(row) < 13:
        return None
    tx_date, tx_time = _parse_icbc_datetime(row[0])
    if tx_date is None:
        return None
    currency = _parse_icbc_currency(row[4])
    if currency is None:
        return None
    currency_filter = str(profile.get("currency_filter") or profile.get("currency") or "").upper()
    if currency_filter and currency != currency_filter:
        return None
    amount = _extract_last_decimal(row[8])
    balance = _extract_last_decimal(row[9])
    if amount is None:
        return None

    account_no = _digits_only(row[1])
    summary = _clean_icbc_label(row[6])
    counterparty = _clean_icbc_label(row[10])
    channel = _clean_icbc_label(row[12])
    counterparty_account = _digits_only(row[11])
    description = " ".join(x for x in [summary, counterparty, counterparty_account, channel] if x)
    uid = _row_uid(
        profile["account_code"],
        currency,
        path.name,
        page_idx,
        row_idx,
        tx_date,
        tx_time,
        account_no,
        amount,
        balance,
        summary,
        counterparty,
        counterparty_account,
        channel,
    )
    return {
        "source_uid": uid,
        "transaction_date": tx_date,
        "posting_date": tx_date,
        "amount": amount,
        "currency": currency,
        "balance": balance,
        "original_amount": amount,
        "original_currency": currency,
        "summary": summary,
        "description": description,
        "counterparty": counterparty,
        "raw": {
            "row": row,
            "page_idx": page_idx,
            "row_idx": row_idx,
            "transaction_time": tx_time,
            "account_no": account_no,
            "counterparty_account": counterparty_account,
            "source_file": str(path),
            "statement_template": "icbc_debit_pdf",
        },
    }


def _parse_santander_account_row(row: list[str], *, row_idx: int, path: Path, profile: dict[str, Any]) -> dict[str, Any] | None:
    """Parse one Santander Mexico account CSV transaction row."""
    if len(row) < 8:
        return None
    tx_date = _parse_santander_date(row[0])
    if tx_date is None:
        return None
    withdrawal = _parse_santander_money(row[4])
    deposit = _parse_santander_money(row[5])
    balance = _parse_santander_money(row[6])
    if withdrawal is None and deposit is None:
        return None
    amount = deposit if deposit is not None else -withdrawal.copy_abs()
    summary = _clean_text(row[3])
    reference = _clean_text(row[7])
    uid = _row_uid(
        profile["account_code"],
        path.name,
        row_idx,
        row[0],
        row[1],
        row[2],
        summary,
        amount,
        balance,
        reference,
    )
    return {
        "source_uid": uid,
        "transaction_date": tx_date,
        "posting_date": tx_date,
        "amount": amount,
        "currency": profile.get("currency", "MXN"),
        "balance": balance,
        "original_amount": amount,
        "original_currency": profile.get("currency", "MXN"),
        "summary": summary,
        "description": summary,
        "counterparty": summary,
        "raw": {
            "row": row,
            "row_idx": row_idx,
            "transaction_time": row[1],
            "branch": row[2],
            "reference": reference,
            "source_file": str(path),
            "statement_template": "santander_mx_account_csv",
        },
    }


_GUANGFA_CREDIT_ROW_RE = re.compile(
    r"^(?P<transaction_date>\d{4}/\d{2}/\d{2})\s+"
    r"(?P<posting_date>\d{4}/\d{2}/\d{2})\s+"
    r"(?P<description>.*?)"
    r"(?P<original_amount>[+-]?[0-9][0-9,]*(?:\.[0-9]+)?)\s*"
    r"(?P<original_currency>人民币|美元|日元|墨西哥比索|其他货币)\s+"
    r"(?P<settlement_amount>[+-]?[0-9][0-9,]*(?:\.[0-9]+)?)\s*"
    r"(?P<settlement_currency>人民币|美元)\s*$"
)


def _statement_balance_amount(value: Any) -> Decimal | None:
    text = _clean_text(value)
    if text in {"无欠款", "无", "-"}:
        return Decimal("0")
    return _to_decimal(text)


def _parse_guangfa_credit_transaction_line(line: str) -> dict[str, Any] | None:
    """Parse one Guangfa credit-card transaction line extracted from PDF text."""
    match = _GUANGFA_CREDIT_ROW_RE.match(_clean_text(line))
    if not match:
        return None

    tx_date = _parse_date_yyyy_slash(match.group("transaction_date"))
    posting_date = _parse_date_yyyy_slash(match.group("posting_date"))
    original_amount = _to_decimal(match.group("original_amount"))
    settlement_amount = _to_decimal(match.group("settlement_amount"))
    if tx_date is None or posting_date is None or original_amount is None or settlement_amount is None:
        return None

    description = _clean_text(match.group("description"))
    tag_match = re.match(r"^\(([^)]+)\)\s*(.*)$", description)
    if tag_match:
        summary = f"信用卡{tag_match.group(1)}"
        counterparty = _clean_text(tag_match.group(2))
    else:
        summary = "信用卡交易"
        counterparty = description

    return {
        "transaction_date": tx_date,
        "posting_date": posting_date,
        "summary": summary,
        "description": description or summary,
        "counterparty": counterparty,
        "original_amount": original_amount,
        "original_currency": _CURRENCY_ALIASES.get(match.group("original_currency"), match.group("original_currency")),
        "original_currency_text": match.group("original_currency"),
        "settlement_amount": settlement_amount,
        "settlement_currency": _CURRENCY_ALIASES.get(match.group("settlement_currency"), match.group("settlement_currency")),
        "settlement_currency_text": match.group("settlement_currency"),
    }


def parse_ccb_debit_pdf(path: Path, profile: dict[str, Any], since: date | None = None) -> ParsedStatement:
    """Parse a CCB debit-card current-account transaction statement PDF."""
    rows: list[dict[str, Any]] = []
    account_tail: str | None = None
    statement_start: date | None = None
    statement_end: date | None = None

    with pdfplumber.open(path) as pdf:
        first_text = pdf.pages[0].extract_text() or ""
        acc_match = re.search(r"卡号/账号[:：]\s*(\d+)", first_text)
        if acc_match:
            account_tail = acc_match.group(1)[-4:]
        range_match = re.search(r"起止日期[:：]\s*(\d{8})-(\d{8})", first_text)
        if range_match:
            statement_start = _parse_date_yyyymmdd(range_match.group(1))
            statement_end = _parse_date_yyyymmdd(range_match.group(2))

        for page_idx, page in enumerate(pdf.pages):
            for table_idx, table in enumerate(page.extract_tables() or []):
                if not table or not table[0] or table[0][0] != "序号":
                    continue
                for row_idx, row in enumerate(table[1:], start=1):
                    tx_date = _parse_date_yyyymmdd(row[2] if len(row) > 2 else None)
                    if tx_date is None or (since and tx_date < since):
                        continue
                    amount = _to_decimal(row[3] if len(row) > 3 else None)
                    balance = _to_decimal(row[4] if len(row) > 4 else None)
                    if amount is None:
                        continue
                    summary = _clean_text(row[1] if len(row) > 1 else "")
                    memo = _clean_text(row[5] if len(row) > 5 else "")
                    counterparty = _clean_text(row[6] if len(row) > 6 else "")
                    description = " ".join(x for x in [summary, memo, counterparty] if x)
                    uid = _row_uid(
                        profile["account_code"],
                        path.name,
                        page_idx,
                        table_idx,
                        row_idx,
                        row[0],
                        tx_date,
                        amount,
                        balance,
                        description,
                    )
                    rows.append(
                        {
                            "source_uid": uid,
                            "transaction_date": tx_date,
                            "posting_date": tx_date,
                            "amount": amount,
                            "currency": profile.get("currency", "CNY"),
                            "balance": balance,
                            "original_amount": amount,
                            "original_currency": profile.get("currency", "CNY"),
                            "summary": summary,
                            "description": description,
                            "counterparty": counterparty,
                            "raw": {
                                "row": row,
                                "page_idx": page_idx,
                                "table_idx": table_idx,
                                "row_idx": row_idx,
                                "source_file": str(path),
                                "statement_template": "ccb_debit_pdf",
                            },
                        }
                    )

    statement_balance = rows[-1]["balance"] if rows else None
    return ParsedStatement(
        profile_code=profile["profile_code"],
        source_file=str(path),
        bank=profile.get("bank", "CCB"),
        account_code=profile["account_code"],
        account_kind=profile.get("account_kind", "debit"),
        account_tail=account_tail,
        statement_start=statement_start,
        statement_end=statement_end,
        statement_balance=statement_balance,
        rows=rows,
    )


def parse_ccb_credit_pdf(path: Path, profile: dict[str, Any], since: date | None = None) -> ParsedStatement:
    """Parse a CCB credit-card monthly statement PDF."""
    rows: list[dict[str, Any]] = []
    account_tail: str | None = None
    statement_start: date | None = None
    statement_end: date | None = None
    statement_balance: Decimal | None = None

    with pdfplumber.open(path) as pdf:
        first_text = pdf.pages[0].extract_text() or ""
        date_match = re.search(r"本期账单日\(Current Statement Date\)\s*(\d{8})", first_text)
        if date_match:
            statement_end = _parse_date_yyyymmdd(date_match.group(1))
        cycle_match = re.search(r"账单周期\(Account Cycle\)\s*(\d{8})-(\d{8})", first_text)
        if cycle_match:
            statement_start = _parse_date_yyyymmdd(cycle_match.group(1))
            statement_end = _parse_date_yyyymmdd(cycle_match.group(2)) or statement_end

        for page_idx, page in enumerate(pdf.pages):
            for table_idx, table in enumerate(page.extract_tables() or []):
                if not table or not table[0]:
                    continue
                header0 = table[0][0]
                if header0 == "信用卡卡号(Card Number)" and len(table) > 1:
                    account_tail = _clean_text(table[1][0])
                    statement_balance = _to_decimal(table[1][2])
                    continue
                if header0 != "交易日":
                    continue
                for row_idx, row in enumerate(table[1:], start=1):
                    tx_date = _parse_date_yyyymmdd(row[0] if len(row) > 0 else None)
                    if tx_date is None or (since and tx_date < since):
                        continue
                    posting_date = _parse_date_yyyymmdd(row[1] if len(row) > 1 else None) or tx_date
                    card_tail = _clean_text(row[2] if len(row) > 2 else "")
                    if card_tail:
                        account_tail = card_tail
                    original_currency, original_amount = _parse_currency_amount(row[3] if len(row) > 3 else None)
                    settlement_currency, settlement_amount = _parse_currency_amount(row[4] if len(row) > 4 else None)
                    if settlement_amount is None:
                        continue
                    description = _clean_text(row[5] if len(row) > 5 else "")
                    # Credit-card charges increase liability. Store cashflow view:
                    # normal charges are negative; negative statement rows become positive adjustments.
                    amount = -settlement_amount
                    uid = _row_uid(
                        profile["account_code"],
                        path.name,
                        page_idx,
                        table_idx,
                        row_idx,
                        tx_date,
                        posting_date,
                        card_tail,
                        original_currency,
                        original_amount,
                        settlement_currency,
                        settlement_amount,
                        description,
                    )
                    rows.append(
                        {
                            "source_uid": uid,
                            "transaction_date": tx_date,
                            "posting_date": posting_date,
                            "amount": amount,
                            "currency": settlement_currency or profile.get("currency", "CNY"),
                            "balance": None,
                            "original_amount": original_amount,
                            "original_currency": original_currency,
                            "summary": "信用卡交易",
                            "description": description,
                            "counterparty": "",
                            "raw": {
                                "row": row,
                                "page_idx": page_idx,
                                "table_idx": table_idx,
                                "row_idx": row_idx,
                                "source_file": str(path),
                                "statement_template": "ccb_credit_pdf",
                                "statement_amount": str(settlement_amount),
                            },
                        }
                    )

    return ParsedStatement(
        profile_code=profile["profile_code"],
        source_file=str(path),
        bank=profile.get("bank", "CCB"),
        account_code=profile["account_code"],
        account_kind=profile.get("account_kind", "credit_card"),
        account_tail=account_tail,
        statement_start=statement_start,
        statement_end=statement_end,
        statement_balance=statement_balance,
        rows=rows,
    )


def _parse_wechat_header_date(text: str) -> datetime | None:
    match = re.search(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]", str(text or ""))
    if not match:
        return None
    return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")


def parse_wechat_pay_xlsx(path: Path, profile: dict[str, Any], since: date | None = None) -> ParsedStatement:
    """Parse a WeChat Pay exported transaction workbook."""
    meta = pd.read_excel(path, sheet_name=0, header=None, nrows=20)
    start_dt: datetime | None = None
    end_dt: datetime | None = None
    for value in meta.iloc[:, 0].dropna().astype(str):
        if "起始时间" in value and "终止时间" in value:
            dates = re.findall(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]", value)
            if len(dates) >= 2:
                start_dt = datetime.strptime(dates[0], "%Y-%m-%d %H:%M:%S")
                end_dt = datetime.strptime(dates[1], "%Y-%m-%d %H:%M:%S")
            break

    header_row = None
    for idx, row in meta.iterrows():
        if str(row.iloc[0]).strip() == "交易时间":
            header_row = idx
            break
    if header_row is None:
        raw = pd.read_excel(path, sheet_name=0, header=None)
        for idx, row in raw.iterrows():
            if str(row.iloc[0]).strip() == "交易时间":
                header_row = idx
                break
    if header_row is None:
        raise ValueError(f"WeChat Pay workbook header row not found: {path}")

    df = pd.read_excel(path, sheet_name=0, header=header_row)
    rows: list[dict[str, Any]] = []
    for idx, row in df.iterrows():
        tx_time_raw = row.get("交易时间")
        if pd.isna(tx_time_raw):
            continue
        tx_dt = pd.to_datetime(tx_time_raw).to_pydatetime()
        tx_date = tx_dt.date()
        if since and tx_date < since:
            continue
        side = _clean_text(row.get("收/支"))
        amount_abs = _to_decimal(row.get("金额(元)")) or Decimal("0")
        if side == "收入":
            amount = amount_abs
        elif side == "支出":
            amount = -amount_abs
        else:
            amount = Decimal("0")

        trade_type = _clean_text(row.get("交易类型"))
        counterparty = _clean_text(row.get("交易对方"))
        product = _clean_text(row.get("商品"))
        payment_method = _clean_text(row.get("支付方式"))
        status = _clean_text(row.get("当前状态"))
        trade_no = _clean_text(row.get("交易单号"))
        merchant_no = _clean_text(row.get("商户单号"))
        note = _clean_text(row.get("备注"))
        description = " ".join(
            x for x in [counterparty, product, payment_method, status, note] if x and x != "/"
        )
        uid = _row_uid(
            profile["account_code"],
            path.name,
            idx,
            tx_dt.isoformat(),
            side,
            amount_abs,
            trade_no,
            merchant_no,
        )
        rows.append(
            {
                "source_uid": uid,
                "transaction_date": tx_date,
                "posting_date": tx_date,
                "amount": amount,
                "currency": profile.get("currency", "CNY"),
                "balance": None,
                "original_amount": amount_abs,
                "original_currency": profile.get("currency", "CNY"),
                "summary": trade_type,
                "description": description,
                "counterparty": counterparty,
                "raw": {
                    "row_idx": int(idx),
                    "transaction_time": tx_dt.isoformat(sep=" "),
                    "side": side,
                    "amount": str(amount_abs),
                    "payment_method": payment_method,
                    "status": status,
                    "trade_no": trade_no,
                    "merchant_no": merchant_no,
                    "source_file": str(path),
                    "statement_template": "wechat_pay_xlsx",
                },
            }
        )

    return ParsedStatement(
        profile_code=profile["profile_code"],
        source_file=str(path),
        bank=profile.get("bank", "WeChat Pay"),
        account_code=profile["account_code"],
        account_kind=profile.get("account_kind", "third_party_payment"),
        account_tail=None,
        statement_start=start_dt.date() if start_dt else None,
        statement_end=end_dt.date() if end_dt else None,
        statement_balance=None,
        rows=rows,
    )


def parse_guangfa_credit_pdf(path: Path, profile: dict[str, Any], since: date | None = None) -> ParsedStatement:
    """Parse a Guangfa Bank combined credit-card monthly statement PDF.

    Guangfa exports multiple card tails in one PDF. A profile may provide
    ``card_tail`` so PAMS can stage CNY and USD card accounts separately.
    """
    rows: list[dict[str, Any]] = []
    account_tail = _clean_text(profile.get("card_tail"))
    profile_tail = account_tail or None
    statement_start: date | None = None
    statement_end: date | None = None
    statement_balance: Decimal | None = None
    current_tail: str | None = None

    with pdfplumber.open(path) as pdf:
        full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        cycle_match = re.search(
            r"账单周期\s+(\d{4}/\d{2}/\d{2})\s*-\s*(\d{4}/\d{2}/\d{2})",
            full_text,
        )
        if cycle_match:
            statement_start = _parse_date_yyyy_slash(cycle_match.group(1))
            statement_end = _parse_date_yyyy_slash(cycle_match.group(2))

        for table in pdf.pages[0].extract_tables() or []:
            for row in table:
                if not row or _clean_text(row[0]) != profile_tail:
                    continue
                statement_balance = _statement_balance_amount(row[1] if len(row) > 1 else None)
                break
            if statement_balance is not None:
                break

        line_idx = 0
        for raw_line in full_text.splitlines():
            line_idx += 1
            tail_match = re.match(r"卡号：\d+\*+(\d{4})", _clean_text(raw_line))
            if tail_match:
                current_tail = tail_match.group(1)
                continue
            if profile_tail and current_tail != profile_tail:
                continue
            parsed = _parse_guangfa_credit_transaction_line(raw_line)
            if not parsed:
                continue
            tx_date = parsed["transaction_date"]
            if since and tx_date < since:
                continue
            settlement_amount = parsed["settlement_amount"]
            # Positive statement rows increase card liability. Store cashflow view:
            # charges are negative; repayment/refund rows become positive.
            amount = -settlement_amount
            uid = _row_uid(
                profile["account_code"],
                path.name,
                line_idx,
                current_tail,
                parsed["transaction_date"],
                parsed["posting_date"],
                parsed["original_amount"],
                parsed["original_currency"],
                parsed["settlement_amount"],
                parsed["settlement_currency"],
                parsed["description"],
            )
            rows.append(
                {
                    "source_uid": uid,
                    "transaction_date": parsed["transaction_date"],
                    "posting_date": parsed["posting_date"],
                    "amount": amount,
                    "currency": parsed["settlement_currency"] or profile.get("currency", "CNY"),
                    "balance": None,
                    "original_amount": parsed["original_amount"],
                    "original_currency": parsed["original_currency"],
                    "summary": parsed["summary"],
                    "description": parsed["description"],
                    "counterparty": parsed["counterparty"],
                    "raw": {
                        "line": raw_line,
                        "line_idx": line_idx,
                        "card_tail": current_tail,
                        "source_file": str(path),
                        "statement_template": "guangfa_credit_pdf",
                        "original_currency_text": parsed["original_currency_text"],
                        "settlement_amount": str(parsed["settlement_amount"]),
                        "settlement_currency_text": parsed["settlement_currency_text"],
                    },
                }
            )

    return ParsedStatement(
        profile_code=profile["profile_code"],
        source_file=str(path),
        bank=profile.get("bank", "Guangfa Bank"),
        account_code=profile["account_code"],
        account_kind=profile.get("account_kind", "credit_card"),
        account_tail=profile_tail,
        statement_start=statement_start,
        statement_end=statement_end,
        statement_balance=statement_balance,
        rows=rows,
    )


def parse_santander_mx_account_csv(path: Path, profile: dict[str, Any], since: date | None = None) -> ParsedStatement:
    """Parse a Santander Mexico account-detail CSV exported from online banking."""
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    lines = text.splitlines()
    rows: list[dict[str, Any]] = []
    account_tail: str | None = _clean_text(profile.get("account_tail")) or None
    statement_start: date | None = None
    statement_end: date | None = None

    if lines:
        first_line = _clean_text(lines[0])
        account_match = re.search(r"cuenta\s+\*\*(\d+)", first_line, re.IGNORECASE)
        if account_match:
            account_tail = account_match.group(1)
        range_match = re.search(
            r"Detalle del\s+(\d{2}/[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]{3}/\d{4})\s+al\s+(\d{2}/[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]{3}/\d{4})",
            first_line,
            re.IGNORECASE,
        )
        if range_match:
            statement_start = _parse_santander_date(range_match.group(1))
            statement_end = _parse_santander_date(range_match.group(2))

    reader = csv.reader(lines[1:])
    header_seen = False
    for row_idx, row in enumerate(reader, start=2):
        if not row:
            continue
        if not header_seen:
            header_seen = [x.strip().upper() for x in row[:4]] == ["FECHA", "HORA", "SUCURSAL", "CONCEPTO"]
            continue
        item = _parse_santander_account_row(row, row_idx=row_idx, path=path, profile=profile)
        if not item:
            continue
        if since and item["transaction_date"] < since:
            continue
        rows.append(item)

    rows.sort(key=lambda r: (r["transaction_date"], str((r.get("raw") or {}).get("transaction_time") or "")))
    statement_balance = rows[-1]["balance"] if rows else None
    return ParsedStatement(
        profile_code=profile["profile_code"],
        source_file=str(path),
        bank=profile.get("bank", "Santander Mexico"),
        account_code=profile["account_code"],
        account_kind=profile.get("account_kind", "debit"),
        account_tail=account_tail,
        statement_start=statement_start,
        statement_end=statement_end,
        statement_balance=statement_balance,
        rows=rows,
    )


def parse_manual_statement_csv(path: Path, profile: dict[str, Any], since: date | None = None) -> ParsedStatement:
    """Parse a manually normalized statement CSV.

    The template is used for accounts whose official export is unavailable or
    only available as screenshots. Required columns are ``transaction_date`` and
    ``amount``. Optional columns include ``posting_date``, ``summary``,
    ``description``, ``counterparty``, ``currency``, ``balance``,
    ``original_amount`` and ``original_currency``.
    """
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row_idx, raw_row in enumerate(reader, start=2):
            tx_date = _parse_flexible_date(raw_row.get("transaction_date"))
            if tx_date is None or (since and tx_date < since):
                continue
            amount = _to_decimal(raw_row.get("amount"))
            if amount is None:
                continue
            posting_date = _parse_flexible_date(raw_row.get("posting_date")) or tx_date
            currency = _clean_text(raw_row.get("currency")) or profile.get("currency", "CNY")
            original_amount = _to_decimal(raw_row.get("original_amount")) or amount
            original_currency = _clean_text(raw_row.get("original_currency")) or currency
            balance = _to_decimal(raw_row.get("balance"))
            summary = _clean_text(raw_row.get("summary"))
            description = _clean_text(raw_row.get("description")) or summary
            counterparty = _clean_text(raw_row.get("counterparty"))
            uid = _row_uid(
                profile["account_code"],
                path.name,
                row_idx,
                tx_date,
                posting_date,
                amount,
                currency,
                balance,
                summary,
                description,
                counterparty,
            )
            rows.append(
                {
                    "source_uid": uid,
                    "transaction_date": tx_date,
                    "posting_date": posting_date,
                    "amount": amount,
                    "currency": currency,
                    "balance": balance,
                    "original_amount": original_amount,
                    "original_currency": original_currency,
                    "summary": summary,
                    "description": description,
                    "counterparty": counterparty,
                    "raw": {
                        "row": raw_row,
                        "row_idx": row_idx,
                        "source_file": str(path),
                        "statement_template": "manual_statement_csv",
                    },
                }
            )

    rows.sort(key=lambda r: (r["transaction_date"], int((r.get("raw") or {}).get("row_idx") or 0)))
    balance_rows = [r for r in rows if r.get("balance") is not None]
    statement_start = rows[0]["transaction_date"] if rows else None
    statement_end = rows[-1]["transaction_date"] if rows else None
    statement_balance = balance_rows[-1]["balance"] if balance_rows else None
    return ParsedStatement(
        profile_code=profile["profile_code"],
        source_file=str(path),
        bank=profile.get("bank", "Manual"),
        account_code=profile["account_code"],
        account_kind=profile.get("account_kind", "manual"),
        account_tail=_clean_text(profile.get("account_tail")) or None,
        statement_start=statement_start,
        statement_end=statement_end,
        statement_balance=statement_balance,
        rows=rows,
    )


def _alipay_signed_amount(side: str, amount_abs: Decimal, description: str) -> Decimal:
    """Convert Alipay export side labels to account cashflow signs."""
    if side == "收入":
        return amount_abs
    if side == "支出":
        return -amount_abs
    if side == "不计收支":
        if re.search(r"提现|转出", description):
            return -amount_abs
        if re.search(r"充值|转入", description):
            return amount_abs
    return Decimal("0")


def _parse_alipay_statement_text(text: str, path: Path, profile: dict[str, Any], since: date | None = None) -> ParsedStatement:
    rows: list[dict[str, Any]] = []
    lines = text.splitlines()
    statement_start: date | None = None
    statement_end: date | None = None
    account_tail: str | None = None
    entry_name = _clean_text(profile.get("_zip_entry"))
    statement_balance = _to_decimal(profile.get("statement_balance"))

    for line in lines[:30]:
        account_match = re.search(r"支付宝账户：(.+)$", line)
        if account_match:
            account_tail = _clean_text(account_match.group(1))
        range_match = re.search(r"起始时间：\[(.*?)\].*终止时间：\[(.*?)\]", line)
        if range_match:
            start_dt = _parse_flexible_datetime(range_match.group(1))
            end_dt = _parse_flexible_datetime(range_match.group(2))
            statement_start = start_dt.date() if start_dt else None
            statement_end = end_dt.date() if end_dt else None

    header_idx = None
    for idx, line in enumerate(lines):
        if line.startswith("交易时间,交易分类,交易对方"):
            header_idx = idx
            break
    if header_idx is None:
        raise ValueError(f"Alipay CSV header row not found: {path}")

    reader = csv.DictReader(lines[header_idx:])
    for row_idx, raw_row in enumerate(reader, start=header_idx + 2):
        row = {str(k or "").strip(): _clean_text(v).strip("\t ") for k, v in raw_row.items() if k is not None}
        tx_dt = _parse_flexible_datetime(row.get("交易时间"))
        if tx_dt is None:
            continue
        tx_date = tx_dt.date()
        if since and tx_date < since:
            continue
        amount_abs = _to_decimal(row.get("金额"))
        if amount_abs is None:
            continue
        side = _clean_text(row.get("收/支"))
        trade_type = _clean_text(row.get("交易分类"))
        counterparty = _clean_text(row.get("交易对方"))
        product = _clean_text(row.get("商品说明"))
        method = _clean_text(row.get("收/付款方式"))
        status = _clean_text(row.get("交易状态"))
        note = _clean_text(row.get("备注"))
        description = " ".join(x for x in [trade_type, counterparty, product, method, status, note] if x and x != "/")
        amount = _alipay_signed_amount(side, amount_abs, description)
        order_no = _clean_text(row.get("交易订单号"))
        merchant_order_no = _clean_text(row.get("商家订单号"))
        uid = _row_uid(
            profile["account_code"],
            path.name,
            entry_name,
            row_idx,
            tx_dt.isoformat(sep=" "),
            side,
            amount_abs,
            order_no,
            merchant_order_no,
        )
        rows.append(
            {
                "source_uid": uid,
                "transaction_date": tx_date,
                "posting_date": tx_date,
                "amount": amount,
                "currency": profile.get("currency", "CNY"),
                "balance": None,
                "original_amount": amount_abs,
                "original_currency": profile.get("currency", "CNY"),
                "summary": trade_type,
                "description": description,
                "counterparty": counterparty,
                "raw": {
                    "row": row,
                    "row_idx": row_idx,
                    "transaction_time": tx_dt.isoformat(sep=" "),
                    "side": side,
                    "amount": str(amount_abs),
                    "payment_method": method,
                    "status": status,
                    "order_no": order_no,
                    "merchant_order_no": merchant_order_no,
                    "source_file": str(path),
                    "zip_entry": entry_name,
                    "statement_template": "alipay_csv_zip",
                },
            }
        )

    rows.sort(key=lambda r: (r["transaction_date"], str((r.get("raw") or {}).get("transaction_time") or "")))
    return ParsedStatement(
        profile_code=profile["profile_code"],
        source_file=str(path),
        bank=profile.get("bank", "Alipay"),
        account_code=profile["account_code"],
        account_kind=profile.get("account_kind", "third_party_payment"),
        account_tail=account_tail,
        statement_start=statement_start,
        statement_end=statement_end,
        statement_balance=statement_balance,
        rows=rows,
    )


def parse_alipay_csv_zip(path: Path, profile: dict[str, Any], since: date | None = None) -> ParsedStatement:
    """Parse a password-protected Alipay transaction export ZIP."""
    password = str(profile.get("zip_password") or "").encode("utf-8") or None
    with zipfile.ZipFile(path) as z:
        entries = [info for info in z.infolist() if info.filename.lower().endswith(".csv")]
        if not entries:
            raise ValueError(f"Alipay ZIP contains no CSV file: {path}")
        entry = entries[0]
        data = z.read(entry, pwd=password)
    profile_with_entry = dict(profile)
    profile_with_entry["_zip_entry"] = entry.filename
    return _parse_alipay_statement_text(_decode_statement_bytes(data), path, profile_with_entry, since)


def parse_icbc_debit_pdf(path: Path, profile: dict[str, Any], since: date | None = None) -> ParsedStatement:
    """Parse an ICBC debit-account history PDF, including password-protected files."""
    rows: list[dict[str, Any]] = []
    account_tail: str | None = _clean_text(profile.get("account_tail")) or None
    statement_start: date | None = None
    statement_end: date | None = None
    password = str(profile.get("pdf_password") or "")

    with pdfplumber.open(path, password=password) as pdf:
        first_text = pdf.pages[0].extract_text() or ""
        card_match = re.search(r"卡号\s*(\d+)", first_text)
        if card_match:
            account_tail = card_match.group(1)[-4:]
        range_match = re.search(r"起止.*?(\d{4}-\d{2}-\d{2}).*?(\d{4}-\d{2}-\d{2})", first_text, re.S)
        if range_match:
            statement_start = datetime.strptime(range_match.group(1), "%Y-%m-%d").date()
            statement_end = datetime.strptime(range_match.group(2), "%Y-%m-%d").date()

        for page_idx, page in enumerate(pdf.pages):
            for table in page.extract_tables() or []:
                if not table or not table[0]:
                    continue
                header = [_clean_text(x) for x in table[0]]
                if "交易日期" not in header[0]:
                    continue
                for row_idx, row in enumerate(table[1:], start=1):
                    item = _parse_icbc_debit_row(row, page_idx=page_idx, row_idx=row_idx, path=path, profile=profile)
                    if item is None:
                        continue
                    if since and item["transaction_date"] < since:
                        continue
                    rows.append(item)

    statement_balance = rows[-1]["balance"] if rows else None
    return ParsedStatement(
        profile_code=profile["profile_code"],
        source_file=str(path),
        bank=profile.get("bank", "ICBC"),
        account_code=profile["account_code"],
        account_kind=profile.get("account_kind", "debit"),
        account_tail=account_tail,
        statement_start=statement_start,
        statement_end=statement_end,
        statement_balance=statement_balance,
        rows=rows,
    )


def parse_statement(path: str | Path, profile: dict[str, Any], since: date | None = None) -> ParsedStatement:
    """Parse a bank statement using the configured template."""
    p = Path(path)
    template = profile.get("statement_template")
    if template == "ccb_debit_pdf":
        return parse_ccb_debit_pdf(p, profile, since)
    if template == "ccb_credit_pdf":
        return parse_ccb_credit_pdf(p, profile, since)
    if template == "wechat_pay_xlsx":
        return parse_wechat_pay_xlsx(p, profile, since)
    if template == "guangfa_credit_pdf":
        return parse_guangfa_credit_pdf(p, profile, since)
    if template == "santander_mx_account_csv":
        return parse_santander_mx_account_csv(p, profile, since)
    if template == "manual_statement_csv":
        return parse_manual_statement_csv(p, profile, since)
    if template == "alipay_csv_zip":
        return parse_alipay_csv_zip(p, profile, since)
    if template == "icbc_debit_pdf":
        return parse_icbc_debit_pdf(p, profile, since)
    raise ValueError(f"Unsupported statement_template: {template}")
