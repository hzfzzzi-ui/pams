"""Review provisional identities created from an account-import source."""
from __future__ import annotations

from psycopg2.extras import Json

SOURCES = ('ths_import', 'broker_statement')
TYPES = {'A股个股', 'ETF / 基金'}


def pending(cur):
    cur.execute('''SELECT asset_id,primary_symbol,canonical_name,asset_type,currency,market,exchange,source,raw_json,updated_at
        FROM pams_asset_master WHERE source = ANY(%s) AND status='active'
          AND COALESCE(raw_json->>'import_identity_reviewed','false') <> 'true'
        ORDER BY primary_symbol,asset_id LIMIT 1000''', (list(SOURCES),))
    return [{'asset_id': row['asset_id'], 'symbol': row['primary_symbol'],
             'name': row['canonical_name'], 'asset_type': row['asset_type'],
             'currency': row['currency'], 'market': row['market'], 'exchange': row['exchange'],
             'source_date': max((r.get('date', '') for r in (row['raw_json'] or {}).get('source_records', [])), default=''),
             'updated_at': row['updated_at'].isoformat(),
             'pending_fields': ['资产类型', '币种']} for row in cur.fetchall()]


def confirm(cur, *, asset_id: str, expected_updated_at: str, asset_type: str, currency: str):
    if asset_type not in TYPES or currency != 'CNY':
        raise ValueError('请选择支持的资产类型，并核对人民币币种')
    cur.execute('''SELECT asset_type,currency,raw_json,updated_at,source FROM pams_asset_master
        WHERE asset_id=%s AND source = ANY(%s) AND status='active' FOR UPDATE''', (asset_id, list(SOURCES)))
    row = cur.fetchone()
    if not row or row['updated_at'].isoformat() != expected_updated_at:
        raise ValueError('资产已变化，请刷新后重新核对')
    metadata = dict(row['raw_json'] or {})
    if metadata.get('import_identity_reviewed'):
        raise ValueError('资产已经核对，请刷新后查看')
    metadata['import_identity_reviewed'] = True
    metadata['import_identity_review'] = {'previous_asset_type': row['asset_type'],
                                          'previous_currency': row['currency'],
                                          'asset_type': asset_type, 'currency': currency,
                                          'basis': 'user_confirmation', 'previous_source': row['source']}
    metadata['pending_fields'] = []
    cur.execute('''UPDATE pams_asset_master SET asset_type=%s,currency=%s,raw_json=%s,source='manual',
        updated_at=now() WHERE asset_id=%s''', (asset_type, currency, Json(metadata), asset_id))
