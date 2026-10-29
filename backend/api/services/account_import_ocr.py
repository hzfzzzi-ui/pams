"""Local, bounded screenshot OCR. Results are evidence-backed drafts, not facts."""

from __future__ import annotations

import csv
import os
import re
import shutil
import subprocess
import warnings
from decimal import Decimal, InvalidOperation
from io import BytesIO, StringIO
from typing import Any

from services.account_import_parse import FIELDS, _issue, _value

MAX_BYTES, MAX_PIXELS, MAX_OUTPUT = 10 * 1024**2, 20_000_000, 2 * 1024**2
MAX_WORDS, MAX_TSV_ROWS, MAX_ROWS = 10000, 20000, 2000
OCR_TIMEOUT, PROBE_TIMEOUT = 20, 3
LABELS = {
    "基金名称": "name", "证券名称": "name", "股票名称": "name", "名称": "name",
    "基金代码": "symbol", "证券代码": "symbol", "股票代码": "symbol", "代码": "symbol",
    "总份额": "quantity", "持有份额": "quantity", "持仓份额": "quantity", "份额": "quantity",
    "持仓数量": "quantity", "成交数量": "quantity", "股数": "quantity", "数量": "quantity",
    "可用数量": "_available", "可用份额": "_available",
    "持仓成本": "cost", "成本金额": "cost", "报表成本": "cost", "摊薄成本金额": "cost",
    "成本价": "_unit_cost", "持仓成本价": "_unit_cost", "摊薄成本价": "_unit_cost",
    "持仓市值": "market_value", "资产市值": "market_value", "市值": "market_value",
    "单位净值": "price", "最新净值": "price", "净值": "price", "现价": "price", "价格": "price", "成交价格": "price",
    "成交金额": "gross_amount", "发生金额": "gross_amount", "交易金额": "gross_amount", "金额": "gross_amount",
    "金额(税费)": "_amount_charges", "金额（税费）": "_amount_charges", "金额税费": "_amount_charges",
    "手续费": "fee_amount", "税费合计": "fee_amount", "税费": "fee_amount", "金额税费total": "fee_amount",
    "印花税": "tax_amount", "净收付": "net_cash", "币种": "currency", "状态": "status",
    "资产类型": "asset_type", "证券类型": "asset_type", "产品类型": "asset_type",
    "交易日期": "business_date", "成交日期": "business_date", "持仓日期": "business_date", "日期": "business_date",
    "确认日期": "confirmed_date", "成交时间": "trade_time", "交易时间": "trade_time",
    "成交编号": "source_ref", "流水号": "source_ref", "订单号": "source_ref", "方向": "action", "业务类型": "action",
}
LABEL_PATTERN = re.compile("|".join(map(re.escape, sorted(LABELS, key=len, reverse=True))))
DATE = r"(?:[12][0-9]{3}[-/.年][0-9]{1,2}[-/.月][0-9]{1,2}日?|[12][0-9]{7})"
TITLE = re.compile(r"^(证券买入|证券卖出|股票买入|股票卖出|基金申购|基金赎回|买入|卖出|申购|赎回|分红)\s*[-—:：]\s*(.+)$")
TABLE_PRICE = re.compile(r"(?:成交)?价格[/／]?(?:数量|股数)")
TABLE_AMOUNT = re.compile(r"(成交金额|净收付|金额)[/／]?税费")
MONTH_GROUP = re.compile(r"^[\W_]*([12][0-9]{3})\s*[-/年]\s*([0-9]{1,2})月?(?![-/.0-9日])")
PARTIAL_STAMP = re.compile(r"^(?:买|卖|申购|赎回)?\s*([0-9]{2})-([0-9]{2})\s*([0-9]{2}:[0-9]{2}(?::[0-9]{2})?)$")


def _validate_image(content: bytes) -> tuple[int, int]:
    from PIL import Image, UnidentifiedImageError

    if not isinstance(content, bytes) or not 0 < len(content) <= MAX_BYTES:
        raise ValueError("Image must contain between 1 byte and 10 MiB")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as image:
                width, height = image.size
                if image.format not in {"PNG", "JPEG"} or getattr(image, "n_frames", 1) != 1:
                    raise ValueError("Only single-frame PNG/JPEG images are supported")
                if width < 1 or height < 1 or width * height > MAX_PIXELS:
                    raise ValueError("Image exceeds 20 megapixels")
                image.verify()
            with Image.open(BytesIO(content)) as image:
                image.load()
            return width, height
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("Malformed or oversized image") from exc


def _run(command: list[str], *, content: bytes | None = None, timeout: int) -> bytes:
    process = subprocess.run(command, input=content, capture_output=True, timeout=timeout,
                             check=False, shell=False, env={**os.environ, "OMP_THREAD_LIMIT": "1"})
    if process.returncode:
        raise RuntimeError("Local OCR process failed")
    if len(process.stdout) > MAX_OUTPUT or len(process.stderr) > MAX_OUTPUT:
        raise ValueError("OCR output limit exceeded")
    return process.stdout


def _engine() -> str | None:
    executable = shutil.which("tesseract")
    if executable:
        try:
            languages = _run([executable, "--list-langs"], timeout=PROBE_TIMEOUT).decode("utf-8").splitlines()
            if {"chi_sim", "eng"} <= {line.strip() for line in languages}:
                return executable
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
            pass
    return None


def ocr_available() -> bool:
    """True only when Pillow and local Tesseract's Chinese/English data exist."""
    try:
        from PIL import Image
        return Image is not None and _engine() is not None
    except ImportError:
        return False


def _read_tsv(data: bytes, width: int, height: int) -> tuple[str, list[dict], list[dict]]:
    if len(data) > MAX_OUTPUT:
        raise ValueError("OCR output limit exceeded")
    tsv = data.decode("utf-8-sig")
    reader = csv.DictReader(StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE)
    required = {"level", "page_num", "block_num", "par_num", "line_num", "left", "top", "width", "height", "conf", "text"}
    if not reader.fieldnames or not required <= set(reader.fieldnames) or len(set(reader.fieldnames)) != len(reader.fieldnames):
        raise ValueError("Malformed OCR TSV headers")
    words: list[dict] = []
    groups: dict[tuple, list[int]] = {}
    for index, row in enumerate(reader):
        if index >= MAX_TSV_ROWS or None in row or any(row[k] is None for k in required):
            raise ValueError("Malformed or oversized OCR TSV")
        if row["level"] != "5" or not row["text"].strip():
            continue
        page, block, paragraph, line = (int(row[k]) for k in ("page_num", "block_num", "par_num", "line_num"))
        left, top, w, h = (int(row[k]) for k in ("left", "top", "width", "height"))
        if not re.fullmatch(r"-?[0-9]{1,3}(?:\.[0-9]{1,6})?", row["conf"]):
            raise ValueError("Invalid OCR confidence")
        confidence = Decimal(row["conf"])
        if page != 1 or min(block, paragraph, line) < 1 or min(left, top) < 0 or min(w, h) <= 0 or left + w > width or top + h > height:
            raise ValueError("Invalid OCR word box")
        if not confidence.is_finite() or not -1 <= confidence <= 100 or len(words) >= MAX_WORDS:
            raise ValueError("Invalid confidence or OCR word limit exceeded")
        groups.setdefault((page, block, paragraph, line), []).append(len(words))
        words.append({"text": row["text"], "confidence": format(confidence, "f"),
                      "box": {"left": left, "top": top, "width": w, "height": h}})
    ordered = sorted(groups.values(), key=lambda ids: (min(words[i]["box"]["top"] for i in ids), min(words[i]["box"]["left"] for i in ids)))
    lines = []
    for number, ids in enumerate(ordered, 1):
        ids.sort(key=lambda i: words[i]["box"]["left"])
        for i in ids:
            words[i]["line"] = number
        lines.append({"number": number, "ids": ids, "text": " ".join(words[i]["text"] for i in ids)})
    return tsv, words, lines


def _compact(text: str) -> str:
    return re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text).strip()


def _pairs(text: str) -> list[tuple[str, str]]:
    matches = list(LABEL_PATTERN.finditer(text))
    pairs = [(m[0], text[m.end():matches[i + 1].start() if i + 1 < len(matches) else len(text)].strip(" :：|")) for i, m in enumerate(matches)]
    pairs = [(label, re.sub(r"^[（(]\s*(?:元/份|元|份|股)\s*[）)]\s*", "", raw).strip()) for label, raw in pairs]
    if matches and LABELS[matches[0][0]] == "name" and any(raw for _, raw in pairs):
        return [(matches[0][0], text[matches[0].end():].strip(" :：|"))]
    return pairs


def _trade_table(lines: list[dict], words: list[dict]) -> list[dict]:
    # Tesseract line IDs may separate columns; visual bands retain word evidence.
    bands: list[dict] = []
    for i in sorted(range(len(words)), key=lambda i: (words[i]["box"]["top"] + words[i]["box"]["height"] / 2, words[i]["box"]["left"])):
        box = words[i]["box"]
        center = box["top"] + box["height"] / 2
        if bands and abs(center - bands[-1]["center"]) <= max(box["height"], bands[-1]["height"]) * 0.6:
            band = bands[-1]
            band["center"] = (band["center"] * len(band["ids"]) + center) / (len(band["ids"]) + 1)
            band["ids"].append(i)
            band["height"] = max(band["height"], box["height"])
        else:
            bands.append({"center": center, "height": box["height"], "ids": [i]})
    visual = []
    for band in bands:
        ids = sorted(band["ids"], key=lambda i: words[i]["box"]["left"])
        visual.append({"number": min(words[i]["line"] for i in ids), "ids": ids, "text": " ".join(words[i]["text"] for i in ids)})

    def span_box(line, pattern):
        text, spans = "", []
        for i in line["ids"]:
            token = re.sub(r"\s+", "", words[i]["text"])
            spans.append((len(text), len(text) + len(token), i))
            text += token
        match = pattern.search(text)
        if not match:
            return None
        boxes = [words[i]["box"] for start, end, i in spans if start < match.end() and end > match.start()]
        return min(b["left"] for b in boxes), max(b["left"] + b["width"] for b in boxes), match

    def cell(ids):
        raw = " ".join(words[i]["text"] for i in ids)
        raw = re.sub(r"\s*([.,:])\s*", r"\1", raw)
        return re.sub(r"^([+\-−])\s+", r"\1", raw).replace("−", "-").strip()

    layout = None
    found = False
    for line in visual:
        month = MONTH_GROUP.match(line["text"])
        if month:
            line["trade_month"] = (month[1], month[2])
        price, amount = span_box(line, TABLE_PRICE), span_box(line, TABLE_AMOUNT)
        if price and amount and price[1] < amount[0]:
            layout = {"left": price[0], "split": (price[1] + amount[0]) / 2,
                      "amount_field": {"成交金额": "gross_amount", "净收付": "net_cash"}.get(amount[2][1]),
                      "header_ids": list(line["ids"])}
            line["trade_header"] = layout
            found = True
            continue
        if layout is None:
            continue
        columns: list[list[int]] = [[], [], []]
        for i in line["ids"]:
            box = words[i]["box"]
            x = box["left"] + box["width"] / 2
            columns[0 if x < layout["left"] else 1 if x < layout["split"] else 2].append(i)
        description = _compact(" ".join(words[i]["text"] for i in columns[0]))
        month = MONTH_GROUP.match(description)
        if month:
            line["trade_month"] = (month[1], month[2])
        elif TITLE.fullmatch(description):
            line["trade_title"] = description
            line["trade_cells"] = {"price": cell(columns[1]), "amount": cell(columns[2]),
                                   "amount_ids": columns[2], "amount_field": layout["amount_field"]}
        else:
            stamp = PARTIAL_STAMP.fullmatch(re.sub(r"\s*([-:])\s*", r"\1", description))
            if not stamp and len(columns[0]) > 1:
                badge, first = (words[i]["box"] for i in columns[0][:2])
                # A separate compact badge may be OCR'd as punctuation or a digit.
                if badge["width"] <= badge["height"] * 2 and badge["left"] + badge["width"] < first["left"]:
                    suffix = " ".join(words[i]["text"] for i in columns[0][1:])
                    stamp = PARTIAL_STAMP.fullmatch(re.sub(r"\s*([-:])\s*", r"\1", suffix))
            if stamp:
                line["trade_detail"] = {"month": stamp[1], "day": stamp[2], "clock": stamp[3],
                                        "quantity": cell(columns[1]), "charges": cell(columns[2])}
            elif re.search(r"[\u4e00-\u9fff]", description) and (
                columns[1] or columns[2] or re.match(r"^(?:证券|股票|基金)?(?:买|卖|申|赎)", description)
            ):
                line["unrecognized_trade_title"] = description
    return visual if found else lines


def _drafts(lines: list[dict], words: list[dict], profile: str, kind: str, diagnostics: list | None = None) -> tuple[list[dict], list[str]]:
    rows: list[dict] = []
    headers: list[str] = []
    context: dict[str, Any] = {"business_date": None, "date_text": None, "title": None, "word_ids": []}
    current: dict[str, Any] = {}
    pending: list[tuple[str, str]] = []
    diagnostics = diagnostics if diagnostics is not None else []

    def diagnostic(line, code, message):
        warning = _issue(None, code, message, "warning")
        warning["source"] = {"row": line["number"], "raw_text": line["text"], "word_ids": list(line["ids"])}
        diagnostics.append(warning)

    def reset():
        current.clear()
        current.update(fields=dict.fromkeys(FIELDS), issues=[], lines=[], attempted=set(), context=dict(context), amount_candidates=[])

    def touch(line):
        if not current["lines"]:
            current["context"] = dict(context)
        if line not in current["lines"]:
            current["lines"].append(line)

    def flush():
        meaningful = current["attempted"] - {"kind", "asset_type", "currency", "status", "fee_scope", "cost_basis_type"}
        if any(current["fields"][f] is not None for f in meaningful):
            if len(rows) >= MAX_ROWS:
                raise ValueError("OCR candidate row limit exceeded")
            fields, issues = current["fields"], current["issues"]
            fields["kind"] = kind
            if "business_date" not in current["attempted"]:
                fields["business_date"] = current["context"]["business_date"]
            if fields["fee_scope"] == "total_charges":
                if fields["tax_amount"] is not None:
                    issues.append(_issue("tax_amount", "aggregate_breakdown", "Aggregate charges do not establish a tax breakdown", "warning"))
                fields["tax_amount"] = None
            if profile == "tencent_licaitong" and fields["asset_type"] == "fund" and fields["action"] in {"buy", "sell"}:
                fields["action"] = {"buy": "subscribe", "sell": "redeem"}[fields["action"]]
            if fields["asset_type"] == "stock" and fields["action"] in {"subscribe", "redeem"}:
                fields["action"] = None
                issues.append(_issue("action", "ambiguous_action", "Stock subscription/redemption requires review"))
            required = ["business_date", "asset_type"] + (["action", "status"] if kind == "trade" else ["quantity"] if kind == "holding" else ["action"])
            for field in required:
                if fields[field] is None:
                    issues.append(_issue(field, "missing", f"Missing {field}"))
            if kind != "cash" and not (fields["symbol"] or fields["name"]):
                issues.append(_issue("symbol", "missing", "Missing both symbol and name"))
            issues.append(_issue(None, "ocr_review_required", "OCR draft requires human verification; confidence is not approval", "warning"))
            ids = sorted(set(current["context"]["word_ids"] + [i for line in current["lines"] for i in line["ids"]]))
            if any(Decimal(words[i]["confidence"]) < 80 for i in ids):
                issues.append(_issue(None, "low_confidence", "One or more source words have low/unknown confidence", "warning"))
            rows.append({"fields": fields, "source": {"sheet": None, "row": current["lines"][0]["number"],
                         "raw_text": "\n".join(line["text"] for line in current["lines"]), "words": [words[i] for i in ids],
                         "context": current["context"], "amount_candidates": current["amount_candidates"]},
                         "issues": issues, "require_review": False, "acknowledged": False})
        reset()

    def assign(label, raw):
        field = LABELS[label]
        if field.startswith("_"):
            if field == "_amount_charges":
                match = re.fullmatch(r"(.+?)\s*[（(]([^()（）]+)[）)]", raw)
                assign("成交金额", match[1] if match else raw)
                if match:
                    assign("税费合计", match[2])
                else:
                    current["issues"].append(_issue("fee_amount", "missing_charge_breakdown", "Amount has no explicit charges breakdown", "warning"))
            else:
                current["issues"].append(_issue("cost" if field == "_unit_cost" else "quantity", "unmapped_label", f"Not a total: {label}", "warning"))
                if "摊薄" in label and "cost" not in current["attempted"]:
                    current["fields"]["cost_basis_type"] = "diluted_cost"
            return
        current["attempted"].add(field)
        if field == "fee_amount":
            scope = "separate" if label == "手续费" else "total_charges"
            prior = current["fields"]["fee_scope"]
            if prior is not None and prior != scope:
                current["fields"]["fee_amount"] = None
                current["issues"].append(_issue("fee_amount", "conflicting_values", "Separate and aggregate fees need selection"))
            current["fields"]["fee_scope"] = "total_charges" if "total_charges" in {prior, scope} else scope
        try:
            cleaned = re.sub(r"\s*(?:元|份|股)$", "", raw).strip()
            if field == "symbol" and not re.fullmatch(r"[0-9]{6}", cleaned):
                raise ValueError("OCR codes require six explicit digits")
            if field == "trade_time" and re.match(DATE, cleaned):
                parts = re.split(r"[T\s]+", cleaned, maxsplit=1)
                assign("成交日期", parts[0])
                cleaned = parts[1] if len(parts) > 1 else ""
            value = _value(field, cleaned)
            if field in current["fields"] and current["fields"][field] not in {None, value}:
                current["fields"][field] = None
                current["issues"].append(_issue(field, "conflicting_values", "Multiple OCR values require explicit selection"))
            elif not any(i["field"] == field and i["code"] == "conflicting_values" for i in current["issues"]):
                current["fields"][field] = value
            if label in {"基金代码", "基金名称", "股票代码", "股票名称"}:
                asset_type = "fund" if label.startswith("基金") else "stock"
                conflict = any(i["field"] == "asset_type" and i["code"] == "conflicting_values" for i in current["issues"])
                if conflict or current["fields"]["asset_type"] not in {None, asset_type}:
                    current["issues"].append(_issue("asset_type", "conflicting_values", "Conflicting asset labels"))
                    current["fields"]["asset_type"] = None
                else:
                    current["fields"]["asset_type"] = asset_type
            if field == "cost" and value is not None:
                basis = "holding_cost" if label == "持仓成本" else "diluted_cost" if label == "摊薄成本金额" else "unknown"
                current["fields"]["cost_basis_type"] = basis
                if basis != "holding_cost":
                    current["issues"].append(_issue("cost_basis_type", basis, "Cost basis requires review", "warning"))
        except (ValueError, InvalidOperation):
            current["fields"][field] = None
            current["issues"].append(_issue(field, "invalid_value", f"Unrecognized OCR {field}: {raw}"))

    if kind == "trade":
        lines = _trade_table(lines, words)
    reset()
    for line in lines:
        text = _compact(line["text"])
        title = TITLE.fullmatch(line.get("trade_title", text))
        pairs = _pairs(text)
        dates = re.findall(DATE, text)
        if "trade_header" in line:
            flush()
            context.update(business_date=None, date_text=context.get("month_text"), title=line["text"],
                           word_ids=list(line["ids"]) + context.get("month_word_ids", []),
                           table_header_ids=list(line["ids"]))
            headers.extend(h for h in ("价格/数量", "金额/税费") if h not in headers)
            pending = []
            continue
        if "trade_month" in line:
            flush()
            year, month = line["trade_month"]
            year_month = f"{year}-{int(month):02d}" if 1 <= int(month) <= 12 else None
            context.update(business_date=None, year_month=year_month, date_text=line["text"],
                           month_text=line["text"], month_word_ids=list(line["ids"]),
                           word_ids=context.get("table_header_ids", []) + line["ids"])
            pending = []
            continue
        if "unrecognized_trade_title" in line:
            flush()
            diagnostic(line, "unrecognized_trade_title", "Trade title could not be parsed; original text and word boxes retained")
            pending = []
            continue
        if "trade_detail" in line:
            if current["fields"]["name"] is not None and current["fields"]["action"] is not None:
                touch(line)
                detail = line["trade_detail"]
                year_month = current["context"].get("year_month")
                if year_month and year_month.endswith("-" + detail["month"]):
                    assign("成交日期", f"{year_month}-{detail['day']}")
                else:
                    current["attempted"].add("business_date")
                    current["issues"].append(_issue("business_date", "missing_date_context", "Row needs matching explicit YYYY-MM group; no current year is inferred"))
                assign("成交时间", detail["clock"])
                if detail["quantity"]:
                    assign("数量", detail["quantity"])
                if detail["charges"]:
                    assign("税费合计", detail["charges"])
            else:
                diagnostic(line, "orphan_trade_detail", "Trade detail has no recognized title; values were not attached to another trade")
            pending = []
            continue
        if not current["attempted"] and len(pairs) == 1 and LABELS[pairs[0][0]] == "business_date":
            try:
                context.update(business_date=_value("business_date", pairs[0][1]), date_text=line["text"], title=line["text"], word_ids=list(line["ids"]))
            except ValueError:
                context.update(business_date=None, date_text=line["text"], word_ids=list(line["ids"]))
            pending = []
            continue
        is_context = (re.fullmatch(DATE, text) or any(t in text for t in ("持仓", "交易记录", "资产明细"))) and not pairs
        if is_context and len(dates) == 1:
            flush()
            try:
                context.update(business_date=_value("business_date", dates[0]), date_text=line["text"], title=line["text"], word_ids=list(line["ids"]))
            except ValueError:
                context.update(business_date=None, date_text=line["text"], word_ids=list(line["ids"]))
            pending = []
            continue
        if title:
            flush()
            touch(line)
            action = title[1].replace("股票", "证券")
            assign("方向", action)
            assign("名称", title[2])
            if title[1].startswith("股票"):
                assign("资产类型", "stock")
            elif title[1].startswith("基金"):
                assign("资产类型", "fund")
            if "trade_cells" in line:
                cells = line["trade_cells"]
                if cells["price"]:
                    assign("价格", cells["price"])
                if cells["amount"]:
                    field = cells["amount_field"]
                    if field:
                        assign("净收付" if field == "net_cash" else "成交金额", cells["amount"])
                    else:
                        try:
                            value = _value("gross_amount", cells["amount"])
                        except (ValueError, InvalidOperation):
                            value = None
                        current["amount_candidates"].append({"raw_text": cells["amount"], "value": value,
                            "word_ids": list(cells["amount_ids"]), "semantics": "unknown"})
                        current["issues"].append(_issue("gross_amount", "amount_semantics_ambiguous", "Amount/charges header does not establish gross vs net; original amount retained", "warning"))
            pending = []
            continue
        if pairs:
            if any(raw and LABELS[label] in {"name", "symbol"} and current["fields"][LABELS[label]] is not None and
                   (LABELS[label] == "symbol" or any(current["fields"][f] is not None for f in ("quantity", "cost", "market_value", "gross_amount"))) for label, raw in pairs):
                flush()
            touch(line)
            headers.extend(label for label, _ in pairs if label not in headers)
            pending = pairs if all(not raw for _, raw in pairs) else []
            if not pending:
                for label, raw in pairs:
                    assign(label, raw)
        elif pending:
            touch(line)
            values = [words[i]["text"] for i in line["ids"]] if len(pending) > 1 else [text]
            if len(values) == len(pending):
                for (label, _), value in zip(pending, values):
                    assign(label, value)
            else:
                current["issues"].append(_issue(None, "ambiguous_columns", "OCR label/value column counts differ"))
            pending = []
        elif current["lines"]:
            touch(line)
    flush()
    return rows, headers


def parse_image(content: bytes, profile: str, kind: str) -> dict:
    """Validate a screenshot and return local OCR drafts; never calls a writer.

    Invalid arguments/images raise ValueError. Missing engines, timeouts and bad
    TSV return empty rows and explicit ocr_status/warnings. Review/ack flags start
    false; every candidate has an ocr_review_required issue. No dates, symbols,
    status, currency or amounts are supplied from ambient time or asset guesses.
    """
    if profile not in {"tonghuashun", "tencent_licaitong"} or kind not in {"trade", "holding", "cash"}:
        raise ValueError("Unsupported profile or kind")
    result: dict[str, Any] = {"rows": [], "headers": [], "warnings": [], "suggested_mapping": {},
                              "origin": "ocr", "ocr_status": "unavailable", "text": "", "words": []}
    try:
        width, height = _validate_image(content)
    except ImportError:
        result["warnings"].append(_issue(None, "ocr_unavailable", "Pillow is not installed", "warning"))
        return result
    executable = _engine()
    if not executable:
        result["warnings"].append(_issue(None, "ocr_unavailable", "Local Tesseract with chi_sim+eng is unavailable", "warning"))
        return result
    try:
        data = _run([executable, "stdin", "stdout", "-l", "chi_sim+eng", "--psm", "6", "tsv"], content=content, timeout=OCR_TIMEOUT)
        tsv, words, lines = _read_tsv(data, width, height)
        result.update(tsv=tsv, words=words, text="\n".join(line["text"] for line in lines))
        rows, headers = _drafts(lines, words, profile, kind, result["warnings"])
        result.update(rows=rows, headers=headers, ocr_status="ok" if rows else "unrecognized" if words else "empty")
        if not rows:
            result["warnings"].append(_issue(None, "no_ocr_candidates", "No unambiguous labelled candidates found; original OCR evidence retained", "warning"))
    except subprocess.TimeoutExpired:
        result.update(ocr_status="timeout")
        result["warnings"].append(_issue(None, "ocr_timeout", "Local OCR timed out; no partial guesses returned", "warning"))
    except (OSError, RuntimeError, ValueError, InvalidOperation, csv.Error) as exc:
        result.update(rows=[], ocr_status="failed")
        result["warnings"].append(_issue(None, "ocr_failed", f"Local OCR output could not be used: {type(exc).__name__}", "warning"))
    return result
