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
