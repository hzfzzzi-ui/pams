"""Bounded cash-fact consumption of already published daily-close evidence.

Independent of the daily-close publisher and its official confirmation state.
The caller supplies a dedicated, idle connection; sync owns its single transaction.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, localcontext
import hashlib
import re
from typing import Any
import xml.etree.ElementTree as ET

import psycopg2.extras
from psycopg2.extensions import TRANSACTION_STATUS_IDLE

from services import ibkr_cash_events as cash
from services import ibkr_daily_close_read as daily
from services.investment_trade_fact import IBKR_TABLE, build_investment_trade_fact, insert_investment_trade_facts

BROKER_SOURCE = "ibkr_published_daily_close"
SYNC_KEY = "ibkr_cash_events"
MAX_DAYS = 3


def _cash_scope_ok(node: ET.Element, parents: dict, account: str) -> bool:
    ancestor: ET.Element | None = node
    while ancestor is not None:
        attrs = {key.lower(): value.strip() for key, value in ancestor.attrib.items()}
        if (len(attrs) != len(ancestor.attrib) or attrs.get("model")
                or any(attrs.get(key) not in (None, "", account) for key in ("accountid", "acctid"))):
            return False
        ancestor = parents.get(ancestor)
    return True


def _snapshot(broker_source: str) -> dict[str, Any]:
    base: dict[str, Any] = {"broker_source": BROKER_SOURCE, "account_code": "ibkr", "cutoff": None,
        "source_account_id": None, "source_sha256": None, "confirmed_day": None, "source_file": None,
        "historical_day_count": 0, "next_days": [], "queued_day_count": 0, "unavailable_retry_days": [],
        "retryable_pending_count": 0, "waiting_evidence_count": 0, "blocking_pending_count": 0,
        "bank_match_pending_count": 0, "pending": [], "facts_written": 0, "reasons": []}
    if broker_source != BROKER_SOURCE:
        return {**base, "status": "unavailable", "reasons": ["unsupported_broker_source"]}
    try:
        cutoff = cash.configured_cutoff()
    except ValueError:
        return {**base, "status": "unavailable", "reasons": ["invalid_cash_event_cutoff"]}
    base["cutoff"] = cutoff.isoformat() if cutoff else None
    if cutoff is None:
        return {**base, "status": "disabled", "facts_written": 0}
    evidence = daily.load_evidence()
    if evidence.get("status") != "ok":
        return {**base, "status": "unavailable", "reasons": evidence.get("reasons", ["missing_evidence"])}
    series = daily.read_series(account_id=evidence["account_id"], evidence=evidence)
    digest = evidence["sha256"]
    if series.get("source_sha256") != digest or not series.get("daily_closes"):
        return {**base, "status": "unavailable", "reasons": ["unverified_daily_series"]}
    confirmed = {day for day, close in series["daily_closes"].items()
                 if close.get("status") == "ok" and close.get("source_sha256") == digest
                 and close.get("account_id") == evidence["account_id"] and close.get("as_of") == day}
    closing = evidence["as_of"].isoformat()
    if closing not in confirmed:
        return {**base, "status": "unavailable", "reasons": ["unconfirmed_closing_day"]}
    root = ET.fromstring(evidence["xml_text"])
    parents = {child: parent for parent in root.iter() for child in parent}
    days = {}
    for statement in root.iter():
        if statement.tag.rsplit("}", 1)[-1] != "FlexStatement" or statement.get("accountId") != evidence["account_id"]:
            continue
        try:
            day = cash._business_date(statement.get("toDate", "")).isoformat()
        except ValueError:
            continue
        if day not in confirmed or day <= cutoff.isoformat():
            continue
        events = []
        for node in statement.iter():
            if node.tag.rsplit("}", 1)[-1] != "CashTransaction":
                continue
            envelope: dict[str, Any] = {"raw": dict(node.attrib), "statement_account_id": evidence["account_id"],
                "source_evidence": {"broker_source": BROKER_SOURCE, "source_sha256": digest, "confirmed_day": day}}
            if not _cash_scope_ok(node, parents, evidence["account_id"]):
                envelope["scope_error"] = True
            events.append(envelope)
        sections = [node for node in statement.iter() if node.tag.rsplit("}", 1)[-1] == "CashTransactions"]
        section_nodes = set(sections[0].iter()) if len(sections) == 1 else set()
        section_ok = len(sections) == 1 and all(node in section_nodes
            for node in statement.iter() if node.tag.rsplit("}", 1)[-1] == "CashTransaction")
        if section_ok:
            section_ok = (_cash_scope_ok(sections[0], parents, evidence["account_id"])
                          and sections[0].get("levelOfDetail", "DETAIL").upper() == "DETAIL")
        days[day] = {"events": events, "section_ok": section_ok,
                     "statement_sha256": hashlib.sha256(ET.tostring(statement)).hexdigest()}
    return {**base, "status": "ready", "source_account_id": evidence["account_id"],
            "source_sha256": digest, "confirmed_day": closing, "source_file": evidence["source_file"],
            "historical_day_count": sum(day <= cutoff.isoformat() for day in confirmed), "days": days}


def _progress(cur, cutoff: str, source_account_id: str) -> dict[str, dict[str, Any]]:
    cur.execute("""SELECT DISTINCT ON (entry->>'day') entry AS day_result
        FROM pams_data_sync_runs run CROSS JOIN LATERAL jsonb_array_elements(
            CASE WHEN jsonb_typeof(summary_json->'days') = 'array' THEN summary_json->'days'
                 ELSE '[]'::jsonb END) AS entry
        WHERE sync_key = %s AND status = 'success' AND entry->>'day' > %s
          AND summary_json->>'source_account_id' = %s
        ORDER BY entry->>'day', run.id DESC""", (SYNC_KEY, cutoff, source_account_id))
    return {row["day_result"]["day"]: row["day_result"] for row in cur.fetchall()}


def _trade_versions(cur, snapshot: dict[str, Any]) -> dict[str, str]:
    # This is a retry-input marker, never a fact identity. Rebuilt row IDs still deduplicate by source_key.
    cur.execute("""SELECT trade_date, MAX(id) AS last_id, COUNT(*) AS row_count, MAX(updated_at) AS changed
        FROM pams_ibkr_trades WHERE account_code = 'ibkr' AND source_account_id = %s
          AND trade_date = ANY(%s::date[]) AND source_trade_key NOT LIKE 'manual_%%'
        GROUP BY trade_date""", (snapshot["source_account_id"], list(snapshot["days"])))
    return {row["trade_date"].isoformat(): f"{row['last_id']}:{row['row_count']}:{row['changed'].isoformat()}"
            for row in cur.fetchall()}


def _import_day_trades(cur, snapshot: dict[str, Any], day: str) -> tuple[int, list[dict[str, Any]]]:
    cur.execute("""SELECT * FROM pams_investment_trade_fact WHERE source_table=%s
        AND account_code='ibkr' AND trade_date=%s""", (IBKR_TABLE, day))
    existing_by_key: dict[str, list[dict]] = {}
    for stored in cur.fetchall():
        existing_by_key.setdefault(stored["source_key"], []).append(dict(stored))
    cur.execute("""SELECT %s AS source_table, ib.id::text AS source_id,
        ib.account_code || ':' || ib.source_trade_key AS source_key, ib.trade_date, ib.account_code,
        'ibkr'::text AS broker, ib.symbol, ib.name, ib.asset_category AS market, ib.currency,
        ib.side, ib.quantity, ib.price, ib.gross_amount, ABS(ib.commission) AS fee_amount,
        0::numeric AS tax_amount, ib.net_cash, ib.realized_pnl, ib.raw_json
        FROM pams_ibkr_trades ib WHERE ib.account_code = 'ibkr' AND ib.source_account_id = %s
          AND ib.trade_date = %s AND ib.source_trade_key NOT LIKE 'manual_%%'
        ORDER BY ib.id""", (IBKR_TABLE, snapshot["source_account_id"], day))
    source_rows = [dict(row) for row in cur.fetchall()]
    inserted, pending = 0, []
    for row in source_rows:
        reason = None
        if not re.fullmatch(r"[A-Z]{3}", row.get("currency") or ""):
            reason = "missing_or_invalid_trade_currency"
        elif str(row.get("side") or "").upper() not in {"BUY", "SELL"} or not row.get("symbol"):
            reason = "unsupported_trade_evidence"
        elif not row.get("quantity") or any(
            value is None or not Decimal(str(value)).is_finite()
            for value in (row.get(key) for key in ("quantity", "price", "gross_amount", "fee_amount", "net_cash"))
        ):
            reason = "missing_or_invalid_trade_amount"
        rate = cash.dated_fx_rate(cur, row["currency"], row["trade_date"]) if reason is None else None
        if reason is None and rate is None:
            reason = "missing_or_invalid_dated_fx"
        if reason:
            retryable = reason == "missing_or_invalid_dated_fx"
            pending.append({"source_key": row["source_key"], "event_kind": "trade", "reason": reason,
                "retryable": retryable, "state": "retryable" if retryable else "waiting_evidence",
                "fact_imported": False, "blocking": True})
            continue
        row["fx_rate_to_cny"] = rate
        row["source_evidence"] = {"broker_source": BROKER_SOURCE, "source_sha256": snapshot["source_sha256"],
                                  "confirmed_day": day, "evidence_role": "confirmed_day_gate"}
        fact = build_investment_trade_fact(row)
        existing = existing_by_key.get(row["source_key"], [])
        if existing:
            protected = ("account_code", "trade_date", "symbol", "currency", "trade_type", "side",
                         "quantity", "price", "gross_amount", "fee_amount", "tax_amount", "net_cash",
                         "realized_pnl", "fx_rate_to_cny", "net_cash_cny", "realized_pnl_cny")
            if len(existing) != 1 or any(not _stored_equal(existing[0].get(key), fact.get(key)) for key in protected):
                pending.append({"source_key": row["source_key"], "event_kind": "trade",
                    "reason": "existing_trade_evidence_conflict", "retryable": False,
                    "state": "waiting_evidence", "fact_imported": True, "blocking": True})
            continue
        inserted += insert_investment_trade_facts(cur, [fact], strict_source=True)
    return inserted, pending


def _stored_equal(left: Any, right: Any) -> bool:
    if isinstance(left, Decimal) or isinstance(right, Decimal):
        if left is None or right is None:
            return False
        with localcontext() as context:
            context.prec = 80
            quantum = Decimal("0.0000000001")
            return Decimal(str(left)).quantize(quantum, rounding=ROUND_HALF_UP) == Decimal(str(right)).quantize(quantum, rounding=ROUND_HALF_UP)
    return left == right


def _plan(snapshot: dict[str, Any], progress: dict[str, dict[str, Any]], limit: int,
          trade_versions: dict[str, str]) -> dict[str, Any]:
    fresh = [day for day, item in sorted(snapshot["days"].items())
             if day not in progress or progress[day].get("statement_sha256") != item["statement_sha256"]
             or progress[day].get("raw_trade_version") != trade_versions.get(day)]
    retry = [day for day in sorted(snapshot["days"]) if day not in fresh
             and progress.get(day, {}).get("retryable_count", 0) > 0]
    pending = [{"day": day, **item} for day, saved in sorted(progress.items())
               for item in saved.get("pending", [])]
    return {**{key: value for key, value in snapshot.items() if key != "days"},
            "next_days": (fresh + retry)[:limit], "queued_day_count": len(fresh) + len(retry),
            "unavailable_retry_days": [day for day, saved in sorted(progress.items())
                                       if saved.get("retryable_count", 0) > 0 and day not in snapshot["days"]],
            "retryable_pending_count": sum(bool(item.get("retryable")) for item in pending),
            "blocking_pending_count": sum(item.get("blocking", True) for item in pending),
            "bank_match_pending_count": sum(item.get("reason") == "bank_match_pending" for item in pending),
            "waiting_evidence_count": sum(not item.get("retryable") for item in pending), "pending": pending}


def status(conn, *, broker_source: str = BROKER_SOURCE) -> dict[str, Any]:
    """Read-only status, including independent retry state without downgrading close confirmation."""
    snapshot = _snapshot(broker_source)
    if snapshot["status"] != "ready":
        return snapshot
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT to_regclass('public.pams_data_sync_runs') IS NOT NULL AS available")
        if not cur.fetchone()["available"]:
            return {**{key: value for key, value in snapshot.items() if key != "days"},
                    "status": "unavailable", "reasons": ["missing_sync_run_table"]}
        return _plan(snapshot, _progress(cur, snapshot["cutoff"], snapshot["source_account_id"]), MAX_DAYS,
                     _trade_versions(cur, snapshot))


def sync(conn, *, broker_source: str = BROKER_SOURCE, max_days: int = MAX_DAYS) -> dict[str, Any]:
    """Import at most max_days new/retryable dates; no downloads, DDL, or balance writes."""
    if type(max_days) is not int or not 1 <= max_days <= 7:
        raise ValueError("max_days must be between 1 and 7")
    snapshot = _snapshot(broker_source)
    if snapshot["status"] != "ready":
        return {**snapshot, "facts_written": 0}
    if conn.autocommit or conn.get_transaction_status() != TRANSACTION_STATUS_IDLE:
        raise ValueError("Cash event sync requires a dedicated idle transactional connection")
    started = datetime.now(timezone.utc)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Same lock as the legacy collector, also protecting the shared immutable fact source.
            cur.execute("SELECT pg_advisory_xact_lock(7362198401)")
            progress = _progress(cur, snapshot["cutoff"], snapshot["source_account_id"])
            trade_versions = _trade_versions(cur, snapshot)
            plan = _plan(snapshot, progress, max_days, trade_versions)
        if not plan["next_days"]:
            conn.rollback()
            return {**plan, "status": "idle", "facts_written": 0}
        results = []
        for day in plan["next_days"]:
            item = snapshot["days"][day]
            if item["section_ok"]:
                result = cash.import_cash_events(conn, item["events"], account_code="ibkr",
                    cutoff=date.fromisoformat(snapshot["cutoff"]), statement_as_of=date.fromisoformat(day))
            else:
                result = {"cutoff": snapshot["cutoff"], "inserted_count": 0, "duplicate_count": 0,
                    "historical_count": 0, "retryable_count": 0, "waiting_evidence_count": 1,
                    "blocking_pending_count": 1, "bank_match_pending_count": 0, "pending": [{
                        "transaction_id": None, "reason": "missing_or_ambiguous_cash_transactions_section",
                        "state": "waiting_evidence", "retryable": False, "retry_on": "source_change",
                        "fact_imported": False, "blocking": True}]}
            result["complete"] = result["blocking_pending_count"] == 0
            result["cash_section_present"] = item["section_ok"]
            result["cash_inserted_count"] = result["inserted_count"]
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                trade_count, trade_pending = _import_day_trades(cur, snapshot, day)
            result["trade_inserted_count"] = trade_count
            result["inserted_count"] += trade_count
            result["pending"].extend(trade_pending)
            result["retryable_count"] += sum(item["retryable"] for item in trade_pending)
            result["waiting_evidence_count"] += sum(not item["retryable"] for item in trade_pending)
            result["blocking_pending_count"] += len(trade_pending)
            result["complete"] = result["blocking_pending_count"] == 0
            result["raw_trade_version"] = trade_versions.get(day)
            results.append({**result, "day": day, "statement_sha256": item["statement_sha256"],
                            "source_sha256": snapshot["source_sha256"]})
        progress.update({item["day"]: item for item in results})
        result = {**_plan(snapshot, progress, max_days, trade_versions), "status": "success",
                  "facts_written": sum(item["inserted_count"] for item in results), "days": results}
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO pams_data_sync_runs
                (sync_key, asset_scope, target_trade_date, status, started_at, finished_at,
                 rows_upserted, summary_json)
                VALUES (%s, 'ibkr', %s, 'success', %s, now(), %s, %s) RETURNING id""",
                (SYNC_KEY, snapshot["confirmed_day"], started, result["facts_written"], psycopg2.extras.Json(result)))
            result["run_id"] = cur.fetchone()[0]
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
