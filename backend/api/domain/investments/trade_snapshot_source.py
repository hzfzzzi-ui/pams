"""Read source-verified Tonghuashun equity events for a snapshot preview."""
from __future__ import annotations

import re
from datetime import date

from domain.imports.isolation import source_enabled
from domain.imports.source_fact_evidence import NativeFactVerifier


def load_verified_equity_events(cur, account_code, *, for_write=False):
    """Read source facts consistently without claiming completeness."""
    if account_code != 'tonghuashun' or cur.connection.autocommit or not source_enabled(cur):
        raise ValueError('unsupported or unguarded snapshot source')
    cur.execute("SELECT current_setting('transaction_read_only') AS ro, current_setting('transaction_isolation') AS level")
    mode = cur.fetchone()
    if for_write:
        from domain.investments.holding_evidence_writer import assert_holding_accounts_locked
        if mode['ro'] != 'off' or mode['level'] != 'serializable':
            raise ValueError('serializable writable transaction required')
        assert_holding_accounts_locked(cur, [account_code])
    elif mode['ro'] != 'on' or mode['level'] not in {'repeatable read', 'serializable'}:
        raise ValueError('consistent read-only transaction required')
    cur.execute("SELECT to_regclass('public.source_records') IS NOT NULL AS redesigned")
    redesigned = cur.fetchone()['redesigned']
    if redesigned:
        from domain.investments.trade_read_v2 import load_trade_rows
        rows = load_trade_rows(cur, account_code=account_code, start=date.min, end=date.max)
    else:
        cur.execute('''SELECT trade_id,account_code,symbol,currency,trade_date,trade_time,side,
                          quantity,net_cash,asset_id,source_table,review_status,affects_position
                   FROM pams_investment_trade_fact WHERE account_code=%s ORDER BY trade_date,trade_time,trade_id''',
                    (account_code,))
        rows = [dict(row) for row in cur.fetchall()]
    events = []
    for row in rows:
        symbol = str(row.get('symbol') or '')
        side = str(row.get('side') or (row.get('trade_type') if redesigned else '') or '').lower()
        if symbol == 'CASH_CNY' and (redesigned or not row.get('affects_position')):
            continue
        if not re.fullmatch(r'\d{6}', symbol) or side not in {'buy', 'sell', 'dividend', 'tax'}:
            raise ValueError('unsupported or unidentified account event')
        if redesigned and row.get('source_evidence_status') != 'verified':
            raise ValueError('trade source verification failed')
        if not redesigned and (row.get('source_table') != 'source_items' or row.get('review_status') not in {'auto', 'reviewed', 'confirmed'}):
            raise ValueError('trade source or review status unsupported')
        row['side'] = side
        events.append(row)
    if not events:
        raise ValueError('no verified stock events')
    if not redesigned:
        verified = NativeFactVerifier(cur).verify_many(table='pams_investment_trade_fact',
                                                        fact_ids=[row['trade_id'] for row in events])
        if any(verified.get(row['trade_id'], {}).get('status') != 'verified' for row in events):
            raise ValueError('trade source verification failed')
    identity_by_symbol = {}
    for row in events:
        symbol, asset_id = row['symbol'], row.get('asset_id')
        if not asset_id or (symbol in identity_by_symbol and identity_by_symbol[symbol]['asset_id'] != asset_id):
            raise ValueError('ambiguous asset identity')
        if symbol not in identity_by_symbol:
            cur.execute('''SELECT asset_id,primary_symbol,canonical_name,asset_type,status,currency
                           FROM pams_asset_master WHERE asset_id=%s''', (asset_id,))
            asset = cur.fetchone()
            if (not asset or asset['primary_symbol'] != symbol or asset['status'] != 'active'
                    or asset['currency'] != 'CNY' or asset['asset_type'] not in {'stock', 'A股个股'}):
                raise ValueError('asset identity is not an active CNY stock')
            identity_by_symbol[symbol] = {'asset_id': asset_id, 'symbol': symbol, 'name': asset['canonical_name']}
        row['source_verified'] = True
    return events, identity_by_symbol
