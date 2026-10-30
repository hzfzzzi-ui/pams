"""Explicit, opt-in publication. One transaction; no refresh, repair or history rebuild."""
from __future__ import annotations

from datetime import datetime, timezone
import hmac
import os

from psycopg2.extras import Json

from domain.imports import account_import_store as store
from domain.imports import account_import_preflight as preflight
from domain.ledger.cashflow_fact import insert_cashflow_facts
from domain.imports.account_import_plan import bank_leg
from domain.investments.investment_trade_fact import insert_investment_trade_facts


def enabled() -> bool:
    return os.environ.get("PAMS_IMPORT_PUBLISH_ENABLED", "").lower() == "true"


def preview(cur, batch_id, revision):
    batch = store.batch(cur, batch_id, lock=True)
    store.editable(batch, revision)
    result = preflight.preview(cur, batch)
    result.pop("_prepared")
    result["prepared_at"] = datetime.now(timezone.utc).isoformat()
    cur.execute("UPDATE pams_account_import_batch SET preview_json=%s,preview_token=%s WHERE batch_id=%s",
                (Json(store.norm(result)), result["preview_token"], batch_id))
    store.audit(cur, batch_id, "previewed", {"revision": revision, "token": result["preview_token"], "counts": result["counts"]})
    return store.norm(result)


def _bank_leg(cur, batch, row, fact):
    value = bank_leg(batch, row, fact)
    identity = value["source_id"]
    if insert_cashflow_facts(cur, [value]) != 1:
        raise store.ImportProblem("cashflow_conflict", "对方现金流水发生冲突，整批未发布", 409)
    cur.execute("SELECT cashflow_id FROM pams_cashflow_fact WHERE source_table=%s AND source_id=%s",
                (value["source_table"], identity))
    return cur.fetchone()["cashflow_id"]


def _snapshot(cur, row):
    value = row["snapshot"]
    columns = tuple(value)
    # Column names are the fixed service dictionary, never a request mapping.
    updates = ",".join(f"{k}=EXCLUDED.{k}" for k in columns if k not in {"account_code", "name", "as_of"})
    cur.execute(f"""INSERT INTO pams_holdings_snapshot ({','.join(columns)})
        VALUES ({','.join('%(' + k + ')s' for k in columns)})
        ON CONFLICT(account_code,name,as_of) DO UPDATE SET {updates},updated_at=now() RETURNING id""", value)
    return cur.fetchone()["id"]


def publish(cur, batch_id, revision, preview_token):
    if not enabled():
        raise store.ImportProblem("publish_disabled", "发布未启用；可保存和核对草稿", 403)
    batch = store.batch(cur, batch_id, lock=True)
    stored = batch.get("preview_json") or {}
    if not stored.get("preview_token") or not hmac.compare_digest(stored["preview_token"], preview_token):
        raise store.ImportProblem("preview_required", "请重新预检后发布", 409)
    if batch["revision"] != revision:
        raise store.ImportProblem("revision_conflict", "批次版本已改变，请重新核对", 409)
    if batch["status"] == "published":
        return {"batch": batch, **batch["published_result"], "replayed": True}
    store.editable(batch, revision)
    prepared = datetime.fromisoformat(stored["prepared_at"])
    if (datetime.now(timezone.utc) - prepared).total_seconds() > 15 * 60:
        raise store.ImportProblem("preview_expired", "预检已过期，请重新核对", 409)
    cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("account-import:" + batch["account_code"],))
    result = preflight.preview(cur, batch)
    if not hmac.compare_digest(result["preview_token"], preview_token):
        raise store.ImportProblem("source_changed", "账户或来源资料已变化，请重新预检", 409)
    if not result["can_publish"]:
        raise store.ImportProblem("preflight_blocked", "仍有阻塞或待确认记录，整批未发布", 409)
    published = []
    for row in result["_prepared"]:
        if row["status"] != "ready":
            continue
        if row.get("snapshot"):
            identifier, table = _snapshot(cur, row), "pams_holdings_snapshot"
        elif row.get("cashflow"):
            value = row["cashflow"]
            if insert_cashflow_facts(cur, [value]) != 1:
                raise store.ImportProblem("cashflow_conflict", "账单来源冲突，整批未发布", 409)
            cur.execute("SELECT cashflow_id FROM pams_cashflow_fact WHERE source_table=%s AND source_id=%s",
                        (value["source_table"], value["source_id"]))
            identifier, table = cur.fetchone()["cashflow_id"], "pams_cashflow_fact"
        else:
            fact = row["fact"]
            if fact["side"] in {"deposit", "withdraw"}:
                fact["cashflow_id"] = _bank_leg(cur, batch, row, fact)
                published.append({"row_id": row["row_id"], "table": "pams_cashflow_fact", "id": fact["cashflow_id"],
                    "role": "bank_leg", "before": None, "after": store.norm(bank_leg(batch, row, fact)), "source": row["source"]})
            if insert_investment_trade_facts(cur, [fact], strict_source=True) != 1:
                raise store.ImportProblem("trade_conflict", "交易来源发生冲突，整批未发布", 409)
            cur.execute("SELECT trade_id FROM pams_investment_trade_fact WHERE source_table=%s AND source_id=%s",
                        (fact["source_table"], fact["source_id"]))
            identifier, table = cur.fetchone()["trade_id"], "pams_investment_trade_fact"
        published.append({"row_id": row["row_id"], "table": table, "id": identifier,
                          "before": row["before"], "after": row["after"], "source": row["source"]})
    summary = {"counts": result["counts"], "records": store.norm(published), "published_at": datetime.now(timezone.utc).isoformat()}
    cur.execute("UPDATE pams_account_import_batch SET status='published',published_result=%s,updated_at=now() WHERE batch_id=%s",
                (Json(summary), batch_id))
    store.audit(cur, batch_id, "published", {"revision": revision, "preview_token": preview_token, **summary})
    return {"batch": store.batch(cur, batch_id), **summary, "replayed": False}
