"""Isolated candidate for preserving time-stamped Tencent quote evidence."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from domain.imports.isolation import source_enabled, source_mode, runtime_import_active
from domain.imports.native_source import enabled as publication_enabled
from domain.investments.daily_return_state import quote_snapshot_from_market_daily, upsert_quote_snapshot


def stage_isolated_quotes(cur, *, as_of, identities, quotes, allow_runtime=False):
    """Record observations without claiming that an observed price is a close."""
    permitted = source_mode() == 'test' or (allow_runtime and runtime_import_active() and publication_enabled())
    if not permitted or cur.connection.autocommit or not source_enabled(cur):
        raise ValueError('quote staging is restricted to isolated testing')
    if not isinstance(as_of, date) or isinstance(as_of, datetime) or not quotes or set(quotes) != set(identities):
        raise ValueError('quote coverage mismatch')
    ids = {}
    for symbol in sorted(quotes):
        identity, quote = identities[symbol], quotes[symbol]
        stamp, price, provider_symbol = quote.get('price_time'), quote.get('price'), quote.get('symbol')
        if (identity.get('symbol') != symbol or not identity.get('asset_id') or not identity.get('name')
                or not isinstance(provider_symbol, str) or provider_symbol[2:] != symbol
                or provider_symbol[:2] not in {'sh', 'sz', 'bj'} or quote.get('source') != 'tencent'
                or not isinstance(stamp, datetime) or stamp.utcoffset() is None
                or stamp.astimezone(ZoneInfo('Asia/Shanghai')).date() != as_of
                or quote.get('price_date') != as_of or not isinstance(price, Decimal)
                or not price.is_finite() or price <= 0):
            raise ValueError('invalid quote observation')
        key = f"tencent_stock_quote:{provider_symbol}:{stamp.isoformat()}"
        observation = quote_snapshot_from_market_daily({
            'as_of': as_of, 'asset_id': identity['asset_id'], 'symbol': provider_symbol,
            'name': identity['name'], 'market': 'CN_A', 'currency': 'CNY', 'provider': 'tencent',
            'source': 'tencent_stock_quote', 'source_key': key, 'close_price': price,
            'price_time': stamp, 'return_status': 'pending', 'raw_json': {'price_kind': 'observed'},
        })
        cur.execute('''SELECT * FROM market_observations WHERE observation_type='quote'
            AND as_of=%s AND provider=%s AND source_key=%s AND quote_session=%s FOR SHARE''',
            (as_of, observation['provider'], key, observation['quote_session']))
        previous = cur.fetchone()
        if previous:
            if previous['source_hash'] != observation['source_hash']:
                raise ValueError('existing quote evidence differs')
            ids[symbol] = previous['quote_id']
            continue
        stored = upsert_quote_snapshot(cur, observation)
        ids[symbol] = stored['quote_id']
    return ids
