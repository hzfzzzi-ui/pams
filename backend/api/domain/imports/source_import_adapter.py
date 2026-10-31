"""Isolated projection of an existing draft into the proposed source layer.

This adapter stages and publishes candidates only when explicitly called. It is
not wired into any production entry point. The caller owns the transaction.
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import hmac
import json
from uuid import UUID

from domain.imports.unified_source import add_item, attach_file, create_batch, lock_source_review
from domain.imports import account_import_store as store
from domain.imports import account_import_publish as publisher
from domain.imports.account_import_review import image_reviewed


def _prepare_legacy_draft(batch: dict, files: list[dict]):
    if batch.get('status') != 'draft':
        raise ValueError('only unpublished draft batches may be staged')
    batch_id = UUID(str(batch['batch_id']))
    account = str(batch.get('account_code') or '')
    kind = batch.get('kind')
    mode = batch.get('mode')
    revision = batch.get('revision')
    if not account or kind not in {'holding', 'trade', 'cash'} or mode not in {'manual', 'upload'}:
        raise ValueError('invalid account, business type or acquisition method')
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise ValueError('invalid legacy revision')
    rows = batch.get('rows')
    if not isinstance(rows, list):
        raise ValueError('legacy draft rows required')

    file_index = {}
    for file in files:
        file_id = UUID(str(file['file_id']))
        if file_id in file_index or str(file.get('batch_id')) != str(batch_id):
            raise ValueError('duplicate or cross-batch file')
        content = bytes(file['content'])
        digest = sha256(content).hexdigest()
        if digest != str(file.get('sha256') or ''):
            raise ValueError('file content hash mismatch')
        suffix = str(file.get('filename') or '').rsplit('.', 1)[-1].lower()
        fmt = 'jpeg' if suffix == 'jpg' else suffix
        if fmt not in {'csv', 'tsv', 'xlsx', 'xls', 'pdf', 'png', 'jpeg'}:
            raise ValueError('unsupported legacy file format')
        file_index[file_id] = (file, content, fmt)

    prepared = []
    seen = set()
    for row in rows:
        item_id = UUID(str(row['row_id']))
        if item_id in seen:
            raise ValueError('duplicate row identity')
        seen.add(item_id)
        source = deepcopy(row.get('source'))
        fields = deepcopy(row.get('fields'))
        if not isinstance(source, dict) or not isinstance(fields, dict):
            raise ValueError('source and candidate fields must be objects')
        for flag in ('acknowledged', 'excluded'):
            if flag in row and not isinstance(row[flag], bool):
                raise ValueError('invalid review flag')
        file_id = UUID(str(source['file_id'])) if source.get('file_id') else None
        if file_id:
            if file_id not in file_index or source.get('file_sha256') != file_index[file_id][0].get('sha256'):
                raise ValueError('source file missing or hash mismatch')
        origin = source.get('origin')
        if origin == 'ocr' and file_id:
            method = 'ocr'
        elif origin == 'file' and file_id:
            method = 'template'
        elif origin == 'manual' and not file_id:
            method = 'manual'
        else:
            raise ValueError('unknown or inconsistent source origin')
        evidence = {'legacy_row_id': str(item_id), 'source': source}
        if file_id:
            evidence['legacy_file_metadata'] = deepcopy(file_index[file_id][0].get('metadata') or {})
        else:
            evidence['manual_draft_fields'] = fields
        candidate = {'fields': fields, 'issues': deepcopy(row.get('issues') or []),
                     'acknowledged': row.get('acknowledged', False), 'excluded': row.get('excluded', False)}
        try:
            encoded = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
        except (TypeError, ValueError) as exc:
            raise ValueError('source evidence is not JSON-safe') from exc
        prepared.append((item_id, file_id, method, evidence, candidate, sha256(encoded).hexdigest()))
    return batch_id, account, kind, mode, revision, file_index, prepared


def stage_legacy_draft(cur, batch: dict, files: list[dict], *, institution: str) -> None:
    """Stage a locked draft without implying review or publication approval."""
    if not isinstance(institution, str) or not institution.strip():
        raise ValueError('institution required')
    batch_id, account, kind, mode, revision, file_index, prepared = _prepare_legacy_draft(batch, files)

    create_batch(cur, batch_id=batch_id, account_code=account, business_type=kind,
                 institution=institution, acquisition_method=mode)
    for file_id, (file, content, fmt) in file_index.items():
        attach_file(cur, file_id=file_id, batch_id=batch_id, filename=file['filename'],
                    file_format=fmt, content_type=file['content_type'], content=content)
    for item_id, file_id, method, evidence, candidate, digest in prepared:
        source = evidence['source']
        add_item(cur, item_id=item_id, batch_id=batch_id, source_uid=str(item_id), file_id=file_id,
                 location=str(source['row']) if source.get('row') is not None else None,
                 parse_method=method, record_hash=digest, raw_fields=evidence, parsed_fields=candidate)
        if candidate['excluded']:
            cur.execute("UPDATE public.source_items SET review_status='excluded' WHERE source_item_id=%s", (str(item_id),))
    cur.execute('UPDATE public.source_batches SET revision=GREATEST(revision,%s), completeness=%s WHERE batch_id=%s',
                (revision, batch.get('completeness', 'unknown'), str(batch_id)))


def ensure_legacy_staged(cur, batch_id: str) -> bool:
    """Stage an existing editable draft on first isolated use under its row lock."""
    legacy = store.batch(cur, str(batch_id), lock=True)
    store.editable(legacy)
    cur.execute('SELECT 1 FROM public.source_batches WHERE batch_id=%s FOR UPDATE', (str(batch_id),))
    if cur.fetchone():
        return False
    cur.execute('SELECT * FROM pams_account_import_file WHERE batch_id=%s', (str(batch_id),))
    files = [dict(row) for row in cur.fetchall()]
    stage_legacy_draft(cur, legacy, files, institution=legacy['account_code'])
    sync_legacy_draft(cur, legacy, action='migration')
    return True


def sync_legacy_draft(cur, batch: dict, *, action: str) -> None:
    """Synchronize a legacy draft inside its write transaction, without publishing."""
    if action not in {'create', 'save', 'upload', 'reparse', 'review_image', 'migration'}:
        raise ValueError('unsupported source synchronization action')
    batch_id = str(batch['batch_id'])
    cur.execute('SELECT * FROM pams_account_import_file WHERE batch_id=%s', (batch_id,))
    files = [dict(row) for row in cur.fetchall()]
    _, account, kind, mode, _, file_index, prepared = _prepare_legacy_draft(batch, files)
    cur.execute('SELECT * FROM public.source_batches WHERE batch_id=%s FOR UPDATE', (batch_id,))
    existing = cur.fetchone()
    if not existing:
        if action != 'create':
            raise ValueError('source draft missing; cannot silently recreate evidence')
        stage_legacy_draft(cur, batch, files, institution=account)
        return
    if (existing['status'] not in {'draft', 'review'} or existing['account_code'] != account or
        existing['business_type'] != kind or existing['acquisition_method'] != mode):
        raise ValueError('source draft attributes changed')
    cur.execute('SELECT * FROM public.source_files WHERE batch_id=%s', (batch_id,))
    old_files = {str(row['file_id']): row for row in cur.fetchall()}
    if set(old_files) - {str(key) for key in file_index}:
        raise ValueError('source file missing from legacy draft')
    for file_id, (file, content, fmt) in file_index.items():
        old = old_files.get(str(file_id))
        if old:
            if (old['sha256'] != file['sha256'] or old['filename'] != file['filename'] or
                old['content_type'] != file['content_type'] or bytes(old['content']) != content):
                raise ValueError('source file differs from legacy draft')
        else:
            attach_file(cur, file_id=file_id, batch_id=UUID(batch_id), filename=file['filename'],
                        file_format=fmt, content_type=file['content_type'], content=content)
    cur.execute('SELECT * FROM public.source_items WHERE batch_id=%s', (batch_id,))
    old_items = {str(row['source_item_id']): row for row in cur.fetchall()}
    new_ids = {str(value[0]) for value in prepared}
    for item_id in set(old_items) - new_ids:
        cur.execute('DELETE FROM public.source_items WHERE source_item_id=%s AND batch_id=%s', (item_id, batch_id))
    for item_id, file_id, method, evidence, candidate, digest in prepared:
        old = old_items.get(str(item_id))
        if old:
            if (str(old['file_id'] or '') != str(file_id or '') or old['parse_method'] != method or
                old['raw_fields'].get('source') != evidence['source']):
                raise ValueError('existing source identity changed')
            status = old['review_status']
        else:
            add_item(cur, item_id=item_id, batch_id=UUID(batch_id), source_uid=str(item_id), file_id=file_id,
                     location=str(evidence['source']['row']) if evidence['source'].get('row') is not None else None,
                     parse_method=method, record_hash=digest, raw_fields=evidence, parsed_fields=candidate)
            status = 'pending'
        if candidate['excluded']:
            status = 'excluded'
        elif method == 'ocr':
            status = 'confirmed' if candidate['acknowledged'] and image_reviewed(batch, str(file_id)) else 'pending'
        elif action == 'save':
            status = 'confirmed'
        elif old and (old['parsed_fields'] != candidate or status == 'excluded'):
            status = 'pending'
        if not old or old['parsed_fields'] != candidate or old['review_status'] != status:
            from psycopg2.extras import Json
            cur.execute('UPDATE public.source_items SET parsed_fields=%s,review_status=%s,review_note=%s,updated_at=now() WHERE source_item_id=%s AND batch_id=%s',
                        (Json(candidate), status, 'legacy explicit review' if status == 'confirmed' else None,
                         str(item_id), batch_id))
    cur.execute('UPDATE public.source_batches SET completeness=%s,revision=revision+1,updated_at=now() WHERE batch_id=%s',
                (batch['completeness'], batch_id))


def _verified_projection(cur, batch_id, legacy_revision, source_revision, *, allow_pending=False):
    """Lock both drafts and fail closed if their reviewed contents diverge."""
    legacy = store.batch(cur, str(batch_id), lock=True)
    store.editable(legacy, legacy_revision)
    result = lock_source_review(cur, batch_id=UUID(str(batch_id)), expected_revision=source_revision)
    snap = result['snapshot']
    batch = snap['batch']
    if (batch['account_code'] != legacy['account_code'] or batch['business_type'] != legacy['kind'] or
        batch['acquisition_method'] != legacy['mode'] or batch['completeness'] != legacy['completeness']):
        raise ValueError('source batch attributes differ from legacy draft')
    if snap['blockers'] and (not allow_pending or 'file_hash_mismatch' in snap['blockers']):
        raise ValueError('source review blocked: ' + ', '.join(snap['blockers']))
    legacy_rows = {str(row['row_id']): row for row in legacy['rows']}
    source_rows = {str(row['source_item_id']): row for row in snap['items']}
    if len(legacy_rows) != len(legacy['rows']) or set(legacy_rows) != set(source_rows):
        raise ValueError('source and legacy row identities differ')
    for key, old in legacy_rows.items():
        new = source_rows[key]
        expected_status = 'excluded' if old.get('excluded', False) else 'confirmed'
        if new['review_status'] != expected_status and not (allow_pending and new['review_status'] == 'pending' and expected_status == 'confirmed'):
            raise ValueError('source review decision differs from legacy draft')
        evidence = new['raw_fields']
        candidate = new['parsed_fields']
        expected = {'fields': old.get('fields'), 'issues': old.get('issues') or [],
                    'excluded': old.get('excluded', False), 'acknowledged': old.get('acknowledged', False)}
        if evidence.get('source') != old.get('source') or candidate != expected:
            raise ValueError('source evidence or candidate differs from legacy draft')
    old_files = {str(f['file_id']): (str(f['sha256']), f['filename'], f['content_type']) for f in legacy['files']}
    new_files = {str(f['file_id']): (str(f['sha256']), f['filename'], f['content_type']) for f in snap['files']}
    if old_files != new_files:
        raise ValueError('source files differ from legacy draft')
    metadata = {str(f['file_id']): f.get('metadata') or {} for f in legacy['files']}
    for item in snap['items']:
        original = item['raw_fields'].get('legacy_file_metadata') or {}
        current = metadata[str(item['file_id'])] if item['file_id'] else {}
        if item['file_id'] and {k: v for k, v in original.items() if k != 'review_fingerprint'} != {k: v for k, v in current.items() if k != 'review_fingerprint'}:
            raise ValueError('source file metadata differs from legacy draft')
    return result


def prepare_candidate(cur, *, batch_id, legacy_revision, source_revision):
    """Prepare both snapshots in the caller's transaction; no facts are written."""
    if cur.connection.autocommit:
        raise ValueError('candidate preflight requires a transaction')
    cur.execute('SAVEPOINT candidate_source_prepare')
    try:
        source = _verified_projection(cur, batch_id, legacy_revision, source_revision, allow_pending=True)
        legacy = publisher._legacy_preview(cur, str(batch_id), legacy_revision, _source_bridge=True)
        if source['snapshot']['blockers']:
            legacy['can_publish'] = False
        if legacy['counts']['duplicates']:
            raise ValueError('duplicate source requires explicit association; not supported yet')
        cur.execute('RELEASE SAVEPOINT candidate_source_prepare')
        return {'source_snapshot_sha256': source['source_snapshot_sha256'], 'legacy_preview': legacy}
    except Exception:
        cur.execute('ROLLBACK TO SAVEPOINT candidate_source_prepare')
        cur.execute('RELEASE SAVEPOINT candidate_source_prepare')
        raise


def publish_candidate(cur, *, batch_id, legacy_revision, source_revision,
                      preview_token, source_snapshot_sha256):
    """Isolated candidate only; never commit, and roll back this operation on error."""
    if cur.connection.autocommit:
        raise ValueError('candidate publication requires a transaction')
    cur.execute('SAVEPOINT candidate_source_publish')
    try:
        legacy = store.batch(cur, str(batch_id), lock=True)
        if legacy['status'] == 'published':
            cur.execute('SELECT status, revision, published_source_hash FROM public.source_batches WHERE batch_id=%s FOR UPDATE',
                        (str(batch_id),))
            source_state = cur.fetchone()
            if (not source_state or source_state['status'] != 'published' or
                source_state['revision'] != source_revision or not source_state['published_source_hash'] or
                not hmac.compare_digest(source_state['published_source_hash'], source_snapshot_sha256)):
                raise ValueError('published source snapshot does not match')
            result = publisher._legacy_publish(cur, str(batch_id), legacy_revision, preview_token, _source_bridge=True)
            _check_complete_records(cur, batch_id, result['records'])
            for record in result['records']:
                _check_fact_link(cur, record, update=False)
            _check_all_source_links(cur, batch_id, result['records'])
            cur.execute('RELEASE SAVEPOINT candidate_source_publish')
            return result
        source = _verified_projection(cur, batch_id, legacy_revision, source_revision)
        if not hmac.compare_digest(source['source_snapshot_sha256'], source_snapshot_sha256):
            raise ValueError('source snapshot changed; repeat preflight')
        if (legacy.get('preview_json') or {}).get('counts', {}).get('duplicates'):
            raise ValueError('duplicate source requires explicit association; not supported yet')
        result = publisher._legacy_publish(cur, str(batch_id), legacy_revision, preview_token, _source_bridge=True)
        _check_complete_records(cur, batch_id, result['records'])
        for record in result['records']:
            _check_fact_link(cur, record, update=True)
        _check_all_source_links(cur, batch_id, result['records'])
        cur.execute("UPDATE public.source_batches SET status='published', published_source_hash=%s, updated_at=now() WHERE batch_id=%s",
                    (source_snapshot_sha256, str(batch_id)))
        if cur.rowcount != 1:
            raise ValueError('source batch disappeared')
        cur.execute('RELEASE SAVEPOINT candidate_source_publish')
        return result
    except Exception:
        cur.execute('ROLLBACK TO SAVEPOINT candidate_source_publish')
        cur.execute('RELEASE SAVEPOINT candidate_source_publish')
        raise


def _check_complete_records(cur, batch_id, records):
    """Every confirmed source must have exactly one primary published fact."""
    cur.execute('SELECT source_item_id, review_status FROM public.source_items WHERE batch_id=%s', (str(batch_id),))
    rows = cur.fetchall()
    if any(row['review_status'] not in {'confirmed', 'excluded'} for row in rows):
        raise ValueError('published source review is incomplete')
    expected = {str(row['source_item_id']) for row in rows if row['review_status'] == 'confirmed'}
    primary = [str(UUID(str(record['row_id']))) for record in records if record.get('role') != 'bank_leg']
    legs = [(str(UUID(str(record['row_id']))), str(record['id'])) for record in records
            if record.get('role') == 'bank_leg' and record.get('table') == 'pams_cashflow_fact']
    if (len(primary) != len(set(primary)) or set(primary) != expected or
        len(legs) != sum(record.get('role') == 'bank_leg' for record in records)):
        raise ValueError('published facts do not cover the reviewed source items')
    expected_legs = []
    for record in records:
        if record.get('table') == 'pams_investment_trade_fact' and record.get('role') != 'bank_leg':
            cur.execute('SELECT cashflow_id FROM public.pams_investment_trade_fact WHERE trade_id=%s', (record['id'],))
            trade = cur.fetchone()
            if trade is None:
                raise ValueError('published investment fact missing')
            if trade['cashflow_id'] is not None:
                expected_legs.append((str(UUID(str(record['row_id']))), str(trade['cashflow_id'])))
    if sorted(legs) != sorted(expected_legs):
        raise ValueError('published cash legs do not match investment facts')


def _check_all_source_links(cur, batch_id, records):
    """Reject extra or missing facts associated with this source batch."""
    targets = {'pams_cashflow_fact': 'cashflow_id', 'pams_investment_trade_fact': 'trade_id',
               'pams_holdings_snapshot': 'id'}
    expected = {(record['table'], str(record['id']), str(UUID(str(record['row_id'])))) for record in records}
    actual = set()
    for table, column in targets.items():
        cur.execute(f'SELECT fact.{column} AS fact_id, fact.source_item_id FROM public.{table} fact '
                    'JOIN public.source_items source ON source.source_item_id=fact.source_item_id '
                    'WHERE source.batch_id=%s', (str(batch_id),))
        actual.update((table, str(row['fact_id']), str(row['source_item_id'])) for row in cur.fetchall())
    if actual != expected:
        raise ValueError('source batch has unexpected or missing fact associations')


def _check_fact_link(cur, record, *, update):
    targets = {'pams_cashflow_fact': 'cashflow_id', 'pams_investment_trade_fact': 'trade_id',
               'pams_holdings_snapshot': 'id'}
    table = record['table']
    if table not in targets:
        raise ValueError('unexpected published fact table')
    item_id = str(UUID(str(record['row_id'])))
    if update:
        cur.execute(f'UPDATE public.{table} SET source_item_id=%s WHERE {targets[table]}=%s '
                    'AND (source_item_id IS NULL OR source_item_id=%s)',
                    (item_id, record['id'], item_id))
        if cur.rowcount != 1:
            raise ValueError('published fact source association conflict')
    else:
        cur.execute(f'SELECT 1 FROM public.{table} WHERE {targets[table]}=%s AND source_item_id=%s',
                    (record['id'], item_id))
        if cur.fetchone() is None:
            raise ValueError('published fact source association changed')
