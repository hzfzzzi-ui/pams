"""Stage validated IBKR Flex trade evidence in the redesigned source model.

This adapter only creates a draft. It does not update holdings, cash, or NAV.
"""
from __future__ import annotations

from hashlib import sha256
from uuid import uuid4
from xml.etree.ElementTree import ParseError

from collectors.ibkr_flex import IBKRFlexError, parse_flex_report, _validated_isolated_report_trades
from domain.brokers.ibkr.ibkr_daily_close import _document
from domain.brokers.ibkr.ibkr_report_state import ReportRejected, inspect_report
from domain.brokers.ibkr.ibkr_report_trade_source import _row
from domain.imports.source_records_v2 import add_item, create_source


def stage_trade_report(cur, *, report_bytes: bytes, expected_source_account: str) -> dict:
    if cur.connection.autocommit:
        raise ValueError('IBKR staging requires a transaction')
    if not isinstance(report_bytes, bytes) or not 0 < len(report_bytes) <= 32 * 1024 * 1024:
        raise ValueError('invalid IBKR report size')
    if not isinstance(expected_source_account, str) or not expected_source_account.strip():
        raise ValueError('explicit IBKR source account is required')
    try:
        text = report_bytes.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise ValueError('IBKR report must be UTF-8') from exc
    digest = sha256(report_bytes).hexdigest()
    _, _, error = _document(text, expected_source_account, digest)
    if error:
        raise ValueError('invalid IBKR report')
    try:
        day = inspect_report(text)
        parsed = parse_flex_report(text, account_code='ibkr', allow_empty_holdings=True)
    except (ParseError, IBKRFlexError, ReportRejected) as exc:
        raise ValueError('invalid IBKR report') from exc
    official, trades, stats = _validated_isolated_report_trades(parsed, day, 'ibkr', text, digest)
    if official != expected_source_account:
        raise ValueError('IBKR source account does not match')
    if not trades:
        raise ValueError('no current-day IBKR trades')
    cur.execute("SELECT pg_advisory_xact_lock(hashtextextended('account-import:ibkr',0))")
    cur.execute("""SELECT 1 FROM public.source_records WHERE account_code='ibkr' AND source_name='ibkr'
        AND sha256=%s AND status IN ('draft','published') LIMIT 1""", (digest,))
    if cur.fetchone():
        raise ValueError('same IBKR report already exists')
    normalized = []
    for trade in trades:
        item = _row(cur, trade, official, day, digest)
        if str(item.get('market') or '').upper() == 'CASH':
            raise ValueError('IBKR currency conversion requires separate handling')
        side = str(item['side']).lower()
        fields = {'business_date': day.isoformat(), 'broker': 'ibkr', 'trade_type': side,
            'side': side, 'symbol': item['symbol'], 'name': item['name'], 'market': item['market'],
            'currency': item['currency'], 'quantity': str(abs(item['quantity'])), 'price': str(item['price']),
            'gross_amount': str(item['gross_amount']), 'fee_amount': str(item['fee_amount']),
            'fee_currency': item['explicit_fee_currency'],
            'tax_amount': str(item['tax_amount']) if item['tax_amount'] is not None else None,
            'net_cash': str(item['net_cash'])}
        raw = {'provider': 'ibkr_flex', 'source_account_id': official, 'report_sha256': digest,
               'official_attributes': trade['raw']}
        normalized.append((item['source_key'], fields, raw))
    source_id = uuid4()
    create_source(cur, source_id=source_id, account_code='ibkr', source_name='ibkr', acquisition_method='api',
                  document_type='trade', filename='ibkr-flex.xml', file_format='xml',
                  content_type='application/xml', content=report_bytes)
    for index, (uid, fields, raw) in enumerate(normalized, 1):
        add_item(cur, source_id=source_id, item_id=uuid4(), parse_method='api', source_uid=uid,
                 source_position={'trade_ordinal': index}, raw_fields=raw, parsed_fields=fields)
    return {'source_id': str(source_id), 'revision': len(normalized) + 1, 'row_count': len(normalized),
            'report_date': day.isoformat(), 'report_sha256': digest, **stats}
