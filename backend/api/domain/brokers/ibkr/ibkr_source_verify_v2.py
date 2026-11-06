"""Verify redesigned IBKR trade facts against the original Flex report."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation

from domain.brokers.ibkr.ibkr_daily_close import _document
from domain.brokers.ibkr.ibkr_cash_event_sync import _cash_scope_ok
from domain.brokers.ibkr.ibkr_report_trade_source import _row


def verify_trade_original(cur, row, cache):
    raw = row.get('raw_fields') or {}
    fields = row.get('parsed_fields') or {}
    content, digest = row.get('content'), row.get('sha256')
    account = raw.get('source_account_id')
    if (raw.get('provider') != 'ibkr_flex' or not content or not digest or
            raw.get('report_sha256') != digest or not isinstance(account, str) or not account):
        return False
    key = (str(digest), account)
    if key not in cache:
        try:
            evidence, _, error = _document(bytes(content).decode('utf-8'), account, digest)
            cache[key] = None if error else evidence
        except (UnicodeError, ValueError, TypeError):
            cache[key] = None
    evidence = cache[key]
    if evidence is None:
        return False
    day = row['trade_date']
    statements = evidence.by_day.get(day, [])
    if len(statements) != 1:
        return False
    official = raw.get('official_attributes')
    if not isinstance(official, dict):
        return False
    trade_date = str(official.get('tradeDate') or '').strip()
    parsed_day = None
    for fmt in ('%Y%m%d', '%Y-%m-%d', '%Y/%m/%d', '%d %B, %Y', '%d %b, %Y', '%B %d, %Y', '%b %d, %Y'):
        try:
            parsed_day = datetime.strptime(trade_date, fmt).date()
            break
        except ValueError:
            continue
    if parsed_day != day:
        return False
    identifier = next(((key, str(official[key]).strip()) for key in ('tradeID', 'transactionID', 'ibExecID')
                       if official.get(key)), None)
    if not identifier:
        return False
    nodes = [node for node in statements[0].iter() if node.tag.rsplit('}', 1)[-1] == 'Trade']
    matching = [node for node in nodes if node.get(identifier[0]) == identifier[1]]
    if (len(matching) != 1 or not _cash_scope_ok(matching[0], evidence.parents, account) or
            {str(k): str(v).strip() for k, v in matching[0].attrib.items()} != official):
        return False
    try:
        if row.get('source_name') == 'ibkr_historical_flex':
            from domain.brokers.ibkr.ibkr_history_backfill import _trade
            uid, expected = _trade(official, account, day, digest)
            return uid == row.get('item_source_uid') and expected == fields
        normalized = _row(cur, {'raw': official}, account, day, digest, include_fx=False)
        if normalized['source_key'] != row.get('item_source_uid'):
            return False
        text_fields = {'business_date': day.isoformat(), 'broker': 'ibkr',
                       'trade_type': normalized['side'].lower(), 'side': normalized['side'].lower(),
                       'symbol': normalized['symbol'], 'name': normalized['name'],
                       'market': normalized['market'], 'currency': normalized['currency'],
                       'fee_currency': normalized['explicit_fee_currency']}
        if any(fields.get(name) != value for name, value in text_fields.items()):
            return False
        numeric = ('quantity', 'price', 'gross_amount', 'fee_amount', 'tax_amount', 'net_cash')
        for name in numeric:
            expected = normalized[name]
            if name == 'quantity':
                expected = abs(expected)
            actual = fields.get(name)
            if (actual is None) != (expected is None):
                return False
            if actual is not None and Decimal(str(actual)) != expected:
                return False
        return True
    except (KeyError, ValueError, TypeError, InvalidOperation):
        return False
