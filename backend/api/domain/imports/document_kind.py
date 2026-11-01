"""Conservative, read-only source-kind detection before creating a batch."""
from __future__ import annotations

from pathlib import PurePath
import re

from domain.imports import account_import_statement as statement
from domain.imports import account_import_ths as ths
from domain.imports.account_import_parse import HEADER_FIELDS, HEADER_SCAN, _csv, _text, _xlsx

INVESTMENT_PROFILES = {'tonghuashun', 'tencent_licaitong'}
IMAGES = {'.png', '.jpg', '.jpeg'}


def _text_kind(text: str) -> str | None:
    trade = bool(re.search(r'证券买入|证券卖出|股票买入|股票卖出|基金申购|基金赎回|交易记录|成交记录', text))
    holding = bool(re.search(r'持仓市值|持仓数量|持有份额|持仓明细|持仓快照', text))
    return 'trade' if trade and not holding else 'holding' if holding and not trade else None


def detect(content: bytes, filename: str, account: str) -> dict:
    suffix = PurePath(filename).suffix.lower()
    if statement.is_statement(account):
        if suffix not in {'.csv', '.tsv', '.xlsx', '.pdf', *IMAGES}:
            return {'kind': None, 'status': 'mismatch'}
        if suffix in IMAGES:
            return {'kind': 'cash', 'status': 'account_kind'}
        parsed = statement.parse(content, filename, account, 'cash')
        rows = parsed.get('rows') or []
        if not rows or any((row.get('fields') or {}).get('action') in
                           {'buy', 'sell', 'subscribe', 'redeem', 'dividend'} for row in rows):
            return {'kind': None, 'status': 'mismatch'}
        return {'kind': 'cash', 'status': 'detected'}
    if account not in INVESTMENT_PROFILES:
        raise ValueError('此账户不支持资料导入')
    if suffix in IMAGES:
        from domain.imports.account_import_ocr import parse_image
        image = parse_image(content, account, 'trade')
        kind = _text_kind(image.get('text') or '') if image.get('ocr_status') in {'ok', 'unrecognized'} else None
        return {'kind': kind, 'status': 'detected' if kind else 'unknown'}
    if suffix not in {'.xls', '.xlsx', '.csv', '.tsv'}:
        return {'kind': None, 'status': 'mismatch'}
    if suffix == '.xls' and (account != 'tonghuashun' or content.lstrip().startswith((b'\xd0\xcf\x11\xe0', b'PK', b'<'))):
        return {'kind': None, 'status': 'mismatch'}
    found = set()
    for _, table in (_xlsx(content) if suffix == '.xlsx' else _csv(content)):
        if not table:
            continue
        headers = max(([_text(v) for v in row] for row in table[:HEADER_SCAN]),
                      key=lambda row: sum(h in HEADER_FIELDS for h in row), default=[])
        if ths.matches(headers):
            if account != 'tonghuashun':
                return {'kind': None, 'status': 'mismatch'}
            found.add('trade')
            continue
        if suffix == '.xls':
            return {'kind': None, 'status': 'mismatch'}
        fields = {HEADER_FIELDS[h] for h in headers if h in HEADER_FIELDS}
        identity = bool(fields & {'symbol', 'name'})
        if identity and {'business_date', 'action'} <= fields and fields & {'gross_amount', 'net_cash', 'quantity'}:
            found.add('trade')
        if identity and {'quantity', 'market_value'} <= fields:
            found.add('holding')
    return {'kind': next(iter(found)), 'status': 'detected'} if len(found) == 1 else {'kind': None, 'status': 'unknown'}
