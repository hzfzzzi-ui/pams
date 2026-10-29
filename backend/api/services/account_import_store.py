"""Private import drafts and evidence. No financial writes or runtime DDL."""
from __future__ import annotations

import hashlib
import os
import json
from uuid import UUID, uuid4
from decimal import Decimal
from typing import Any

import psycopg2
from psycopg2.extras import Json
from services.account_import_types import asset_type
from services.account_import_parse import FIELDS as PARSE_FIELDS
from services import account_import_statement as statement
from services.account_import_review import DECISIONS, editable_fields, image_fingerprint

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_BATCH_BYTES = 30 * 1024 * 1024
MAX_FILES = 10
MAX_ROWS = 2000
PROFILES: dict[str, dict[str, Any]] = {
    "tonghuashun": {"label": "同花顺", "default_asset_type": "stock", "currency": "CNY",
        "trade_actions": {"stock": ["buy", "sell", "dividend", "fee"], "fund": ["buy", "sell", "dividend", "fee"]}},
    "tencent_licaitong": {"label": "腾讯理财通", "default_asset_type": "fund", "currency": "CNY",
        "trade_actions": {"fund": ["subscribe", "redeem", "dividend", "fee"]}},
}
FIELDS = set(PARSE_FIELDS)


class ImportProblem(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status


def get_conn():
    return psycopg2.connect(host=os.environ.get("PGHOST", "pams-postgres"),
                            port=int(os.environ.get("PGPORT", "5432")),
                            dbname=os.environ.get("PGDATABASE", "pams"),
                            user=os.environ.get("PGUSER", "pams"), password=os.environ.get("PGPASSWORD", ""),
                            connect_timeout=5)


def schema_ready(cur) -> bool:
    cur.execute("SELECT to_regclass('pams_account_import_batch') IS NOT NULL AND "
                "to_regclass('pams_account_import_file') IS NOT NULL AND "
                "to_regclass('pams_account_import_template') IS NOT NULL AND "
                "to_regclass('pams_account_import_audit') IS NOT NULL AS ready")
    return bool(cur.fetchone()["ready"])


def norm(value):
    if isinstance(value, dict):
        return {k: norm(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [norm(v) for v in value]
    return value.isoformat() if hasattr(value, "isoformat") else str(value) if isinstance(value, (UUID, Decimal)) else value


def parse(content, filename, profile, kind, mapping=None):
    if statement.is_statement(profile):
        return statement.parse(content, filename, profile, kind, mapping)
    if filename.rsplit(".", 1)[-1].lower() in {"png", "jpg", "jpeg"}:
        from services.account_import_ocr import parse_image
        return parse_image(content, profile, kind)
    from services.account_import_parse import parse_document
    return parse_document(content, filename, profile, kind, mapping)


def audit(cur, batch_id: str, event: str, details: dict):
    cur.execute("INSERT INTO pams_account_import_audit(audit_id,batch_id,event,details) VALUES (%s,%s,%s,%s)",
                (str(uuid4()), batch_id, event, Json(details)))


def context(cur) -> dict:
    cur.execute("SELECT account_code,display_name,base_currency FROM pams_accounts WHERE active ORDER BY account_code")
    all_accounts = [dict(r) for r in cur.fetchall()]
    sources = {**{key: statement.public_profile(key) for key in statement.profiles()},
               **{key: {**value, "profile": key, "kinds": ["trade", "holding"]} for key, value in PROFILES.items()}}
    accounts = [{**sources[r["account_code"]], "account_code": r["account_code"],
                 "label": r["display_name"], "currency": r["base_currency"]}
                for r in all_accounts if r["account_code"] in sources]
    return {"accounts": accounts, "counter_accounts": norm(all_accounts),
            "profiles": [{"profile": key, **value} for key, value in PROFILES.items()]}


def assets(cur, query: str) -> dict:
    query = query.strip()[:80]
    if len(query) < 2:
        return {"rows": []}
    cur.execute("""SELECT asset_id,primary_symbol AS symbol,canonical_name AS name,asset_type,currency
        FROM pams_asset_master WHERE status='active' AND currency='CNY'
          AND (primary_symbol ILIKE %s OR canonical_name ILIKE %s)
        ORDER BY (primary_symbol=%s) DESC,canonical_name LIMIT 25""", ("%" + query + "%", "%" + query + "%", query))
    rows = [{**dict(r), "source_asset_type": r["asset_type"], "asset_type": asset_type(r["asset_type"])} for r in cur.fetchall()]
    return {"rows": norm(rows)}


def batch(cur, batch_id: str, *, lock: bool = False, files: bool = True) -> dict:
    cur.execute("SELECT * FROM pams_account_import_batch WHERE batch_id=%s" + (" FOR UPDATE" if lock else ""), (batch_id,))
    row = cur.fetchone()
    if not row:
        raise ImportProblem("batch_missing", "找不到导入批次", 404)
    result = dict(row)
    result["rows"] = result.pop("rows_json")
    for item in result["rows"]:
        item["can_edit_fields"] = editable_fields(item)
    result.pop("preview_token", None)
    if files:
        cur.execute("SELECT file_id,filename,content_type,sha256,metadata FROM pams_account_import_file "
                    "WHERE batch_id=%s ORDER BY created_at,file_id", (batch_id,))
        result["files"] = [dict(r) for r in cur.fetchall()]
    return norm(result)


def editable(result: dict, revision: int | None = None):
    if result["status"] != "draft":
        raise ImportProblem("not_draft", "此批次不可修改；更正请新建批次并重新核对", 409)
    if revision is not None and result["revision"] != revision:
        raise ImportProblem("revision_conflict", "批次已更新，请重新读取后再编辑", 409)


def create(cur, account_code: str, mode: str, kind: str, completeness: str) -> dict:
    if account_code not in PROFILES and not (kind == "cash" and statement.is_statement(account_code)):
        raise ImportProblem("unsupported_account", "该账户不在本轮投资导入范围")
    cur.execute("SELECT 1 FROM pams_accounts WHERE account_code=%s AND active", (account_code,))
    if not cur.fetchone():
        raise ImportProblem("account_missing", "账户不存在或已停用")
    key = str(uuid4())
    cur.execute("""INSERT INTO pams_account_import_batch(batch_id,account_code,mode,kind,completeness)
        VALUES (%s,%s,%s,%s,%s)""", (key, account_code, mode, kind, completeness))
    audit(cur, key, "created", {"mode": mode, "kind": kind})
    return batch(cur, key)


def save(cur, batch_id: str, revision: int, rows: list[dict], completeness: str) -> dict:
    existing = batch(cur, batch_id, lock=True)
    editable(existing, revision)
    if len(rows) > MAX_ROWS:
        raise ImportProblem("too_many_rows", "每批最多2000条记录")
    if len(json.dumps(rows, ensure_ascii=False).encode()) > 4 * 1024 * 1024:
        raise ImportProblem("draft_size", "候选记录超过4MB", 413)
    previous = {row["row_id"]: row for row in existing["rows"]}
    clean, seen = [], set()
    for row in rows:
        fields = row.get("fields")
        if not isinstance(fields, dict) or set(fields) - FIELDS:
            raise ImportProblem("invalid_fields", "记录包含未知字段")
        if any(v is not None and (not isinstance(v, str) or len(v) > 500) for v in fields.values()):
            raise ImportProblem("invalid_value", "字段必须为文本或空值；金额不能经过浮点转换")
        row_id = row.get("row_id")
        if row_id and row_id not in previous:
            raise ImportProblem("unknown_row", "候选记录不属于此批次")
        row_id = row_id or str(uuid4())
        if row_id in seen:
            raise ImportProblem("duplicate_row_id", "同一候选记录不能重复提交")
        seen.add(row_id)
        old = previous.get(row_id, {})
        source = old.get("source", {"origin": "manual"})
        if not old and row.get("image_file_id"):
            image_id = str(UUID(str(row["image_file_id"])))
            image = get_file(cur, batch_id, image_id)
            if not image["content_type"].startswith("image/"):
                raise ImportProblem("not_image", "只能为本批截图补充核对记录")
            source = {"origin": "ocr", "file_id": image_id, "file_sha256": image["sha256"], "human_added": True}
        if old and not editable_fields(old):
            if any(fields.get(k) != old["fields"].get(k) for k in (set(fields) | set(old["fields"])) - DECISIONS):
                raise ImportProblem("file_row_readonly", "文件记录不能逐行修改；请调整列映射后重新解析")
        for flag in ("acknowledged", "excluded"):
            if flag in row and not isinstance(row[flag], bool):
                raise ImportProblem("invalid_flag", "核对状态必须为布尔值")
        clean.append({"row_id": row_id, "fields": fields,
                      "source": source,
                      "issues": old.get("issues", []),
                      "acknowledged": row.get("acknowledged", False), "excluded": row.get("excluded", False)})
    if any(r.get("source", {}).get("file_id") and r["row_id"] not in seen for r in existing["rows"]):
        raise ImportProblem("file_row_readonly", "来源记录不能删除；请明确排除并保留原件")
    cur.execute("""UPDATE pams_account_import_batch SET rows_json=%s,completeness=%s,
        revision=revision+1,preview_json=NULL,preview_token=NULL,updated_at=now() WHERE batch_id=%s""",
                (Json(clean), completeness, batch_id))
    changes = [{"row_id": row["row_id"], "before": previous.get(row["row_id"]), "after": row}
               for row in clean if previous.get(row["row_id"]) != row]
    changes += [{"row_id": key, "before": value, "after": None}
                for key, value in previous.items() if key not in seen]
    audit(cur, batch_id, "draft_saved", {"from_revision": revision, "rows": len(clean), "changes": changes})
    return batch(cur, batch_id)


def review_image(cur, batch_id: str, file_id: str, revision: int):
    result = batch(cur, batch_id, lock=True)
    editable(result, revision)
    file = get_file(cur, batch_id, file_id)
    if not file["content_type"].startswith("image/"):
        raise ImportProblem("not_image", "只有截图使用逐图核对")
    members = [r for r in result["rows"] if r.get("source", {}).get("file_id") == file_id]
    if not members:
        raise ImportProblem("empty_image", "截图尚未识别出记录，不能标记为已核对")
    for row in members:
        row["acknowledged"] = True
    metadata = {**file["metadata"], "review_fingerprint": image_fingerprint(result, file_id)}
    cur.execute("UPDATE pams_account_import_file SET metadata=%s WHERE file_id=%s", (Json(metadata), file_id))
    cur.execute("UPDATE pams_account_import_batch SET rows_json=%s,revision=revision+1,preview_json=NULL,preview_token=NULL,updated_at=now() WHERE batch_id=%s",
                (Json(result["rows"]), batch_id))
    audit(cur, batch_id, "image_reviewed", {"file_id": file_id, "rows": len(members), "fingerprint": metadata["review_fingerprint"]})
    return batch(cur, batch_id)


def list_batches(cur, limit: int) -> dict:
    cur.execute("""SELECT batch_id,account_code,mode,kind,completeness,status,revision,
        jsonb_array_length(rows_json) AS row_count,created_at,updated_at
        FROM pams_account_import_batch ORDER BY created_at DESC LIMIT %s""", (limit,))
    return {"rows": norm([dict(r) for r in cur.fetchall()])}


def get_file(cur, batch_id: str, file_id: str) -> dict:
    cur.execute("SELECT * FROM pams_account_import_file WHERE batch_id=%s AND file_id=%s", (batch_id, file_id))
    row = cur.fetchone()
    if not row:
        raise ImportProblem("file_missing", "找不到本批次原始资料", 404)
    return dict(row)


def _append_parsed(cur, result: dict, file_id: str, sha256: str, parsed: dict):
    rows = [r for r in result["rows"] if r.get("source", {}).get("file_id") != file_id]
    for row in parsed["rows"]:
        rows.append({"row_id": str(uuid4()), "fields": row["fields"],
                     "source": {**row.get("source", {}), "file_id": file_id, "file_sha256": sha256,
                                "origin": parsed.get("origin", "file")},
                     "issues": row.get("issues", []), "acknowledged": False, "excluded": bool(row.get("excluded", False))})
    if len(rows) > MAX_ROWS:
        raise ImportProblem("too_many_rows", "每批最多2000条记录")
    cur.execute("""UPDATE pams_account_import_batch SET rows_json=%s,revision=revision+1,
        preview_json=NULL,preview_token=NULL,updated_at=now() WHERE batch_id=%s""", (Json(rows), result["batch_id"]))


def upload(cur, batch_id: str, filename: str, content: bytes, content_type: str) -> dict:
    result = batch(cur, batch_id, lock=True)
    editable(result)
    if not filename or len(filename) > 180 or any(c in filename for c in ("/", "\\", "\x00", "\r", "\n")):
        raise ImportProblem("invalid_filename", "文件名无效")
    if not 0 < len(content) <= MAX_FILE_BYTES:
        raise ImportProblem("file_size", "文件为空或超过10MB", 413)
    digest = hashlib.sha256(content).hexdigest()
    cur.execute("SELECT file_id FROM pams_account_import_file WHERE batch_id=%s AND sha256=%s", (batch_id, digest))
    if cur.fetchone():
        return result
    cur.execute("SELECT count(*) AS count,COALESCE(sum(octet_length(content)),0) AS size "
                "FROM pams_account_import_file WHERE batch_id=%s", (batch_id,))
    limits = cur.fetchone()
    if limits["count"] >= MAX_FILES or limits["size"] + len(content) > MAX_BATCH_BYTES:
        raise ImportProblem("batch_size", "每批最多10个文件、合计30MB", 413)
    cur.execute("SELECT mapping FROM pams_account_import_template WHERE account_code=%s AND kind=%s",
                (result["account_code"], result["kind"]))
    template = cur.fetchone()
    parsed = parse(content, filename, result["account_code"], result["kind"],
                            template["mapping"] if template else None)
    suffix = filename.rsplit(".", 1)[-1].lower()
    safe_type = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "csv": "text/plain",
                 "tsv": "text/plain", "xls": "text/plain", "pdf": "application/pdf", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}.get(suffix)
    if not safe_type:
        raise ImportProblem("file_type", "仅支持CSV、TSV、同花顺文本XLS、XLSX、PNG、JPEG及账单PDF")
    key = str(uuid4())
    metadata = {k: v for k, v in parsed.items() if k != "rows"}
    cur.execute("""INSERT INTO pams_account_import_file(file_id,batch_id,filename,content_type,sha256,content,metadata)
        VALUES (%s,%s,%s,%s,%s,%s,%s)""", (key, batch_id, filename, safe_type, digest, content, Json(metadata)))
    _append_parsed(cur, result, key, digest, parsed)
    audit(cur, batch_id, "file_parsed", {"file_id": key, "sha256": digest, "rows": len(parsed["rows"])})
    return batch(cur, batch_id)


def reparse(cur, batch_id: str, file_id: str, mapping: dict, kind: str | None = None) -> dict:
    result = batch(cur, batch_id, lock=True)
    editable(result)
    if kind is not None and kind != result["kind"]:
        raise ImportProblem("kind_mismatch", "重新解析不能改变批次资料类型")
    file = get_file(cur, batch_id, file_id)
    parsed = parse(bytes(file["content"]), file["filename"], result["account_code"], result["kind"], mapping)
    _append_parsed(cur, result, file_id, file["sha256"], parsed)
    cur.execute("UPDATE pams_account_import_file SET metadata=%s WHERE file_id=%s", (Json({k: v for k, v in parsed.items() if k != "rows"}), file_id))
    cur.execute("""INSERT INTO pams_account_import_template(account_code,kind,mapping) VALUES (%s,%s,%s)
        ON CONFLICT(account_code,kind) DO UPDATE SET mapping=EXCLUDED.mapping,updated_at=now()""",
                (result["account_code"], result["kind"], Json(mapping)))
    audit(cur, batch_id, "mapping_applied", {"file_id": file_id, "mapping": mapping,
          "replaced_rows": [r for r in result["rows"] if r.get("source", {}).get("file_id") == file_id]})
    return batch(cur, batch_id)
