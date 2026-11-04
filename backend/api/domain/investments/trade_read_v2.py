"""Read investment facts through the redesigned source evidence chain."""
from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json

from domain.imports.ths_asset_candidates import MARKETS, PROFILE


def _asset_matches(row, fields):
    if fields.get('asset_id') == row['asset_id']:
        return True
    raw = row.get('raw_fields') or {}
    source = raw.get('source') or {}
    original = raw.get('initial_fields') or {}
    exchange = MARKETS.get(source.get('market'))
    asset = row.get('asset_identity') or {}
    return (not fields.get('asset_id') and row['account_code'] == 'tonghuashun'
            and row.get('acquisition_method') == 'file' and row.get('parse_method') == 'template'
            and row.get('content') is not None and source.get('profile') == PROFILE
            and row['trade_type'] in {'buy', 'sell', 'dividend', 'tax'} and exchange is not None
            and original.get('symbol') == fields.get('symbol') == asset.get('primary_symbol')
            and asset.get('exchange') == exchange and asset.get('status') == 'active'
            and asset.get('currency') == row['currency'] == 'CNY'
            and asset.get('asset_type') in {'stock', 'A股个股'})


def _matches(row: dict) -> bool:
    fields = row['parsed_fields'] or {}
    try:
        action = fields.get('trade_type') or fields.get('action')
        action = {'subscribe': 'fund_subscription', 'redeem': 'fund_redemption'}.get(action, action)
        business_date = date.fromisoformat(fields['business_date'])
        trade_time = datetime.combine(business_date, time.fromisoformat(fields['trade_time'])) if fields.get('trade_time') else None
        settle_date = date.fromisoformat(fields['settle_date']) if fields.get('settle_date') else None
        numeric = ('quantity', 'price', 'gross_amount', 'fee_amount', 'tax_amount', 'net_cash')
        expected = {key: Decimal(str(fields[key])) if fields.get(key) not in (None, '') else None for key in numeric}
        side = fields.get('side') or (action if action in {'buy', 'sell', 'deposit', 'withdraw'} else None)
        return (business_date == row['trade_date'] and trade_time == row['trade_time'] and settle_date == row['settle_date']
                and action == row['trade_type'] and side == row['side'] and fields.get('currency') == row['currency']
                and all(expected[key] == row[key] for key in numeric)
                and _asset_matches(row, fields)
                and all(fields.get(key) == row[key] for key in
                        ('symbol', 'name', 'market', 'fee_currency', 'broker', 'portfolio_code', 'execution_status')))
    except (ValueError, TypeError, KeyError, InvalidOperation):
        return False


def load_trade_rows(cur, *, account_code: str, start: date, end: date, fact_id: int | None = None,
                    source_id=None) -> list[dict]:
    """Return source status per fact; caller must not calculate from unverified facts."""
    fact_filter = ' AND f.trade_id=%s' if fact_id is not None else ''
    params = (account_code, start, end) if fact_id is None else (account_code, start, end, fact_id)
    if source_id is not None:
        fact_filter += ' AND r.source_id=%s'
        params += (str(source_id),)
    cur.execute('''SELECT f.*, i.raw_fields, i.parsed_fields, i.record_hash,
        i.review_status AS item_review, i.source_uid AS item_source_uid,
        r.source_name,r.acquisition_method,r.file_format, r.status AS source_status,
        r.account_code AS source_account, r.content, r.sha256, i.parse_method,
        json_build_object('primary_symbol', a.primary_symbol, 'exchange', a.exchange,
                          'status', a.status, 'currency', a.currency, 'asset_type', a.asset_type) AS asset_identity
        FROM public.pams_investment_trade_fact f
        JOIN public.source_items i ON i.source_item_id=f.source_item_id
        JOIN public.source_records r ON r.source_id=i.source_id
        LEFT JOIN public.pams_asset_master a ON a.asset_id=f.asset_id
        WHERE f.account_code=%s AND f.trade_date BETWEEN %s AND %s''' + fact_filter +
        ' ORDER BY f.trade_date,f.trade_time NULLS FIRST,f.trade_id', params)
    columns = [column[0] for column in cur.description]
    out = []
    ibkr_cache = {}
    for value in cur.fetchall():
        row = dict(value) if hasattr(value, 'keys') else dict(zip(columns, value))
        raw = json.dumps(row['raw_fields'], ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), allow_nan=False).encode('utf-8')
        content, digest = row['content'], row['sha256']
        verified = (row['source_status'] == 'published' and row['item_review'] == 'confirmed'
                    and row['source_account'] == row['account_code'] and _matches(row)
                    and sha256(raw).hexdigest() == row['record_hash']
                    and ((content is None and digest is None) or
                         (content is not None and sha256(bytes(content)).hexdigest() == digest)))
        ibkr_report = ((row.get('raw_fields') or {}).get('provider') == 'ibkr_flex' or
                       (row.get('source_name') == 'ibkr' and row.get('acquisition_method') == 'api'
                        and row.get('file_format') == 'xml'))
        if verified and ibkr_report:
            from domain.brokers.ibkr.ibkr_source_verify_v2 import verify_trade_original
            verified = verify_trade_original(cur, row, ibkr_cache)
        row['source_evidence_status'] = 'verified' if verified else 'unknown'
        row['fee_scope'] = (row['parsed_fields'] or {}).get('fee_scope')
        origin = row['raw_fields'].get('source') if isinstance(row['raw_fields'], dict) else None
        row['source_market'] = origin.get('market') if isinstance(origin, dict) else None
        for key in ('raw_fields', 'parsed_fields', 'record_hash', 'item_review', 'source_status',
                    'source_account', 'content', 'sha256', 'item_source_uid', 'source_name',
                    'acquisition_method', 'file_format', 'parse_method', 'asset_identity'):
            row.pop(key, None)
        out.append(row)
    return out


def _normalized_symbol(value):
    symbol = str(value or '').strip()
    if symbol.upper().startswith(('SSE:', 'SZSE:')):
        return symbol.split(':', 1)[1]
    if symbol.lower().startswith(('sh', 'sz', 'bj')) and len(symbol) >= 8:
        return symbol[2:]
    return symbol


def same_day_trade_groups(cur, *, keys, as_of):
    grouped = {}
    unverified = set()
    allowed = {'buy', 'sell', 'fund_subscription', 'fund_redemption'}
    for account in sorted({key[0] for key in keys}):
        for row in load_trade_rows(cur, account_code=account, start=as_of, end=as_of):
            key = (account, _normalized_symbol(row.get('symbol')))
            if key not in keys or row['trade_type'] not in allowed:
                continue
            if row['source_evidence_status'] != 'verified' or row['quantity'] is None or row['quantity'] <= 0:
                unverified.add(key)
                continue
            grouped.setdefault(key, []).append(row)
    for key in unverified:
        grouped.pop(key, None)
    return grouped, unverified


def prior_trade_quantities(cur, *, keys, as_of):
    from datetime import timedelta
    quantities = {}
    coverage_unknown = set()
    if as_of == date.min:
        return quantities, coverage_unknown
    for account in sorted({key[0] for key in keys}):
        for row in load_trade_rows(cur, account_code=account, start=date.min, end=as_of - timedelta(days=1)):
            key = (account, _normalized_symbol(row.get('symbol')))
            if key not in keys or row['trade_type'] not in {'buy', 'sell', 'fund_subscription', 'fund_redemption'}:
                continue
            # Individual source facts cannot establish all preceding positions.
            coverage_unknown.add(key)
            if row['source_evidence_status'] != 'verified' or row['quantity'] is None or row['quantity'] <= 0:
                continue
            sign = 1 if row['trade_type'] in {'buy', 'fund_subscription'} else -1
            quantities[key] = quantities.get(key, Decimal(0)) + sign * row['quantity']
    return quantities, coverage_unknown


def calculate_verified_positions(cur, *, account_code: str, start: date, end: date,
                                 coverage_confirmed: bool) -> list[dict]:
    """Reconstruct positions only when the caller has confirmed full coverage."""
    if coverage_confirmed is not True:
        raise ValueError('complete trade coverage must be explicitly confirmed')
    from domain.investments.diluted_trade_positions import calculate_diluted_positions
    rows = load_trade_rows(cur, account_code=account_code, start=start, end=end)
    if any(row['source_evidence_status'] != 'verified' for row in rows):
        raise ValueError('trade source evidence is incomplete')
    security_rows = []
    for row in rows:
        if row['trade_type'] in {'deposit', 'withdraw', 'interest'} and row.get('symbol') in (None, '', 'CASH_CNY'):
            continue
        row['side'] = row['side'] or row['trade_type']
        security_rows.append(row)
    return calculate_diluted_positions(security_rows)
