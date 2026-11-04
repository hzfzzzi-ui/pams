"""Link one published Tonghuashun document's trade facts to verified asset identities."""
import argparse
from datetime import date
from pathlib import Path
import sys
from uuid import UUID

from psycopg2.extras import RealDictCursor

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend/api'))
from domain.assets.asset_master import asset_id_for
from domain.imports.account_import_store import get_conn
from domain.imports.isolation import account_import_scope, source_enabled
from domain.imports.ths_asset_candidates import collect_candidates, find_identities, initialize
from domain.investments.trade_read_v2 import load_trade_rows


def link_assets(cur, source_id, *, expected_count, apply):
    if not source_enabled(cur):
        raise ValueError('source database is not enabled')
    if apply:
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended('account-import:tonghuashun',0))")
        cur.execute('LOCK TABLE pams_asset_master, pams_asset_alias IN SHARE ROW EXCLUSIVE MODE')
    cur.execute("""SELECT sha256 FROM source_records WHERE source_id=%s AND account_code='tonghuashun'
        AND document_type='trade' AND status='published' AND acquisition_method='file'""", (str(source_id),))
    source = cur.fetchone()
    if not source or not source['sha256']:
        raise ValueError('published Tonghuashun source file required')
    scope = {'account_code': 'tonghuashun', 'start': date.min, 'end': date.max, 'source_id': source_id}
    rows = load_trade_rows(cur, **scope)
    if len(rows) != expected_count or any(row['source_evidence_status'] != 'verified' for row in rows):
        raise ValueError('trade source count or evidence changed')
    events = [row for row in rows if row['trade_type'] in {'buy', 'sell', 'dividend', 'tax'}]
    cur.execute('SELECT source_item_id,raw_fields FROM source_items WHERE source_id=%s', (str(source_id),))
    originals = {row['source_item_id']: row['raw_fields'] for row in cur.fetchall()}
    parsed = {'rows': [{'fields': originals[event['source_item_id']]['initial_fields'],
                       'source': originals[event['source_item_id']]['source']} for event in events]}
    if any(row['fields'].get('symbol') != event['symbol'] or
           row['fields'].get('business_date') != event['trade_date'].isoformat()
           for row, event in zip(parsed['rows'], events)):
        raise ValueError('source security identity does not match the trade')
    candidates, conflicts = collect_candidates(parsed)
    if conflicts or not candidates or {item['symbol'] for item in candidates} != {row['symbol'] for row in events}:
        raise ValueError(f'source security identities require review: {conflicts}')
    links = {}
    missing = []
    for candidate in candidates:
        symbol = candidate['symbol']
        proposed_id = asset_id_for(symbol, candidate['name'], 'A股个股')
        matches = find_identities(cur, symbol, proposed_id)
        if not matches:
            missing.append(symbol)
            links[symbol] = proposed_id
            continue
        asset = matches[0]
        if (len(matches) != 1 or asset['primary_symbol'] != symbol or asset['exchange'] != candidate['exchange']
                or asset['status'] != 'active' or asset['currency'] != 'CNY'
                or asset['asset_type'] not in {'stock', 'A股个股'}):
            raise ValueError(f'existing security identity requires review: {symbol}')
        links[symbol] = asset['asset_id']
    if any(row['asset_id'] and row['asset_id'] != links[row['symbol']] for row in events):
        raise ValueError('existing trade asset association conflicts')
    unlinked = [row for row in events if not row['asset_id']]
    if apply:
        result = initialize(cur, parsed, source_record_id=source_id, file_sha256=source['sha256'])
        if result['conflicts'] or set(result['created']) != set(missing):
            raise ValueError('asset initialization plan changed')
        cur.executemany('UPDATE pams_investment_trade_fact SET asset_id=%s WHERE trade_id=%s AND asset_id IS NULL',
                        [(links[row['symbol']], row['trade_id']) for row in unlinked])
        verified = load_trade_rows(cur, **scope)
        if (len(verified) != expected_count or any(row['source_evidence_status'] != 'verified' for row in verified)
                or any(row['asset_id'] != links[row['symbol']] for row in verified if row['symbol'] in links)):
            raise ValueError('asset links failed post-write evidence check')
    return {'source_rows': len(rows), 'security_rows': len(events), 'new_assets': len(missing),
            'new_links': len(unlinked)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-id', required=True, type=UUID)
    parser.add_argument('--expected-count', required=True, type=int)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    with account_import_scope(), get_conn() as conn:
        conn.set_session(isolation_level='SERIALIZABLE', readonly=not args.apply)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SET LOCAL statement_timeout='30s'")
            cur.execute("SET LOCAL lock_timeout='3s'")
            result = link_assets(cur, args.source_id, expected_count=args.expected_count, apply=args.apply)
    print(result)


if __name__ == '__main__':
    main()
