"""Tencent unadjusted A-share daily prices with conflict-safe provenance."""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re
from urllib.request import Request, urlopen

from psycopg2.extras import Json


SOURCE = 'tencent_cn_unadjusted_day_v1'
_SYMBOL = re.compile(r'^(?:sh|sz)\d{6}$')


def parse_history(payload, symbol, *, start, end):
    """Validate provider rows; retain raw evidence and never adjust close prices."""
    if not _SYMBOL.fullmatch(symbol) or not isinstance(start, date) or not isinstance(end, date) or start > end:
        raise ValueError('invalid history request')
    if not isinstance(payload, dict) or payload.get('code') != 0:
        raise ValueError('Tencent history request failed')
    instrument = payload.get('data', {}).get(symbol, {})
    raw_rows = instrument.get('day') if isinstance(instrument, dict) else None
    if not isinstance(raw_rows, list) or not raw_rows:
        raise ValueError('unadjusted daily prices unavailable')
    rows, seen = [], set()
    for raw in raw_rows:
        try:
            if not isinstance(raw, list) or len(raw) < 5:
                raise ValueError
            day = date.fromisoformat(raw[0])
            opening, close, high, low = (Decimal(str(value)) for value in raw[1:5])
            if (day in seen or day < start or day > end or
                    any(not v.is_finite() or v <= 0 for v in (opening, close, high, low)) or
                    not low <= min(opening, close) <= max(opening, close) <= high or
                    close != close.quantize(Decimal('0.0001')) or
                    any(v != v.quantize(Decimal('0.0000000001')) for v in (opening, high, low))):
                raise ValueError
        except (ValueError, TypeError, InvalidOperation) as exc:
            raise ValueError('invalid Tencent daily price') from exc
        seen.add(day)
        evidence = {'provider': 'tencent', 'basis': 'unadjusted', 'symbol': symbol,
                    'date': day.isoformat(), 'raw': raw}
        encoded = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
        rows.append({'symbol': symbol, 'date': day, 'open': opening, 'close': close, 'high': high,
                     'low': low, 'source_hash': sha256(encoded).hexdigest(), 'evidence': evidence})
    return sorted(rows, key=lambda row: row['date'])


def fetch_history(symbol, *, start, end):
    from domain.imports.isolation import require_external_access
    require_external_access()
    if not _SYMBOL.fullmatch(symbol) or not isinstance(start, date) or not isinstance(end, date) or start > end:
        raise ValueError('invalid history request')
    url = ('https://web.ifzq.gtimg.cn/appstock/app/kline/kline?param='
           f'{symbol},day,{start.isoformat()},{end.isoformat()},640')
    with urlopen(Request(url, headers={'User-Agent': 'PAMS/2.0'}), timeout=20) as response:
        return parse_history(json.load(response), symbol, start=start, end=end)


def publish_history(cur, rows):
    """Insert only missing equal-price observations; conflicting data aborts transaction."""
    if cur.connection.autocommit or not rows:
        raise ValueError('non-empty transactional history required')
    cur.execute("SELECT current_setting('transaction_isolation') AS level")
    if cur.fetchone()['level'] != 'serializable':
        raise ValueError('serializable transaction required')
    keys = [(row['symbol'], row['date']) for row in rows]
    if len(keys) != len(set(keys)):
        raise ValueError('duplicate history identity')
    for row in rows:
        symbol, day = row['symbol'], row['date']
        raw = row.get('evidence', {}).get('raw')
        valid = parse_history({'code': 0, 'data': {symbol: {'day': [raw]}}}, symbol, start=day, end=day)
        if valid != [row]:
            raise ValueError('history evidence changed')
    cur.execute("SELECT pg_advisory_xact_lock(hashtext('pams:tencent:history:v1'))")
    inserted = 0
    for row in rows:
        symbol, day = row['symbol'], row['date']
        if not _SYMBOL.fullmatch(symbol) or not isinstance(day, date):
            raise ValueError('invalid price identity')
        cur.execute('''INSERT INTO pams_ohlcv_daily(symbol,market,trade_date,close)
            VALUES (%s,'CN_A',%s,%s) ON CONFLICT (symbol,market,trade_date) DO NOTHING RETURNING symbol''',
            (symbol, day, row['close']))
        inserted += int(cur.fetchone() is not None)
        cur.execute("SELECT close FROM pams_ohlcv_daily WHERE symbol=%s AND market='CN_A' AND trade_date=%s FOR UPDATE", (symbol, day))
        current = cur.fetchone()
        if not current or current['close'] != row['close']:
            raise ValueError('existing daily close conflicts with Tencent evidence')
        cur.execute('''INSERT INTO market_observations
            (observation_type,as_of,symbol,market,currency,source,source_key,provider,confidence,
             open_price,high_price,low_price,close_price,source_hash,raw_json)
            VALUES ('daily',%s,%s,'CN_A','CNY',%s,%s,'tencent','medium',%s,%s,%s,%s,%s,%s)
            ON CONFLICT (as_of,source,source_key) WHERE observation_type='daily' DO NOTHING''',
            (day, symbol, SOURCE, symbol, row['open'], row['high'], row['low'], row['close'],
             row['source_hash'], Json(row['evidence'])))
        cur.execute('''SELECT source_hash,open_price,high_price,low_price,close_price,raw_json
            FROM market_observations WHERE observation_type='daily' AND as_of=%s AND source=%s AND source_key=%s FOR UPDATE''',
            (day, SOURCE, symbol))
        existing = cur.fetchone()
        if (not existing or existing['source_hash'] != row['source_hash'] or existing['raw_json'] != row['evidence']
                or any(existing[key + '_price'] != row[key] for key in ('open', 'high', 'low', 'close'))):
            raise ValueError('existing Tencent observation conflicts with source evidence')
    return {'rows': len(rows), 'inserted': inserted, 'reused': len(rows) - inserted}
