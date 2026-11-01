"""Private import drafts and evidence. No financial writes or runtime DDL."""
from __future__ import annotations

import os
from uuid import UUID
from decimal import Decimal
from typing import Any

from domain.imports.account_import_types import asset_type
from domain.imports.account_import_parse import FIELDS as PARSE_FIELDS
from domain.imports import account_import_statement as statement

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
    from domain.imports.isolation import guarded_connect

    return guarded_connect(host=os.environ.get("PGHOST", "pams-postgres"),
                            port=int(os.environ.get("PGPORT", "5432")),
                            dbname=os.environ.get("PGDATABASE", "pams"),
                            user=os.environ.get("PGUSER", "pams"), password=os.environ.get("PGPASSWORD", ""),
                            connect_timeout=5)


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
        from domain.imports.account_import_ocr import parse_image
        return parse_image(content, profile, kind)
    from domain.imports.account_import_parse import parse_document
    return parse_document(content, filename, profile, kind, mapping)


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


def editable(result: dict, revision: int | None = None):
    if result["status"] != "draft":
        raise ImportProblem("not_draft", "此批次不可修改；更正请新建批次并重新核对", 409)
    if revision is not None and result["revision"] != revision:
        raise ImportProblem("revision_conflict", "批次已更新，请重新读取后再编辑", 409)
