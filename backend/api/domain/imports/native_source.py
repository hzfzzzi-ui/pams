"""Isolated source-first import; no legacy draft or audit table writes.

This initial path covers reviewed CNY manual, template and OCR rows. The caller
owns the transaction; existing endpoints select it only in marked test databases.
"""
from __future__ import annotations

import hmac
import json
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

from psycopg2.extensions import ISOLATION_LEVEL_SERIALIZABLE
from psycopg2.extras import Json

from domain.imports import account_import_preflight as preflight
from domain.imports import account_import_publish as legacy_publisher
from domain.imports import account_import_store as store
from domain.imports.account_import_plan import bank_leg
from domain.imports.isolation import source_enabled
from domain.imports.unified_source import add_item, create_batch, lock_source_review
from domain.investments.holding_evidence_writer import (
    finish_holding_change,
    prepare_holding_change,
    verify_historical_holding_source,
)
from domain.investments.investment_trade_fact import insert_investment_trade_facts
from domain.ledger.cashflow_fact import insert_cashflow_facts, make_source_hash


def _require(cur):
    if not source_enabled(cur) or cur.connection.autocommit:
        raise ValueError('native import requires an isolated transactional database')


def create_manual_batch(cur, *, account_code: str, kind: str, completeness: str, institution: str, mode='manual'):
    _require(cur)
    if kind not in {'holding', 'trade', 'cash'} or completeness not in {'partial', 'complete'} or mode not in {'manual', 'upload'}:
        raise ValueError('invalid native batch')
    cur.execute('SELECT 1 FROM pams_accounts WHERE account_code=%s AND active AND base_currency=%s',
                (account_code, 'CNY'))
    if not cur.fetchone():
        raise ValueError('account missing, inactive or unsupported currency')
    batch_id = uuid4()
    create_batch(cur, batch_id=batch_id, account_code=account_code, business_type=kind,
                 institution=institution, acquisition_method=mode)
    cur.execute("UPDATE source_batches SET completeness=%s,workflow='account_import' WHERE batch_id=%s", (completeness, str(batch_id)))
    return batch_id


def add_manual_record(cur, *, batch_id: UUID, fields: dict):
    _require(cur)
    if (not isinstance(fields, dict) or set(fields) - store.FIELDS or
        any(value is not None and (not isinstance(value, str) or len(value) > 500) for value in fields.values())):
        raise ValueError('invalid manual fields')
    cur.execute('SELECT business_type,status,workflow FROM source_batches WHERE batch_id=%s FOR UPDATE', (str(batch_id),))
    batch = cur.fetchone()
    if not batch or batch['status'] != 'draft' or batch['workflow'] != 'account_import' or fields.get('kind') != batch['business_type']:
        raise ValueError('manual source batch unavailable or kind differs')
    cur.execute('SELECT count(*) AS n FROM source_items WHERE batch_id=%s', (str(batch_id),))
    if cur.fetchone()['n'] >= store.MAX_ROWS:
        raise ValueError('too many manual source records')
    item_id = uuid4()
    raw = {'fields': fields}
    data = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    add_item(cur, item_id=item_id, batch_id=batch_id, source_uid=str(item_id), parse_method='manual',
             record_hash=sha256(data).hexdigest(), raw_fields=raw,
             parsed_fields={'fields': fields, 'issues': [], 'acknowledged': False, 'excluded': False})
    return item_id


def _snapshot(cur, batch_id, revision, *, allow_pending=False):
    source = lock_source_review(cur, batch_id=batch_id, expected_revision=revision)
    snapshot = source['snapshot']
    if snapshot['blockers'] and (not allow_pending or 'file_hash_mismatch' in snapshot['blockers']):
        raise ValueError('source review is incomplete')
    b = snapshot['batch']
    if b['workflow'] != 'account_import':
        raise ValueError('source batch belongs to another workflow')
    rows = []
    for item in snapshot['items']:
        parsed = item['parsed_fields']
        if item['parse_method'] not in {'manual', 'template', 'ocr'} or item['review_status'] not in ({'confirmed', 'excluded', 'pending'} if allow_pending else {'confirmed', 'excluded'}):
            raise ValueError('native review decision invalid')
        fields = parsed.get('fields')
        if (set(parsed) != {'fields', 'issues', 'acknowledged', 'excluded'} or not isinstance(parsed['issues'], list) or
            not isinstance(parsed['acknowledged'], bool) or not isinstance(parsed['excluded'], bool) or
            parsed['excluded'] != (item['review_status'] == 'excluded') or
            not isinstance(fields, dict) or set(fields) - store.FIELDS or
            any(v is not None and (not isinstance(v, str) or len(v) > 500) for v in fields.values())):
            raise ValueError('native candidate fields invalid')
        origin = item['raw_fields'].get('source', {'origin': 'manual'})
        rows.append({'row_id': str(item['source_item_id']), 'fields': fields, 'source': origin,
                     'issues': parsed.get('issues', []), 'acknowledged': parsed.get('acknowledged', False),
                     'excluded': item['review_status'] == 'excluded'})
    files = [{'file_id': str(f['file_id']), 'filename': f['filename'], 'content_type': f['content_type'],
              'sha256': f['sha256'], 'metadata': {**f['current_metadata'],
              **({'review_fingerprint': f['review_fingerprint']} if f['review_fingerprint'] else {})}}
             for f in snapshot['files']]
    batch = {'batch_id': str(batch_id), 'account_code': b['account_code'], 'kind': b['business_type'],
             'mode': b['acquisition_method'], 'completeness': b['completeness'], 'revision': revision, 'rows': rows, 'files': files}
    return source, batch


def preview_native(cur, *, batch_id: UUID, revision: int):
    _require(cur)
    source, batch = _snapshot(cur, batch_id, revision, allow_pending=True)
    result = preflight.preview(cur, batch)
    for row in result['_prepared']:
        if row['status'] == 'ready' and row.get('fact') and _existing_source(cur, 'pams_investment_trade_fact', row['key']):
            raise ValueError('trade source identity already exists')
        if row['status'] == 'ready' and row.get('cashflow') and _existing_source(cur, 'pams_cashflow_fact', row['key']):
            raise ValueError('cashflow source identity already exists')
    exits = _unverified_exits(result['_prepared'], batch)
    if exits:
        result['can_publish'] = False
        for row in result['rows']:
            if row['row_id'] in exits:
                row['status'] = 'blocked'
                row['issues'].append({'field': None, 'code': 'exit_scope_unverified',
                    'message': '零持仓退出需要核对完整账户范围及明确零值证据', 'severity': 'error'})
        result['counts']['new'] -= len(exits)
        result['counts']['blocked'] += len(exits)
        result['write_plan'] = [entry for entry in result['write_plan'] if entry['row_id'] not in exits]
        result['write_count'] = len(result['write_plan'])
        result['issue_groups'].append({'code': 'exit_scope_unverified',
            'message': '零持仓退出需要核对完整账户范围及明确零值证据', 'row_ids': sorted(exits), 'count': len(exits)})
    if source['snapshot']['blockers']:
        result['can_publish'] = False
    if any(row['status'] == 'duplicate' and not row.get('snapshot') for row in result['_prepared']):
        raise ValueError('duplicate source requires explicit association')
    result.pop('_prepared')
    for entry in result['write_plan']:
        value = entry['after']
        if entry['table'] in {'pams_cashflow_fact', 'pams_investment_trade_fact'}:
            value['source_table'] = 'source_items'
            identity = {'source_id': value['source_id']}
            if entry['table'] == 'pams_investment_trade_fact':
                identity['source_key'] = value['source_key']
            value['source_hash'] = make_source_hash('source_items', identity)
    return {'source_sha256': source['source_snapshot_sha256'], 'preview': store.norm(result),
            'prepared_at': datetime.now(UTC).isoformat()}


def _existing_source(cur, table, key):
    cur.execute(f'SELECT 1 FROM {table} WHERE source_table IN (%s,%s) AND source_id=%s LIMIT 1',
                ('pams_account_import_batch', 'source_items', key))
    return cur.fetchone() is not None


def _unverified_exits(rows, batch):
    if batch['completeness'] == 'complete':
        return set()
    return {row['row_id'] for row in rows if row['status'] == 'ready' and row.get('before') and row.get('snapshot')
            and row['fields'].get('asset_type') != 'cash'
            and all(row['snapshot'].get(field) == 0 for field in ('quantity', 'cost', 'market_value'))}


def _attach(cur, table, pk, identifier, item_id):
    cur.execute(f'UPDATE {table} SET source_item_id=%s WHERE {pk}=%s '
                'AND (source_item_id IS NULL OR source_item_id=%s)', (item_id, identifier, item_id))
    if cur.rowcount != 1:
        raise ValueError('source fact association conflict')


def _check_associations(cur, batch_id, records):
    actual = []
    for table, pk in (('pams_cashflow_fact', 'cashflow_id'), ('pams_investment_trade_fact', 'trade_id'),
                      ('pams_holdings_snapshot', 'id')):
        cur.execute(f'SELECT f.{pk} AS id,f.source_item_id FROM {table} f '
                    'JOIN source_items s ON s.source_item_id=f.source_item_id WHERE s.batch_id=%s', (str(batch_id),))
        actual.extend((table, row['id'], str(row['source_item_id'])) for row in cur.fetchall())
    if sorted(actual) != sorted(records):
        raise ValueError('unexpected source fact association')


def _replay(cur, batch_id, revision, source_sha256):
    cur.execute('SELECT status,revision,workflow,published_source_hash,published_result FROM source_batches WHERE batch_id=%s FOR UPDATE', (str(batch_id),))
    state = cur.fetchone()
    if not state:
        raise ValueError('native source batch missing')
    if state['workflow'] != 'account_import':
        raise ValueError('source batch belongs to another workflow')
    if state['status'] != 'published':
        return None
    if state['revision'] != revision or not hmac.compare_digest(state['published_source_hash'], source_sha256):
        raise ValueError('published source identity differs')
    cur.execute('SELECT source_item_id,review_status FROM source_items WHERE batch_id=%s ORDER BY source_item_id', (str(batch_id),))
    items = cur.fetchall()
    saved = state['published_result']
    if not isinstance(saved, dict) or not isinstance(saved.get('records'), list):
        raise ValueError('published source result missing')  # noqa: TRY004
    records = []
    for item in items:
        item_id = str(item['source_item_id'])
        found = {}
        for table, pk in (('pams_cashflow_fact', 'cashflow_id'), ('pams_investment_trade_fact', 'trade_id'),
                          ('pams_holdings_snapshot', 'id')):
            cur.execute(f'SELECT {pk} AS id FROM {table} WHERE source_item_id=%s', (item_id,))
            found[table] = [r['id'] for r in cur.fetchall()]
        cash, trade, holding = (found['pams_cashflow_fact'], found['pams_investment_trade_fact'], found['pams_holdings_snapshot'])
        expected_updates = [r['id'] for r in saved['records'] if str(r.get('row_id')) == item_id
                            and r.get('table') == 'pams_holdings_snapshot' and r.get('role') == 'holding_update']
        if expected_updates:
            if len(expected_updates) != 1 or holding:
                raise ValueError('published holding update identity differs')
            holding = [_verified_holding_update(cur, item_id, expected_updates[0])]
            found['pams_holdings_snapshot'] = holding
        if item['review_status'] == 'excluded':
            if cash or trade or holding:
                raise ValueError('excluded source has published facts')
            continue
        if item['review_status'] != 'confirmed' or len(trade) > 1 or len(cash) > 1 or len(holding) > 1:
            raise ValueError('published source fact coverage invalid')
        if trade:
            cur.execute('SELECT cashflow_id FROM pams_investment_trade_fact WHERE trade_id=%s', (trade[0],))
            leg = cur.fetchone()['cashflow_id']
            if holding or cash != ([leg] if leg is not None else []):
                raise ValueError('published funding leg differs')
        elif len(cash) + len(holding) != 1:
            raise ValueError('published source fact coverage invalid')
        for table, ids in found.items():
            records.extend((table, identifier, item_id) for identifier in ids)
    recorded = [(row['table'], row['id'], str(row['row_id'])) for row in saved['records']]
    if sorted(records) != sorted(recorded):
        raise ValueError('published source result differs from facts')
    return {'records': records, 'details': saved['records'], 'counts': saved['counts'],
            'published_at': saved['published_at'], 'replayed': True}


def _verified_holding_update(cur, item_id, holding_id):
    if not verify_historical_holding_source(cur, holding_id=holding_id, source_item_id=item_id):
        raise ValueError('holding update source evidence missing or invalid')
    return holding_id


def publish_native(cur, *, batch_id: UUID, revision: int, source_sha256: str,
                   preview_token: str, prepared_at: str, published_token: str | None = None):
    _require(cur)
    if not legacy_publisher.enabled() or cur.connection.isolation_level != ISOLATION_LEVEL_SERIALIZABLE:
        raise ValueError('native publication disabled or not serializable')
    cur.execute('SAVEPOINT native_source_publish')
    try:
        replay = _replay(cur, batch_id, revision, source_sha256)
        if replay is not None:
            cur.execute('RELEASE SAVEPOINT native_source_publish')
            return replay
        source, batch = _snapshot(cur, batch_id, revision)
        if not hmac.compare_digest(source['source_snapshot_sha256'], source_sha256):
            raise ValueError('source snapshot changed')
        prepared = datetime.fromisoformat(prepared_at)
        age = (datetime.now(UTC) - prepared).total_seconds()
        if age < 0 or age > 15 * 60:
            raise ValueError('native preview expired')
        cur.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('account-import:' + batch['account_code'],))
        result = preflight.preview(cur, batch)
        if (not hmac.compare_digest(result['preview_token'], preview_token) or not result['can_publish'] or
            _unverified_exits(result['_prepared'], batch) or
            any(row['status'] == 'duplicate' and not row.get('snapshot') for row in result['_prepared'])):
            raise ValueError('native preflight changed or blocked')
        records, details, direct_records, holding_versions = [], [], [], []
        cur.execute("SELECT to_regclass('public.pams_valuation_result_version') AS table_name")
        versioning = cur.fetchone()['table_name'] is not None
        for row in result['_prepared']:
            unchanged_holding = row['status'] == 'duplicate' and bool(row.get('snapshot'))
            if row['status'] != 'ready' and not unchanged_holding:
                continue
            item_id, key = row['row_id'], row['key']
            indirect_holding = False
            if row.get('snapshot'):
                target = row['snapshot']
                if not versioning:
                    raise ValueError('holding publication requires versioned evidence')
                prepared_holding = prepare_holding_change(cur, account_code=target['account_code'],
                    name=target['name'], as_of=target['as_of'], source_item_id=item_id)
                indirect_holding = prepared_holding['retain_origin']
                if unchanged_holding:
                    if prepared_holding['previous_id'] is None:
                        raise ValueError('unchanged holding evidence disappeared')
                    identifier = prepared_holding['previous_id']
                    indirect_holding = True
                else:
                    identifier = legacy_publisher._snapshot(cur, row)
                table, pk = 'pams_holdings_snapshot', 'id'
                if versioning:
                    holding_versions.append((identifier, item_id, indirect_holding))
            elif row.get('cashflow'):
                if _existing_source(cur, 'pams_cashflow_fact', key):
                    raise ValueError('cashflow source identity already exists')
                value = row['cashflow']
                value.update(source_table='source_items', source_hash=make_source_hash('source_items', {'source_id': key}))
                if insert_cashflow_facts(cur, [value]) != 1:
                    raise ValueError('cashflow source conflict')
                cur.execute('SELECT cashflow_id FROM pams_cashflow_fact WHERE source_table=%s AND source_id=%s', ('source_items', key))
                identifier, table, pk = cur.fetchone()['cashflow_id'], 'pams_cashflow_fact', 'cashflow_id'
            else:
                fact = row['fact']
                if _existing_source(cur, 'pams_investment_trade_fact', key):
                    raise ValueError('trade source identity already exists')
                if fact['side'] in {'deposit', 'withdraw'}:
                    leg = bank_leg(batch, row, fact)
                    leg.update(source_table='source_items', source_hash=make_source_hash('source_items', {'source_id': leg['source_id']}))
                    if insert_cashflow_facts(cur, [leg]) != 1:
                        raise ValueError('bank leg source conflict')
                    cur.execute('SELECT cashflow_id FROM pams_cashflow_fact WHERE source_table=%s AND source_id=%s',
                                ('source_items', leg['source_id']))
                    fact['cashflow_id'] = cur.fetchone()['cashflow_id']
                    _attach(cur, 'pams_cashflow_fact', 'cashflow_id', fact['cashflow_id'], item_id)
                    records.append(('pams_cashflow_fact', fact['cashflow_id'], item_id))
                    direct_records.append(('pams_cashflow_fact', fact['cashflow_id'], item_id))
                    details.append({'row_id': item_id, 'table': 'pams_cashflow_fact', 'id': fact['cashflow_id'],
                                    'role': 'bank_leg', 'before': None, 'after': store.norm(leg), 'source': row['source']})
                fact['source_table'] = 'source_items'
                fact['source_hash'] = make_source_hash('source_items', {'source_key': fact['source_key'], 'source_id': key})
                if insert_investment_trade_facts(cur, [fact], strict_source=True) != 1:
                    raise ValueError('trade source conflict')
                cur.execute('SELECT trade_id FROM pams_investment_trade_fact WHERE source_table=%s AND source_id=%s', ('source_items', key))
                identifier, table, pk = cur.fetchone()['trade_id'], 'pams_investment_trade_fact', 'trade_id'
            if not indirect_holding:
                _attach(cur, table, pk, identifier, item_id)
                direct_records.append((table, identifier, item_id))
            records.append((table, identifier, item_id))
            detail = {'row_id': item_id, 'table': table, 'id': identifier,
                      'before': store.norm(row['before']), 'after': store.norm(row['after']), 'source': row['source']}
            if indirect_holding:
                detail['role'] = 'holding_update'
            details.append(detail)
        expected = result['counts']['new'] + sum(row['status'] == 'duplicate' and bool(row.get('snapshot')) for row in result['_prepared'])
        if len({record[2] for record in records}) != expected:
            raise ValueError('native source fact coverage incomplete')
        _check_associations(cur, batch_id, direct_records)
        saved = {'counts': result['counts'], 'records': store.norm(details), 'published_at': datetime.now(UTC).isoformat()}
        cur.execute("UPDATE source_batches SET status='published',published_source_hash=%s,published_preview_token=%s,published_result=%s,updated_at=now() WHERE batch_id=%s",
                    (source_sha256, published_token, Json(saved), str(batch_id)))
        for identifier, item_id, indirect in holding_versions:
            finish_holding_change(cur, holding_id=identifier, source_item_id=item_id)
            if indirect:
                _verified_holding_update(cur, item_id, identifier)
        cur.execute('RELEASE SAVEPOINT native_source_publish')
        return {'records': records, 'details': saved['records'], 'counts': saved['counts'],
                'published_at': saved['published_at'], 'replayed': False}
    except Exception:
        cur.execute('ROLLBACK TO SAVEPOINT native_source_publish')
        cur.execute('RELEASE SAVEPOINT native_source_publish')
        raise
