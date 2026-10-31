"""Isolated, evidence-checked legacy bank source and cashflow migration.

This candidate is never registered as an application endpoint. Callers own the
transaction; only explicitly marked disposable PostgreSQL databases are used.
"""
from __future__ import annotations

import hmac
import json
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import NAMESPACE_URL, uuid5

from collectors.bank_statement_parser import parse_statement
from psycopg2.extensions import ISOLATION_LEVEL_SERIALIZABLE

from domain.imports.isolation import source_enabled
from domain.imports.unified_source import (
    add_item,
    attach_file,
    create_batch,
    lock_source_review,
)
from domain.ledger.cashflow_fact import (
    SOURCE_TABLE,
    _json_ready,
    build_cashflow_fact,
    insert_cashflow_facts,
)

_FIELDS = ('source_uid', 'transaction_date', 'posting_date', 'amount', 'currency', 'balance',
           'original_amount', 'original_currency', 'summary', 'description', 'counterparty')


def _isolated(cur):
    if not source_enabled(cur):
        raise ValueError('bank source candidate requires an isolated database')
    if cur.connection.autocommit:
        raise ValueError('bank source candidate requires a transaction')


def _identifier(kind, value):
    return uuid5(NAMESPACE_URL, f'pams:legacy-bank:{kind}:{value}')


def _locked_legacy(cur, import_id):
    cur.execute('SELECT * FROM pams_bank_statement_imports WHERE id=%s FOR UPDATE', (import_id,))
    imported = cur.fetchone()
    if not imported:
        raise ValueError('bank import missing')
    cur.execute('SELECT * FROM pams_bank_statement_rows WHERE import_id=%s ORDER BY id FOR UPDATE', (import_id,))
    rows = cur.fetchall()
    if not rows or any(row['account_code'] != imported['account_code'] for row in rows):
        raise ValueError('bank source rows missing or account differs')
    return dict(imported), [dict(row) for row in rows]


def _matches(cur, row_id):
    cur.execute("""SELECT * FROM pams_bank_transfer_matches
                   WHERE (source_row_id=%s OR target_row_id=%s)
                   AND status <> 'rejected' ORDER BY id FOR SHARE""", (row_id, row_id))
    matches = [dict(value) for value in cur.fetchall()]
    if len(matches) > 1:
        raise ValueError('ambiguous bank transfer confirmation')
    return matches


def _confirmed_cases(cur, row_id):
    cur.execute("""SELECT * FROM pams_reconciliation_case
        WHERE ((source_ref_table=%s AND source_ref_id=%s) OR (target_ref_table=%s AND target_ref_id=%s))
          AND (status='confirmed' OR reviewed_at IS NOT NULL) ORDER BY case_id FOR SHARE""",
        (SOURCE_TABLE, str(row_id), SOURCE_TABLE, str(row_id)))
    return [dict(value) for value in cur.fetchall()]


def _evidence(row, matches, cases):
    return _json_ready({**{key: row.get(key) for key in ('id', 'import_id', 'profile_code', 'bank',
                        'account_code', 'account_kind', *_FIELDS, 'category', 'subcategory',
                        'auto_classified', 'raw_json')}, 'transfer_matches': matches,
                        'confirmed_cases': cases})


def stage_bank_source(cur, *, import_id: int, profile: dict, filename: str, content: bytes):
    """Stage only when supplied original parses back to exactly the legacy rows."""
    _isolated(cur)
    cur.execute('SAVEPOINT bank_source_stage')
    try:
        result = _stage_bank_source(cur, import_id=import_id, profile=profile, filename=filename, content=content)
        cur.execute('RELEASE SAVEPOINT bank_source_stage')
        return result
    except Exception:
        cur.execute('ROLLBACK TO SAVEPOINT bank_source_stage')
        cur.execute('RELEASE SAVEPOINT bank_source_stage')
        raise


def _stage_bank_source(cur, *, import_id, profile, filename, content):
    imported, rows = _locked_legacy(cur, import_id)
    name = Path(str(imported['source_file']).replace('\\', '/')).name
    if (not filename or filename != name or Path(filename).name != filename or
        not isinstance(content, bytes) or not 0 < len(content) <= 10485760):
        raise ValueError('bank original filename or content invalid')
    fmt = filename.rsplit('.', 1)[-1].lower()
    if fmt not in {'csv', 'tsv', 'xlsx', 'xls', 'pdf', 'png', 'jpeg', 'jpg'}:
        raise ValueError('unsupported source file format')
    fmt = 'jpeg' if fmt == 'jpg' else fmt
    if (profile.get('profile_code') != imported['profile_code'] or
        profile.get('account_code') != imported['account_code'] or
        profile.get('bank', 'Manual') != imported['bank'] or
        profile.get('account_kind', 'manual') != imported['account_kind']):
        raise ValueError('bank profile differs from imported source')
    with TemporaryDirectory(prefix='pams-bank-source-') as directory:
        path = Path(directory) / filename
        path.write_bytes(content)
        parsed = parse_statement(path, profile)
    by_uid = {row['source_uid']: row for row in parsed.rows}
    if len(by_uid) != len(parsed.rows) or len(rows) != len(by_uid):
        raise ValueError('bank original row count or identity differs')
    for row in rows:
        match = by_uid.get(row['source_uid'])
        legacy_raw = row.get('raw_json')
        parsed_raw = match.get('raw') if match else None
        if (not match or not isinstance(legacy_raw, dict) or not isinstance(parsed_raw, dict) or
            any(row.get(key) != match.get(key) for key in _FIELDS) or
            {k: v for k, v in _json_ready(legacy_raw).items() if k != 'source_file'} !=
            {k: v for k, v in _json_ready(parsed_raw).items() if k != 'source_file'}):
            raise ValueError('bank original does not match legacy row')

    evidence = {row['source_uid']: _evidence(row, _matches(cur, row['id']), _confirmed_cases(cur, row['id']))
                for row in rows}
    batch_id = _identifier('batch', import_id)
    file_id = _identifier('file', import_id)
    cur.execute('SELECT batch_id FROM source_batches WHERE batch_id=%s FOR UPDATE', (str(batch_id),))
    if cur.fetchone():
        cur.execute('SELECT sha256, content FROM source_files WHERE batch_id=%s AND file_id=%s',
                    (str(batch_id), str(file_id)))
        original = cur.fetchone()
        cur.execute('SELECT source_uid,raw_fields FROM source_items WHERE batch_id=%s', (str(batch_id),))
        existing = {r['source_uid']: r['raw_fields'] for r in cur.fetchall()}
        if (not original or original['sha256'] != sha256(content).hexdigest() or
            bytes(original['content']) != content or existing != evidence):
            raise ValueError('previous bank source differs')
        return batch_id
    create_batch(cur, batch_id=batch_id, account_code=imported['account_code'], business_type='cash',
                 institution=imported['bank'], acquisition_method='upload')
    attach_file(cur, file_id=file_id, batch_id=batch_id, filename=filename, file_format=fmt,
                content_type='application/octet-stream', content=content)
    for row in rows:
        raw = evidence[row['source_uid']]
        digest = sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        add_item(cur, item_id=_identifier('row', row['source_uid']), batch_id=batch_id,
                 source_uid=row['source_uid'], file_id=file_id, parse_method='template',
                 record_hash=digest, raw_fields=raw, parsed_fields=raw)
    return batch_id


def publish_bank_source(cur, *, import_id: int, expected_revision: int, source_snapshot_sha256: str):
    """Publish reviewed CNY rows or associate existing facts without rewriting them."""
    _isolated(cur)
    if cur.connection.isolation_level != ISOLATION_LEVEL_SERIALIZABLE:
        raise ValueError('bank publication requires serializable transaction')
    cur.execute('SAVEPOINT bank_source_publish')
    try:
        _, rows = _locked_legacy(cur, import_id)
        batch_id = _identifier('batch', import_id)
        cur.execute('SELECT status,revision,published_source_hash FROM source_batches WHERE batch_id=%s FOR UPDATE', (str(batch_id),))
        state = cur.fetchone()
        if not state or state['revision'] != expected_revision:
            raise ValueError('bank source revision changed')
        if state['status'] == 'published':
            if not hmac.compare_digest(state['published_source_hash'], source_snapshot_sha256):
                raise ValueError('published bank source snapshot differs')
            replayed = True
        else:
            snap = lock_source_review(cur, batch_id=batch_id, expected_revision=expected_revision)
            if snap['snapshot']['blockers'] or not hmac.compare_digest(snap['source_snapshot_sha256'], source_snapshot_sha256):
                raise ValueError('bank source review changed or incomplete')
            replayed = False
        cur.execute('SELECT source_item_id,source_uid,raw_fields,parsed_fields,review_status FROM source_items WHERE batch_id=%s', (str(batch_id),))
        items = {r['source_uid']: r for r in cur.fetchall()}
        if set(items) != {r['source_uid'] for r in rows}:
            raise ValueError('bank source rows changed')
        records = []
        for row in rows:
            item = items[row['source_uid']]
            matches = _matches(cur, row['id'])
            cases = _confirmed_cases(cur, row['id'])
            evidence = _evidence(row, matches, cases)
            if (item['review_status'] != 'confirmed' or item['raw_fields'] != evidence or
                item['parsed_fields'] != evidence or row['currency'] != 'CNY'):
                raise ValueError('bank source requires unchanged confirmed CNY rows')
            cur.execute("""SELECT * FROM pams_cashflow_fact WHERE source_table=%s AND source_id=%s FOR UPDATE""",
                        (SOURCE_TABLE, str(row['id'])))
            fact = cur.fetchone()
            cur.execute('SELECT 1 FROM pams_cashflow_fact WHERE source_table=%s AND source_uid=%s AND source_id<>%s LIMIT 1',
                        (SOURCE_TABLE, row['source_uid'], str(row['id'])))
            if cur.fetchone():
                raise ValueError('duplicate bank source identity')
            if fact is None:
                if replayed:
                    raise ValueError('published bank fact missing')
                if cases:
                    raise ValueError('confirmed bank reconciliation requires explicit fact mapping')
                value = build_cashflow_fact(row, matches[0] if matches else None)
                if insert_cashflow_facts(cur, [value]) != 1:
                    raise ValueError('bank fact source conflict')
                cur.execute('SELECT * FROM pams_cashflow_fact WHERE source_table=%s AND source_id=%s FOR UPDATE',
                            (SOURCE_TABLE, str(row['id'])))
                fact = cur.fetchone()
            if (fact['source_uid'] != row['source_uid'] or fact['event_date'] != row['transaction_date'] or
                fact['account_code'] != row['account_code'] or fact['amount'] != row['amount'] or
                fact['currency'] != row['currency'] or
                str(fact['source_item_id'] or '') not in ('', str(item['source_item_id']))):
                raise ValueError('bank fact conflicts with source')
            if replayed and str(fact['source_item_id'] or '') != str(item['source_item_id']):
                raise ValueError('published bank association differs')
            if not replayed and fact['source_item_id'] is None:
                cur.execute('UPDATE pams_cashflow_fact SET source_item_id=%s WHERE cashflow_id=%s AND source_item_id IS NULL',
                            (str(item['source_item_id']), fact['cashflow_id']))
                if cur.rowcount != 1:
                    raise ValueError('bank association conflict')
            records.append((str(item['source_item_id']), fact['cashflow_id']))
        cur.execute('SELECT source_item_id,cashflow_id FROM pams_cashflow_fact WHERE source_item_id IN (SELECT source_item_id FROM source_items WHERE batch_id=%s)', (str(batch_id),))
        if sorted(records) != sorted((str(r['source_item_id']), r['cashflow_id']) for r in cur.fetchall()):
            raise ValueError('unexpected bank fact association')
        for table in ('pams_investment_trade_fact', 'pams_holdings_snapshot'):
            cur.execute(f'SELECT 1 FROM {table} WHERE source_item_id IN '
                        '(SELECT source_item_id FROM source_items WHERE batch_id=%s) LIMIT 1', (str(batch_id),))
            if cur.fetchone():
                raise ValueError('unexpected bank fact association')
        if not replayed:
            cur.execute("UPDATE source_batches SET status='published',published_source_hash=%s,updated_at=now() WHERE batch_id=%s",
                        (source_snapshot_sha256, str(batch_id)))
        cur.execute('RELEASE SAVEPOINT bank_source_publish')
        return {'replayed': replayed, 'records': records}
    except Exception:
        cur.execute('ROLLBACK TO SAVEPOINT bank_source_publish')
        cur.execute('RELEASE SAVEPOINT bank_source_publish')
        raise
