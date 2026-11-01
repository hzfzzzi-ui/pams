"""Prepare identity candidates from an immutable Tonghuashun source document.

This module only proposes identities. Callers decide how to store and review
them; no ticker prefix or transaction amount implies a security type.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date

from psycopg2.extras import Json

from domain.assets.asset_master import asset_id_for

PROFILE = 'ths_broker_ledger_v1'
MARKETS = {'上海Ａ股': 'SSE', '深圳Ａ股': 'SZSE'}


def collect_candidates(parsed: dict) -> tuple[list[dict], list[dict]]:
    """Group source-backed security identities; keep ambiguous groups unresolved."""
    groups: dict[str, list[dict]] = defaultdict(list)
    invalid: set[str] = set()
    for row in parsed.get('rows', []):
        fields, source = row.get('fields') or {}, row.get('source') or {}
        symbol = str(fields.get('symbol') or '').strip()
        if source.get('profile') != PROFILE or not re.fullmatch(r'\d{6}', symbol):
            continue
        name = str(fields.get('name') or '').strip()
        market = source.get('market')
        try:
            day = date.fromisoformat(fields.get('business_date') or '').isoformat()
        except ValueError:
            invalid.add(symbol)
            continue
        if not name or market not in MARKETS or not isinstance(source.get('row'), int):
            invalid.add(symbol)
            continue
        groups[symbol].append({'name': name, 'market': market, 'date': day, 'row': source['row']})

    candidates, conflicts = [], []
    for symbol in sorted(groups.keys() | invalid):
        records = groups[symbol]
        if symbol in invalid:
            conflicts.append({'symbol': symbol, 'reason': 'source_incomplete'})
            continue
        markets = {record['market'] for record in records}
        # XD/XR/DR prefixes denote broker display variants, not new identities.
        ordinary = [r for r in records if not r['name'].upper().startswith(('XD', 'XR', 'DR'))]
        pool = ordinary or records
        last_day = max(record['date'] for record in pool)
        names = {record['name'] for record in pool if record['date'] == last_day}
        if len(markets) != 1 or len(names) != 1:
            conflicts.append({'symbol': symbol, 'reason': 'identity_ambiguous'})
            continue
        candidates.append({'symbol': symbol, 'name': names.pop(), 'market': next(iter(markets)),
                           'exchange': MARKETS[next(iter(markets))], 'source_records': records})
    return candidates, conflicts


def initialize(cur, parsed: dict, *, batch_id, file_id, file_sha256: str) -> dict:
    """Create missing source-backed identities in the caller's upload transaction."""
    candidates, conflicts = collect_candidates(parsed)
    created = []
    if candidates:
        # Symbol and alias are not globally unique; exclude concurrent identity
        # writers while checking both tables and inserting the missing records.
        cur.execute('LOCK TABLE pams_asset_master, pams_asset_alias IN SHARE ROW EXCLUSIVE MODE')
    for candidate in candidates:
        symbol = candidate['symbol']
        asset_id = asset_id_for(symbol, candidate['name'], 'A股个股')
        # Coordinate with the asset-master synchronizer and serialize same-ID uploads.
        cur.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',
                    ('pams:asset-identity:' + asset_id,))
        identities = [symbol, symbol + '.SH', symbol + '.SS', symbol + '.SZ', 'SH' + symbol, 'SZ' + symbol]
        cur.execute('''SELECT DISTINCT m.asset_id,m.primary_symbol,m.exchange
            FROM pams_asset_master m LEFT JOIN pams_asset_alias a ON a.asset_id=m.asset_id
            WHERE m.asset_id=%s OR upper(m.primary_symbol)=ANY(%s) OR upper(a.alias_value)=ANY(%s)''',
                    (asset_id, identities, identities))
        existing = cur.fetchall()
        if existing:
            # Never alter an existing identity or turn a conflict into a duplicate.
            if len(existing) != 1:
                conflicts.append({'symbol': symbol, 'reason': 'existing_identity_ambiguous'})
            continue
        evidence = {'source_batch_id': str(batch_id), 'source_file_id': str(file_id),
                    'source_file_sha256': file_sha256, 'source_profile': PROFILE,
                    'source_records': candidate['source_records'],
                    'pending_fields': ['asset_type', 'currency'],
                    'type_basis': '同花顺账户默认个股，待核对',
                    'currency_basis': '导入账户默认人民币，待核对'}
        cur.execute('''INSERT INTO pams_asset_master
            (asset_id,primary_symbol,canonical_name,display_name,exchange,asset_type,market,currency,
             status,source,confidence,raw_json)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'active','ths_import','low',%s)''',
            (asset_id, symbol, candidate['name'], candidate['name'], candidate['exchange'],
             'A股个股', 'A股', 'CNY', Json(evidence)))
        created.append(symbol)
    return {'created': created, 'conflicts': conflicts}
