"""Repository for the isolated candidate source schema.

Callers own the transaction. This module does not publish financial facts or
replace the existing import pipeline.
"""
from hashlib import sha256
import json
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any
from uuid import UUID

from psycopg2.extras import Json


def create_batch(cur: Any, *, batch_id: UUID, account_code: str, business_type: str,
                 institution: str, acquisition_method: str) -> None:
    cur.execute(
        """INSERT INTO public.source_batches
           (batch_id, account_code, business_type, institution, acquisition_method)
           VALUES (%s, %s, %s, %s, %s)""",
        (str(batch_id), account_code, business_type, institution, acquisition_method),
    )


def attach_file(cur: Any, *, file_id: UUID, batch_id: UUID, filename: str,
                file_format: str, content_type: str, content: bytes) -> str:
    digest = sha256(content).hexdigest()
    cur.execute(
        """INSERT INTO public.source_files
           (file_id, batch_id, filename, file_format, content_type, sha256, content)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        (str(file_id), str(batch_id), filename, file_format, content_type, digest, content),
    )
    return digest


def add_item(cur: Any, *, item_id: UUID, batch_id: UUID, source_uid: str,
             parse_method: str, record_hash: str, file_id: UUID | None = None,
             origin_file_id: UUID | None = None,
             external_id: str | None = None, location: str | None = None,
             raw_fields: dict[str, Any] | None = None,
             parsed_fields: dict[str, Any] | None = None) -> None:
    cur.execute(
        """INSERT INTO public.source_items
           (source_item_id, batch_id, file_id, origin_file_id, source_uid, external_id, location, parse_method,
            record_hash, raw_fields, parsed_fields)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (str(item_id), str(batch_id), str(file_id) if file_id else None,
         str(origin_file_id) if origin_file_id else None,
         source_uid, external_id, location, parse_method, record_hash,
         Json(raw_fields or {}), Json(parsed_fields or {})),
    )


def review_item(cur: Any, *, batch_id: UUID, item_id: UUID, expected_revision: int,
                parsed_fields: dict[str, Any], review_status: str,
                review_note: str | None = None) -> int:
    """Edit one candidate and advance its batch revision in one statement.

    Missing, stale or non-editable batches fail without changing either row.
    The caller owns the surrounding transaction and its commit or rollback.
    """
    cur.execute(
        """WITH locked AS (
             SELECT b.batch_id, b.revision FROM public.source_batches b
             WHERE b.batch_id = %s AND b.revision = %s
               AND b.status IN ('draft', 'review')
               AND EXISTS (SELECT 1 FROM public.source_items i
                           WHERE i.batch_id = b.batch_id AND i.source_item_id = %s)
             FOR UPDATE
           )
           UPDATE public.source_items i
           SET parsed_fields = %s, review_status = %s, review_note = %s, updated_at = now()
           FROM locked l WHERE i.batch_id = l.batch_id AND i.source_item_id = %s
           RETURNING l.revision + 1 AS revision""",
        (str(batch_id), expected_revision, str(item_id), Json(parsed_fields), review_status,
         review_note, str(item_id)),
    )
    row = cur.fetchone()
    if row is None:
        raise ValueError('source batch or item missing, stale, or not editable')
    return int(row['revision'] if isinstance(row, Mapping) else row[0])


def _rows(cur: Any) -> list[dict[str, Any]]:
    columns = [column[0] for column in cur.description]
    return [dict(row) if isinstance(row, Mapping) else dict(zip(columns, row)) for row in cur.fetchall()]


def _json_value(value: Any) -> str:
    if isinstance(value, (date, datetime, UUID)):
        return str(value)
    raise TypeError(f'unsupported snapshot value: {type(value).__name__}')


def lock_source_review(cur: Any, *, batch_id: UUID, expected_revision: int) -> dict[str, Any]:
    """Lock and inspect source evidence; this snapshot does not permit publication.

    The caller must keep the transaction open and use its own account/fact
    preflight before publishing. No source or financial records are modified.
    """
    cur.execute(
        """SELECT batch_id, account_code, business_type, workflow, institution, acquisition_method,
                  date_from, date_to, completeness, scope_attestation, status, revision
           FROM public.source_batches WHERE batch_id=%s FOR UPDATE""", (str(batch_id),),
    )
    batches = _rows(cur)
    if not batches:
        raise ValueError('source batch missing')
    batch = batches[0]
    if batch['revision'] != expected_revision or batch['status'] not in ('draft', 'review'):
        raise ValueError('source batch stale or not editable')

    cur.execute(
        """SELECT file_id, filename, file_format, content_type, sha256, content, current_metadata, review_fingerprint
           FROM public.source_files WHERE batch_id=%s ORDER BY file_id FOR SHARE""", (str(batch_id),),
    )
    files = _rows(cur)
    bad_file = False
    for file in files:
        content = file.pop('content')
        if sha256(bytes(content)).hexdigest() != file['sha256']:
            bad_file = True
    cur.execute(
        """SELECT source_item_id, source_order, file_id, origin_file_id, source_uid, external_id, location,
                  parse_method, raw_fields, parsed_fields, record_hash, review_status, review_note
           FROM public.source_items WHERE batch_id=%s ORDER BY source_order FOR SHARE""",
        (str(batch_id),),
    )
    items = _rows(cur)
    blockers = []
    if bad_file:
        blockers.append('file_hash_mismatch')
    if not any(item['review_status'] == 'confirmed' for item in items):
        blockers.append('no_confirmed_items')
    if any(item['review_status'] == 'pending' for item in items):
        blockers.append('pending_items')
    payload = {'batch': batch, 'files': files, 'items': items, 'blockers': blockers}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=_json_value)
    return {'snapshot': payload, 'source_snapshot_sha256': sha256(canonical.encode('utf-8')).hexdigest()}
