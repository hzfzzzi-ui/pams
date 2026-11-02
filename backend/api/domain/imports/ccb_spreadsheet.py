"""Bounded CCB statement readers; return source rows for review, never facts."""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from domain.imports.account_import_parse import MAX_CELLS, MAX_INPUT_BYTES, MAX_ROWS, MAX_SHEETS, _csv

_DEBIT = ("序号", "摘要", "币别", "钞汇", "交易日期", "交易金额", "账户余额", "交易地点/附言", "对方账号与户名")
_CREDIT = ("交易日", "入账日", "信用卡卡号", "类型", "入账币种", "入账金额", "交易描述")


def _amount(value):
    text = str(value).strip().replace(",", "")
    if not re.fullmatch(r"[+-]?\d+(?:\.\d{1,4})?", text):
        raise ValueError("建行账单金额无效，不能导入")
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError("建行账单金额无效，不能导入") from exc
    if not amount.is_finite() or abs(amount) > Decimal("999999999999999"):
        raise ValueError("建行账单金额超限")
    return amount


def _date(value):
    try:
        return datetime.strptime(str(value).strip(), "%Y%m%d").date().isoformat()
    except ValueError as exc:
        raise ValueError("建行账单交易日期无效") from exc


def _tail(account):
    match = re.search(r"(\d{4})$", account)
    if not match:
        raise ValueError("建行账户缺少可核对的尾号")
    return match.group(1)


def _row(fields, cells, index, template):
    return {"fields": fields, "source": {"row": index, "statement": True,
        "statement_row": {"transaction_date": fields["business_date"], "amount": fields["net_cash"],
            "currency": fields["currency"], "summary": fields["note"],
            "raw": {"row": cells, "statement_template": template}}}, "issues": []}


def parse(content: bytes, filename: str, account: str, template: str) -> dict | None:
    if not content or len(content) > MAX_INPUT_BYTES:
        raise ValueError("文件为空或超过大小上限")
    suffix = filename.lower().rsplit(".", 1)[-1]
    if template == "ccb_debit_pdf" and suffix == "xls":
        if not content.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            raise ValueError("建行 XLS 文件签名无效")
        import xlrd
        try:
            book = xlrd.open_workbook(file_contents=content, on_demand=True)
            if book.nsheets != 1 or book.nsheets > MAX_SHEETS:
                raise ValueError("建行账单应包含一个工作表")
            sheet = book.sheet_by_index(0)
            if sheet.nrows > MAX_ROWS + 20 or sheet.nrows * sheet.ncols > MAX_CELLS:
                raise ValueError("建行账单超过行数或单元格上限")
            table = [[str(v).strip() for v in sheet.row_values(i)] for i in range(sheet.nrows)]
        except (xlrd.XLRDError, IndexError) as exc:
            raise ValueError("建行 XLS 无法解析") from exc
        if len(table) < 4 or tuple(table[3][:9]) != _DEBIT or "个人活期账户全部交易明细" not in "".join(table[0]):
            raise ValueError("文件与建行储蓄卡交易模板不匹配")
        identity = "".join(table[1])
        match = re.search(r"卡号/账号\s*[:：]\s*(\d+)", identity)
        if not match or not match.group(1).endswith(_tail(account)):
            raise ValueError("账单账户与所选储蓄卡不一致")
        result = []
        for i, cells in enumerate(table[4:], 5):
            if not any(cells):
                continue
            if len(cells) < 9 or cells[2] != "人民币元":
                raise ValueError("建行储蓄卡账单行或币种不支持")
            amount = _amount(cells[5]); day = _date(cells[4])
            fields = {"kind":"cash", "asset_type":"cash", "business_date":day, "currency":"CNY",
                "net_cash":str(amount), "action":"income" if amount >= 0 else "expense",
                "note":" ".join(x for x in (cells[1], cells[7], cells[8]) if x)}
            result.append(_row(fields, cells, i, "ccb_debit_xls"))
    elif template == "ccb_credit_pdf" and suffix == "csv":
        table = _csv(content)[0][1]
        matches = [i for i, row in enumerate(table[:20]) if tuple(x.strip() for x in row[:7]) == _CREDIT]
        title = "中国建设银行信用卡交易明细" in "".join("".join(r) for r in table[:20])
        if not title and not matches:
            return None
        if len(matches) != 1 or not title:
            raise ValueError("文件与建行信用卡交易模板不匹配")
        result = []
        for i, original in enumerate(table[matches[0]+1:], matches[0]+2):
            cells = [x.strip() for x in original]
            if not any(cells):
                continue
            if len(cells) < 7 or not cells[2].endswith(_tail(account)) or cells[4] != "人民币":
                raise ValueError("信用卡账单行、账户或币种不匹配")
            if cells[3] not in {"消费", "利息", "存入", "退货退税", "取现", "费用"}:
                raise ValueError("信用卡交易类型未识别，不能推断方向")
            day = _date(cells[0]); _date(cells[1]); amount = -_amount(cells[5])
            action = "transfer" if cells[3] in {"存入", "取现"} else ("income" if amount >= 0 else "expense")
            fields = {"kind":"cash", "asset_type":"cash", "business_date":day, "currency":"CNY",
                "net_cash":str(amount), "action":action,
                "note":" ".join((cells[3], ",".join(cells[6:])))}
            record = _row(fields, cells, i, "ccb_credit_csv")
            record["source"]["statement_row"]["posting_date"] = _date(cells[1])
            result.append(record)
    else:
        return None
    if not result or len(result) > MAX_ROWS:
        raise ValueError("建行账单无有效交易或超过记录上限")
    return {"rows":result, "headers":[], "suggested_mapping":{}, "warnings":[{"code":"partial_statement",
        "severity":"warning", "message":"已识别建行交易明细；请核对账户、交易方向和转账关联，不能据此自动认定资金范围完整。"}]}
