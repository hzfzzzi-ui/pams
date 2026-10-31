"""Isolated projection of an existing draft into the proposed source layer.

This adapter never publishes facts, changes an existing draft or commits. It is
not wired into any production entry point. The caller owns the transaction.
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from uuid import UUID

from domain.imports.unified_source import add_item, attach_file, create_batch


def stage_legacy_draft(cur, batch: dict, files: list[dict], *, institution: str) -> None:
    """Stage one locked legacy draft; reject cases without lossless evidence.

    The caller must obtain the legacy batch using FOR UPDATE and must rollback
    the whole transaction on any failure. No preflight or publication approval
    is implied. Existing source IDs remain stable; review is deliberately reset
    to pending except explicit exclusions.
    """
    if batch.get('status') != 'draft':
        raise ValueError('only unpublished draft batches may be staged')
    batch_id = UUID(str(batch['batch_id']))
    account = str(batch.get('account_code') or '')
    kind = batch.get('kind')
    mode = batch.get('mode')
    revision = batch.get('revision')
    if not account or not isinstance(institution, str) or not institution.strip() or kind not in {'holding', 'trade', 'cash'} or mode not in {'manual', 'upload'}:
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
    seen, referenced = set(), set()
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
            referenced.add(file_id)
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
    if set(file_index) != referenced:
        raise ValueError('files without source items require separate metadata migration')

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
    cur.execute('UPDATE public.source_batches SET revision=%s, completeness=%s WHERE batch_id=%s',
                (revision, batch.get('completeness', 'unknown'), str(batch_id)))
