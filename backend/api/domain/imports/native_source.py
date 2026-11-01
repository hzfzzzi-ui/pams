"""Isolated source-first import; no legacy draft or audit table writes.

Reviewed CNY asset rows and same-currency bank statements use explicit sources. The caller
owns the transaction; existing endpoints select it only in marked test databases.
"""
from __future__ import annotations

import os
import re

import hmac
import json
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

from psycopg2.extensions import ISOLATION_LEVEL_SERIALIZABLE
from psycopg2.extras import Json

from domain.imports import account_import_preflight as preflight
from domain.imports.account_import_plan import bank_leg
from domain.imports import account_import_store as store
from domain.imports.isolation import source_enabled
from domain.imports.unified_source import add_item, create_batch, lock_source_review
from domain.investments.holding_evidence_writer import (
    finish_holding_change,
    assert_holding_accounts_locked,
    lock_holding_accounts,
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
    cur.execute('SELECT base_currency FROM pams_accounts WHERE account_code=%s AND active', (account_code,))
    account = cur.fetchone()
    currency = account['base_currency'] if account else None
    bank_cash = kind == 'cash' and store.statement.is_statement(account_code)
    if not isinstance(currency, str) or (currency != 'CNY' and (not bank_cash or not re.fullmatch(r'[A-Z]{3}', currency))):
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
             'mode': b['acquisition_method'], 'completeness': b['completeness'], 'revision': revision, 'rows': rows, 'files': files,
             'unified_source': True, 'scope_attestation': b.get('scope_attestation')}
    return source, batch


def attest_complete_scope(cur, *, batch_id: UUID, revision: int, as_of, member_count: int,
                          cash_amount: str, note: str, confirmed: bool):
    """Record an explicit account-owner scope decision on the reviewed source."""
    from datetime import date
    from decimal import Decimal, InvalidOperation
    _require(cur)
    if confirmed is not True or not isinstance(note, str) or not note.strip() or len(note) > 500:
        raise ValueError('complete scope requires explicit confirmation and a note')
    try:
        day = date.fromisoformat(str(as_of))
        amount = Decimal(cash_amount)
    except (ValueError, TypeError, InvalidOperation) as exc:
        raise ValueError('complete scope date or cash is invalid') from exc
    if not amount.is_finite() or amount < 0 or amount >= Decimal('1e14') or amount != amount.quantize(Decimal('0.0001')):
        raise ValueError('complete scope cash is invalid or unrepresentable')
    locked_accounts = lock_publication_accounts(cur, batch_id)
    source, batch = _snapshot(cur, batch_id, revision)
    actual_accounts = {batch['account_code']} | {r['fields'].get('counter_account_code') for r in batch['rows'] if r['fields'].get('counter_account_code')}
    if locked_accounts != actual_accounts:
        raise ValueError('native account set changed')
    if batch['kind'] != 'holding' or batch['completeness'] != 'complete':
        raise ValueError('complete scope requires a complete holding batch')
    result = preflight.preview(cur, batch)
    rows = [row for row in result['_prepared'] if row['status'] in {'ready','duplicate'} and row.get('snapshot')]
    if not result['can_publish'] or len(rows) != member_count or len(rows) == 0:
        raise ValueError('complete scope members require a passing full preview')
    if any(row['snapshot']['as_of'] != day for row in rows):
        raise ValueError('complete scope date conflicts with reviewed members')
    cash = [row for row in rows if row['snapshot']['major_category'] == '现金']
    if len(cash) != 1 or cash[0]['snapshot']['market_value'] != amount:
        raise ValueError('complete scope requires one explicitly reviewed cash balance')
    member_fields = ('symbol','name','asset_id','currency','major_category','minor_category','asset_liability',
                     'quantity','cost','market_value','current_price','buy_price')
    members = sorted([{'source_item_id': row['row_id'], **store.norm({field:row['snapshot'][field] for field in member_fields})}
                      for row in rows], key=lambda row: row['source_item_id'])
    attestation = {'kind':'user_confirmed_complete_account', 'account_code':batch['account_code'],
                   'as_of':day.isoformat(), 'member_count':member_count, 'cash_amount':str(amount),
                   'revision':revision+1, 'members':members, 'note':note.strip()}
    cur.execute('''UPDATE public.source_batches SET scope_attestation=%s,revision=revision+1,updated_at=now()
        WHERE batch_id=%s AND revision=%s AND status IN ('draft','review') RETURNING revision''',
        (Json(attestation),str(batch_id),revision))
    changed = cur.fetchone()
    if not changed:
        raise ValueError('complete scope batch changed')
    return {'revision':changed['revision'], 'scope_attestation':attestation}


def preview_native(cur, *, batch_id: UUID, revision: int, locked_accounts=None):
    _require(cur)
    if locked_accounts is None:
        locked_accounts = lock_publication_accounts(cur, batch_id)
    else:
        assert_holding_accounts_locked(cur, locked_accounts)
    source, batch = _snapshot(cur, batch_id, revision, allow_pending=True)
    actual_accounts = {batch['account_code']} | {r['fields'].get('counter_account_code') for r in batch['rows'] if r['fields'].get('counter_account_code')}
    if locked_accounts != actual_accounts:
        raise ValueError('native account set changed')
    result = preflight.preview(cur, batch)
    pending = [row for row in result['_prepared'] if row['status'] == 'pending']
    result['can_publish_ready'] = bool(
        batch['account_code'] == 'tonghuashun' and batch['kind'] == 'trade'
        and set(source['snapshot']['blockers']) <= {'pending_items', 'no_confirmed_items'}
        and result['counts']['new'] > 0 and pending
        and all(row['status'] in {'ready', 'pending', 'excluded'} for row in result['_prepared'])
        and all(row['source'].get('profile') == 'ths_broker_ledger_v1' and row['source'].get('origin') == 'file'
                for row in result['_prepared'] if row['status'] == 'ready')
        and all(row['fields'].get('action') in {'deposit', 'withdraw'}
                and row['source'].get('profile') == 'ths_broker_ledger_v1'
                and row['source'].get('origin') == 'file'
                and row['source'].get('file_id')
                and {issue.get('code') for issue in row['issues']} == {'counter_required'}
                for row in pending))
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
        if entry.get('operation') == 'confirm_source':
            continue
        value = entry['after']
        if entry['table'] in {'pams_cashflow_fact', 'pams_investment_trade_fact'}:
            value['source_table'] = 'source_items'
            identity = {'source_id': value['source_id']}
            if entry['table'] == 'pams_investment_trade_fact':
                identity['source_key'] = value['source_key']
            value['source_hash'] = make_source_hash('source_items', identity)
    return {'source_sha256': source['source_snapshot_sha256'], 'preview': store.norm(result),
            'prepared_at': datetime.now(UTC).isoformat()}


def enabled() -> bool:
    return os.environ.get('PAMS_IMPORT_PUBLISH_ENABLED', '').lower() == 'true'


def _write_snapshot(cur, row):
    value = row['snapshot']
    columns = tuple(value)
    updates = ','.join(f'{k}=EXCLUDED.{k}' for k in columns if k not in {'account_code','name','as_of'})
    cur.execute(f"""INSERT INTO pams_holdings_snapshot ({','.join(columns)})
        VALUES ({','.join('%(' + k + ')s' for k in columns)})
        ON CONFLICT(account_code,name,as_of) DO UPDATE SET {updates},updated_at=now() RETURNING id""", value)
    return cur.fetchone()['id']


def _existing_source(cur, table, key):
    cur.execute(f'SELECT 1 FROM {table} WHERE source_table=%s AND source_id=%s LIMIT 1', ('source_items', key))
    return cur.fetchone() is not None


def _unverified_exits(rows, batch):
    attestation = batch.get('scope_attestation')
    if (batch['completeness'] == 'complete' and isinstance(attestation,dict) and
        attestation.get('kind') == 'user_confirmed_complete_account' and
        attestation.get('account_code') == batch['account_code'] and attestation.get('revision') == batch['revision']):
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
    actual: list[tuple[str, int, str]] = []
    for table, pk in (('pams_cashflow_fact', 'cashflow_id'), ('pams_investment_trade_fact', 'trade_id'),
                      ('pams_holdings_snapshot', 'id')):
        cur.execute(f'SELECT f.{pk} AS id,f.source_item_id FROM {table} f '
                    'JOIN source_items s ON s.source_item_id=f.source_item_id WHERE s.batch_id=%s', (str(batch_id),))
        actual.extend((table, row['id'], str(row['source_item_id'])) for row in cur.fetchall())
    if sorted(actual) != sorted(records):
        raise ValueError('unexpected source fact association')


def _replay(cur, batch_id, revision, source_sha256):
    cur.execute('SELECT * FROM source_batches WHERE batch_id=%s FOR UPDATE', (str(batch_id),))
    state = cur.fetchone()
    if not state:
        raise ValueError('native source batch missing')
    if state['workflow'] != 'account_import':
        raise ValueError('source batch belongs to another workflow')
    if state['status'] != 'published':
        return None
    if state['revision'] != revision or not hmac.compare_digest(state['published_source_hash'], source_sha256):
        raise ValueError('published source identity differs')
    from domain.imports.source_fact_evidence import _LockedNativeReplay, verify_native_fact
    # Proof locks and caches exist only for this replay call. A subsequent
    # replay, including after savepoint rollback, always rebuilds the evidence.
    with _LockedNativeReplay(cur, state) as replay_proof:
        snapshot_cache: dict = {}
        cur.execute('SELECT source_item_id,review_status FROM source_items WHERE batch_id=%s ORDER BY source_item_id', (str(batch_id),))
        items = cur.fetchall()
        saved = state['published_result']
        if not isinstance(saved, dict) or not isinstance(saved.get('records'), list):
            raise ValueError('published source result missing')  # noqa: TRY004
        records: list[tuple[str, int, str]] = []
        bank_by_item: dict[str, list[dict]] = {}
        for confirmation in saved.get('bank_source_confirmations', []):
            bank_by_item.setdefault(str(confirmation['source_item_id']), []).append(confirmation)
        updates_by_item: dict[str, list[int]] = {}
        for record in saved['records']:
            if record.get('table') == 'pams_holdings_snapshot' and record.get('role') == 'holding_update':
                updates_by_item.setdefault(str(record.get('row_id')), []).append(record['id'])
        pairs_by_facts: dict[tuple[int, int], list[dict]] = {}
        for pair in saved.get('confirmed_pairs', []):
            pairs_by_facts.setdefault((pair['trade_id'], pair['bank_id']), []).append(pair)
        for item in items:
            item_id = str(item['source_item_id'])
            found = {}
            for table, pk in (('pams_cashflow_fact', 'cashflow_id'), ('pams_investment_trade_fact', 'trade_id'),
                              ('pams_holdings_snapshot', 'id')):
                cur.execute(f'SELECT {pk} AS id FROM {table} WHERE source_item_id=%s', (item_id,))
                found[table] = [r['id'] for r in cur.fetchall()]
            cash, trade, holding = (found['pams_cashflow_fact'], found['pams_investment_trade_fact'], found['pams_holdings_snapshot'])
            bank_sources = bank_by_item.get(item_id, [])
            if bank_sources:
                if len(bank_sources) != 1 or cash or trade or holding:
                    raise ValueError('published bank source confirmation identity differs')
                from domain.ledger.bank_source_confirmation import verified
                if not verified(cur, bank_sources[0]['confirmation_id']):
                    raise ValueError('published bank source confirmation is invalidated')
                cash = [bank_sources[0]['cashflow_id']]
                found['pams_cashflow_fact'] = cash
            expected_updates = updates_by_item.get(item_id, [])
            if expected_updates:
                if len(expected_updates) != 1 or holding:
                    raise ValueError('published holding update identity differs')
                holding = [_verified_holding_update(cur, item_id, expected_updates[0], snapshot_cache=snapshot_cache)]
                found['pams_holdings_snapshot'] = holding
            elif holding:
                for holding_id in holding:
                    _verified_holding_update(cur, item_id, holding_id, snapshot_cache=snapshot_cache)
            if item['review_status'] == 'excluded':
                if cash or trade or holding:
                    raise ValueError('excluded source has published facts')
                continue
            if item['review_status'] != 'confirmed' or len(trade) > 1 or len(cash) > 1 or len(holding) > 1:
                raise ValueError('published source fact coverage invalid')
            if trade:
                cur.execute('SELECT cashflow_id FROM pams_investment_trade_fact WHERE trade_id=%s', (trade[0],))
                leg = cur.fetchone()['cashflow_id']
                if holding:
                    raise ValueError('published funding leg differs')
                if cash != ([leg] if leg is not None else []):
                    pairs = pairs_by_facts.get((trade[0], leg), [])
                    if cash or len(pairs) != 1:
                        raise ValueError('published funding leg differs')
                    from domain.ledger.confirmed_transfers import read_one
                    pair = read_one(cur, pairs[0]['transfer_id'])
                    if not pair or pair['evidence_status'] != 'confirmed':
                        raise ValueError('published bank confirmation is revoked or invalidated')
            elif len(cash) + len(holding) != 1:
                raise ValueError('published source fact coverage invalid')
            for table, ids in found.items():
                if table != 'pams_holdings_snapshot' and not (table == 'pams_cashflow_fact' and bank_sources):
                    for identifier in ids:
                        if verify_native_fact(cur, table=table, fact_id=identifier, _replay_context=replay_proof)['status'] != 'verified':
                            raise ValueError('published financial fact evidence differs')
                records.extend((table, identifier, item_id) for identifier in ids)
        recorded = [(row['table'], row['id'], str(row['row_id'])) for row in saved['records']]
        if sorted(records) != sorted(recorded):
            raise ValueError('published source result differs from facts')
        return {'records': records, 'details': saved['records'], 'counts': saved['counts'],
                'published_at': saved['published_at'], 'replayed': True,
                **({'deferred_batch_id': saved['partial']['deferred_batch_id']} if saved.get('partial') else {})}


def _verified_holding_update(cur, item_id, holding_id, *, snapshot_cache=None):
    if not verify_historical_holding_source(cur, holding_id=holding_id, source_item_id=item_id, snapshot_cache=snapshot_cache):
        raise ValueError('holding update source evidence missing or invalid')
    return holding_id



def lock_publication_accounts(cur, batch_id):
    """Lock all known accounts before acquiring a source-batch row lock.

    Callers must revalidate the account set after locking the batch. Under the
    required serializable transaction, a concurrent draft edit cannot silently
    change the set between this read and the locked snapshot.
    """
    cur.execute('SELECT account_code FROM source_batches WHERE batch_id=%s', (str(batch_id),))
    found = cur.fetchone()
    if not found:
        raise ValueError('native source batch missing')
    accounts = {found['account_code']}
    cur.execute("SELECT parsed_fields->'fields'->>'counter_account_code' AS counter_account FROM source_items WHERE batch_id=%s", (str(batch_id),))
    accounts.update(row['counter_account'] for row in cur.fetchall() if row['counter_account'])
    lock_holding_accounts(cur, accounts)
    return accounts

def publish_native(cur, *, batch_id: UUID, revision: int, source_sha256: str,
                   preview_token: str, prepared_at: str, published_token: str | None = None, locked_accounts=None,
                   partial_info: dict | None = None):
    _require(cur)
    if not enabled() or cur.connection.isolation_level != ISOLATION_LEVEL_SERIALIZABLE:
        raise ValueError('native publication disabled or not serializable')
    cur.execute('SAVEPOINT native_source_publish')
    try:
        if locked_accounts is None:
            locked_accounts = lock_publication_accounts(cur, batch_id)
        else:
            assert_holding_accounts_locked(cur, locked_accounts)
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
        actual_accounts = {batch['account_code']} | {row['fields'].get('counter_account_code') for row in batch['rows']
                                                       if row['fields'].get('counter_account_code')}
        if actual_accounts != locked_accounts:
            raise ValueError('native publication account set changed')
        result = preflight.preview(cur, batch)
        if (not hmac.compare_digest(result['preview_token'], preview_token) or not result['can_publish'] or
            _unverified_exits(result['_prepared'], batch) or
            any(row['status'] == 'duplicate' and not row.get('snapshot') for row in result['_prepared'])):
            raise ValueError('native preflight changed or blocked')
        records, details, direct_records, holding_versions, confirmed_pairs, bank_source_confirmations = [], [], [], [], [], []
        cur.execute("SELECT to_regclass('public.valuation_evidence') AS table_name")
        versioning = cur.fetchone()['table_name'] is not None
        for row in result['_prepared']:
            unchanged_holding = row['status'] == 'duplicate' and bool(row.get('snapshot'))
            if row['status'] != 'ready' and not unchanged_holding:
                continue
            item_id, key = row['row_id'], row['key']
            indirect_holding = False
            indirect_bank_source = False
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
                    identifier = _write_snapshot(cur, row)
                table, pk = 'pams_holdings_snapshot', 'id'
                if versioning:
                    holding_versions.append((identifier, item_id, indirect_holding))
            elif row.get('cashflow_confirmation'):
                identifier, table, pk = row['cashflow_confirmation'], 'pams_cashflow_fact', 'cashflow_id'
                indirect_bank_source = True
                bank_source_confirmations.append({'cashflow_id':identifier, 'source_item_id':item_id,
                                                  'confirmation_id':str(uuid4())})
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
                if fact['side'] in {'deposit', 'withdraw'} and not row.get('existing_bank_cashflow_id'):
                    leg = bank_leg(batch, row, fact)
                    leg.update(source_table='source_items', source_hash=make_source_hash('source_items', {'source_id': leg['source_id']}))
                    if insert_cashflow_facts(cur, [leg]) != 1:
                        raise ValueError('bank leg source conflict')
                    cur.execute('SELECT cashflow_id FROM pams_cashflow_fact WHERE source_table=%s AND source_id=%s', ('source_items', leg['source_id']))
                    fact['cashflow_id'] = cur.fetchone()['cashflow_id']
                    _attach(cur, 'pams_cashflow_fact', 'cashflow_id', fact['cashflow_id'], item_id)
                    records.append(('pams_cashflow_fact', fact['cashflow_id'], item_id))
                    direct_records.append(('pams_cashflow_fact', fact['cashflow_id'], item_id))
                    details.append({'row_id': item_id, 'table': 'pams_cashflow_fact', 'id': fact['cashflow_id'],
                                    'role': 'derived_bank_leg', 'before': None, 'after': store.norm(leg), 'source': row['source']})
                fact['source_table'] = 'source_items'
                fact['source_hash'] = make_source_hash('source_items', {'source_key': fact['source_key'], 'source_id': key})
                if insert_investment_trade_facts(cur, [fact], strict_source=True) != 1:
                    raise ValueError('trade source conflict')
                cur.execute('SELECT trade_id FROM pams_investment_trade_fact WHERE source_table=%s AND source_id=%s', ('source_items', key))
                identifier, table, pk = cur.fetchone()['trade_id'], 'pams_investment_trade_fact', 'trade_id'
                if row.get('existing_bank_cashflow_id'):
                    confirmed_pairs.append({'bank_id': row['existing_bank_cashflow_id'], 'trade_id': identifier,
                                            'side': fact['side'], 'transfer_id': str(uuid4())})
            if not indirect_holding and not indirect_bank_source:
                _attach(cur, table, pk, identifier, item_id)
                direct_records.append((table, identifier, item_id))
            records.append((table, identifier, item_id))
            detail = {'row_id': item_id, 'table': table, 'id': identifier,
                      'before': store.norm(row['before']), 'after': store.norm(row['after']), 'source': row['source']}
            if indirect_holding:
                detail['role'] = 'holding_update'
            if indirect_bank_source:
                detail['role'] = 'bank_source_confirmation'
            details.append(detail)
        expected = result['counts']['new'] + sum(row['status'] == 'duplicate' and bool(row.get('snapshot')) for row in result['_prepared'])
        if len({record[2] for record in records}) != expected:
            raise ValueError('native source fact coverage incomplete')
        _check_associations(cur, batch_id, direct_records)
        attestation = batch.get('scope_attestation')
        scope_version_id = (str(uuid4()) if isinstance(attestation,dict) and batch['kind']=='holding'
                            and batch['completeness']=='complete' and attestation.get('revision')==revision else None)
        saved = {'counts': result['counts'], 'records': store.norm(details), 'confirmed_pairs': confirmed_pairs,
                 'bank_source_confirmations': bank_source_confirmations, 'account_scope_version_id': scope_version_id,
                 'published_at': datetime.now(UTC).isoformat()}
        if partial_info is not None:
            saved['partial'] = partial_info
        cur.execute("UPDATE source_batches SET status='published',published_source_hash=%s,published_preview_token=%s,published_result=%s,updated_at=now() WHERE batch_id=%s",
                    (source_sha256, published_token, Json(saved), str(batch_id)))
        if bank_source_confirmations:
            from domain.ledger.bank_source_confirmation import confirm as confirm_bank_source
            for item in bank_source_confirmations:
                confirm_bank_source(cur, **item)
        if confirmed_pairs:
            from domain.ledger.confirmed_transfers import confirm
            for pair in confirmed_pairs:
                bank_id, trade_id, side = pair['bank_id'], pair['trade_id'], pair['side']
                if side == 'deposit':
                    confirm(cur, from_kind='cashflow', from_id=bank_id, to_kind='trade', to_id=trade_id,
                            note='用户在资料核准时明确选择唯一银行流水', confirmed=True, transfer_id=pair['transfer_id'])
                else:
                    confirm(cur, from_kind='trade', from_id=trade_id, to_kind='cashflow', to_id=bank_id,
                            note='用户在资料核准时明确选择唯一银行流水', confirmed=True, transfer_id=pair['transfer_id'])
        snapshot_cache: dict = {}
        for identifier, item_id, indirect in holding_versions:
            finish_holding_change(cur, holding_id=identifier, source_item_id=item_id, snapshot_cache=snapshot_cache)
            if indirect:
                _verified_holding_update(cur, item_id, identifier, snapshot_cache=snapshot_cache)
        if scope_version_id:
            from datetime import date
            from domain.investments.account_snapshot_evidence import record_complete
            record_complete(cur, account_code=batch['account_code'], as_of=date.fromisoformat(attestation['as_of']),
                members=[{'holding_id':identifier,'source_item_id':item_id} for identifier,item_id,_ in holding_versions],
                source_batch_id=batch_id, source_revision=revision, source_sha256=source_sha256, version_id=scope_version_id)
        cur.execute('RELEASE SAVEPOINT native_source_publish')
        return {'records': records, 'details': saved['records'], 'counts': saved['counts'],
                'published_at': saved['published_at'], 'replayed': False,
                **({'deferred_batch_id': partial_info['deferred_batch_id']} if partial_info else {})}
    except Exception:
        cur.execute('ROLLBACK TO SAVEPOINT native_source_publish')
        cur.execute('RELEASE SAVEPOINT native_source_publish')
        raise
