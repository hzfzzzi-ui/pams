"""Source-specific review policy; neither OCR confidence nor UI flags approve facts."""
import hashlib
import json


DECISIONS = {"duplicate_decision", "counter_account_code", "bank_cashflow_id", "derived_cashflow_id"}


def editable_fields(row):
    source = row.get("source", {})
    return not source.get("file_id") or source.get("origin") == "ocr"


def image_fingerprint(batch, file_id):
    rows = [{"row_id": row["row_id"], "fields": row["fields"], "excluded": bool(row.get("excluded"))}
            for row in batch["rows"] if row.get("source", {}).get("file_id") == file_id]
    return hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def image_reviewed(batch, file_id):
    file = next((f for f in batch.get("files", []) if f["file_id"] == file_id), None)
    return bool(file and file["content_type"].startswith("image/") and
                file.get("metadata", {}).get("review_fingerprint") == image_fingerprint(batch, file_id))
