"""Repository for the isolated candidate source schema.

Callers own the transaction. This module does not publish financial facts or
replace the existing import pipeline.
"""
from hashlib import sha256
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
             external_id: str | None = None, location: str | None = None,
             raw_fields: dict[str, Any] | None = None,
             parsed_fields: dict[str, Any] | None = None) -> None:
    cur.execute(
        """INSERT INTO public.source_items
           (source_item_id, batch_id, file_id, source_uid, external_id, location, parse_method,
            record_hash, raw_fields, parsed_fields)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (str(item_id), str(batch_id), str(file_id) if file_id else None,
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
             SELECT b.batch_id FROM public.source_batches b
             WHERE b.batch_id = %s AND b.revision = %s
               AND b.status IN ('draft', 'review')
               AND EXISTS (SELECT 1 FROM public.source_items i
                           WHERE i.batch_id = b.batch_id AND i.source_item_id = %s)
             FOR UPDATE
           ), changed AS (
             UPDATE public.source_items i
             SET parsed_fields = %s, review_status = %s, review_note = %s, updated_at = now()
             FROM locked l WHERE i.batch_id = l.batch_id AND i.source_item_id = %s
             RETURNING i.batch_id
           )
           UPDATE public.source_batches b SET revision = b.revision + 1, updated_at = now()
           FROM changed c WHERE b.batch_id = c.batch_id RETURNING b.revision""",
        (str(batch_id), expected_revision, str(item_id), Json(parsed_fields), review_status,
         review_note, str(item_id)),
    )
    row = cur.fetchone()
    if row is None:
        raise ValueError('source batch or item missing, stale, or not editable')
    return int(row[0])
