"""Isolated transaction candidate for trade-derived holding snapshots.

Not available in runtime mode. A production path requires explicit coverage,
quote acquisition, replay handling and release review before enabling it.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from domain.imports.isolation import source_mode
from domain.investments.diluted_trade_positions import calculate_diluted_positions
from domain.investments.holding_evidence_writer import lock_holding_accounts, prepare_holding_change, finish_holding_change
from domain.investments.trade_snapshot_preview import build_trade_snapshot_candidates
from domain.investments.trade_snapshot_source import load_verified_equity_events
from domain.investments.valuation_input_evidence import _hash
from domain.imports.account_import_store import norm


def preview(cur, *, account_code, as_of, quote_ids, for_write=False):
    """Re-read verified trades and stored quote evidence in the caller transaction."""
    if not isinstance(as_of, date) or not isinstance(quote_ids, dict):
        raise ValueError('snapshot date and quote identities required')
    events, identities = load_verified_equity_events(cur, account_code, for_write=for_write)
    closed = []
    positions = calculate_diluted_positions(events, closed_adjustments=closed)
    symbols = {row['symbol'] for row in positions}
    if not symbols or set(quote_ids) != symbols or len(set(quote_ids.values())) != len(quote_ids):
        raise ValueError('quote identities must cover all open positions')
    quotes = {}
    for symbol in sorted(symbols):
        quote_id = quote_ids[symbol]
        if not isinstance(quote_id, int) or isinstance(quote_id, bool) or quote_id <= 0:
            raise ValueError('invalid quote identity')
        cur.execute('''SELECT quote_id,symbol,asset_id,currency,provider,source,as_of,price,price_time
                       FROM market_observations WHERE quote_id=%s AND observation_type='quote' ''', (quote_id,))
        row = cur.fetchone()
        if (not row or row['asset_id'] != identities[symbol]['asset_id'] or row['currency'] != 'CNY'
                or row['provider'] != 'tencent' or row['source'] != 'tencent_stock_quote'):
            raise ValueError('quote identity or provider mismatch')
        quotes[symbol] = {'symbol': row['symbol'], 'source': row['source'], 'price': row['price'],
                          'price_date': row['as_of'], 'price_time': row['price_time']}
    candidates = build_trade_snapshot_candidates(events=events, identities=identities, quotes=quotes,
                                                  as_of=as_of, closed_adjustments=[])
    digest = _hash(norm({'account_code': account_code, 'as_of': as_of, 'events': events,
                         'quote_ids': quote_ids, 'quotes': quotes, 'candidates': candidates,
                         'closed_adjustments': closed}))
    return {'candidates': candidates, 'closed_adjustments': closed, 'digest': digest}


def publish_isolated(cur, *, account_code, as_of, quote_ids, expected_digest):
    """Prototype only: atomic writes in explicitly marked disposable test DBs."""
    if source_mode() != 'test' or cur.connection.autocommit:
        raise ValueError('snapshot writer is restricted to isolated testing')
    lock_holding_accounts(cur, [account_code])
    prepared = preview(cur, account_code=account_code, as_of=as_of, quote_ids=quote_ids, for_write=True)
    if not isinstance(expected_digest, str) or expected_digest != prepared['digest']:
        raise ValueError('preview is stale')
    cur.execute('SELECT 1 FROM pams_holdings_snapshot WHERE account_code=%s LIMIT 1', (account_code,))
    if cur.fetchone():
        raise ValueError('existing account snapshot requires reconciliation')
    ids = []
    for row in prepared['candidates']:
        prepare_holding_change(cur, account_code=account_code, name=row['name'], as_of=as_of, source_item_id=None)
        normal_cost = row['remaining_cost'].quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)
        unit_cost = (normal_cost / row['quantity']).quantize(Decimal('0.000001'), rounding=ROUND_HALF_UP)
        cur.execute('''INSERT INTO pams_holdings_snapshot
            (account_code,symbol,name,currency,asset_liability,major_category,minor_category,as_of,
             market_value,cost,quantity,buy_price,current_price,asset_id)
            VALUES (%s,%s,%s,%s,'资产','A股','A股',%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
            (account_code,row['symbol'],row['name'],row['currency'],as_of,row['market_value'],normal_cost,
             row['quantity'],unit_cost,row['price'],row['asset_id']))
        holding_id = cur.fetchone()['id']
        refs = [{'kind': 'trade', 'key': {'trade_id': ident}, 'role': 'trade_input'} for ident in row['all_event_ids']]
        refs.append({'kind': 'quote', 'key': {'quote_id': quote_ids[row['symbol']]}, 'role': 'valuation_quote'})
        finish_holding_change(cur, holding_id=holding_id, source_item_id=None, input_refs=refs)
        ids.append(holding_id)
    return {'holding_ids': ids, 'digest': prepared['digest']}
