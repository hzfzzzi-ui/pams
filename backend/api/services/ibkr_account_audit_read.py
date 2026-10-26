"""Read a published Flex report and audit the existing parser independently."""
from __future__ import annotations

from contextlib import closing
from datetime import date
import hashlib
from pathlib import Path
from typing import Any

import psycopg2.extras

from services import ibkr_performance as performance
from services.ibkr_account_audit import audit_ibkr_account
from services.ibkr_report_state import _report_date, report_quality, select_report, production_scope


def _unavailable(reason: str, error_type: str | None = None) -> dict[str, Any]:
    return {"status": "unavailable", "read_only": True, "executable": False,
            "reason": reason, "error_type": error_type, "checks": [], "counts": None}


def _original_scope(content: bytes, as_of: date) -> tuple[str, str]:
    return production_scope(content, as_of)

def _history_dates(conn, account: str) -> tuple[list[date], str]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT to_regclass('public.pams_ibkr_position_history') IS NOT NULL AS present")
        if not cur.fetchone()["present"]:
            return [], "missing_table"
        cur.execute("""
            SELECT DISTINCT as_of FROM pams_ibkr_position_history
            WHERE account_code = 'ibkr' AND source_account_id = %s AND as_of IS NOT NULL
            ORDER BY as_of
        """, (account,))
        return [row["as_of"] for row in cur.fetchall()], "stored_position_dates_not_verified_nav_history"


def read_ibkr_account_audit(*, connection_factory=None, raw_dir: Path | None = None,
                            period_start: date | None = None) -> dict[str, Any]:
    try:
        with closing((connection_factory or performance.get_conn)()) as conn:
            conn.set_session(readonly=True, isolation_level="REPEATABLE READ")
            with conn:
                with conn.cursor() as cur:
                    cur.execute("SET LOCAL statement_timeout = '5000ms'")
                report, latest = select_report(conn, raw_dir if raw_dir is not None else performance._raw_dir())
                if report is None:
                    return _unavailable("missing_report")
                quality = report_quality(report, latest)
                content = report.path.read_bytes()
                if hashlib.sha256(content).hexdigest() != report.sha256:
                    return _unavailable("report_changed")
                account, base = _original_scope(content, report.as_of)
                dates, history_source = _history_dates(conn, account)
                subject = performance._parse_flex_performance(report.path, include_display_fx=False)
                from services import ibkr_daily_close_read as daily
                supplemental = daily.load_evidence() if daily.configured() else None
                if supplemental is not None:
                    with conn.cursor() as cur:
                        subject = performance.apply_daily_close(subject, report.path, cur=cur,
                                                                period_start=period_start)
                if hashlib.sha256(report.path.read_bytes()).hexdigest() != report.sha256:
                    return _unavailable("report_changed")
                if (subject.get("status") != "ok" or subject.get("source_file") != report.path.name
                        or _report_date(subject.get("as_of")) != report.as_of):
                    return _unavailable("subject_report_mismatch")
                # These are the subject's values and semantics, not oracle-derived replacements.
                state = {"account_id": account, "as_of": subject["as_of"],
                         "base_currency": subject.get("base_currency"),
                         "ownership_basis": "full_account", "valuation_basis": "ibkr_statement_close",
                         "cash_basis": "ending_cash", "position_basis": "signed_market_value",
                         "nav_basis": "cash_plus_positions_not_official_nav_total",
                         **{key: subject.get(key) for key in
                            ("cash_base", "position_value_base", "net_liquidation_base")}}
                close_state = subject.get("daily_close") or {}
                portfolio_identity = None
                audit_content, audit_digest = content.decode("utf-8"), report.sha256
                if (supplemental and supplemental.get("status") == "ok"
                        and close_state.get("status") == "ok"
                        and supplemental["account_id"] == account
                        and supplemental["sha256"] == close_state.get("source_sha256")):
                    state["nav_basis"] = "official_nav_total"
                    if period_start is not None:
                        state["period"] = close_state.get("period")
                    audit_content, audit_digest = supplemental["xml_text"], supplemental["sha256"]
                    from services.valuation_nav import daily_close_account_identity
                    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                        portfolio_identity = daily_close_account_identity(cur, source_account_id=account,
                                                                         as_of=report.as_of)
                result = audit_ibkr_account(xml_text=audit_content, account_id=account,
                    as_of=report.as_of, base_currency=base,
                    internal_state=state if portfolio_identity is None or portfolio_identity.get("status") == "ok" else None,
                    period_start=period_start, history_dates=dates, expected_sha256=audit_digest,
                    quality=quality)
                result["report_quality"] = quality
                result["adapter"] = {"subject": "ibkr_performance._parse_flex_performance",
                    "include_display_fx": False, "snapshot": "repeatable_read_read_only",
                    "source_file": report.path.name, "run_id": report.run_id,
                    "original_sha256": report.sha256, "history_dates_source": history_source,
                    "history_date_count": len(dates), "internal_nav_basis": state["nav_basis"],
                    "internal_period_supplied": bool(state.get("period")), "internal_fx_supplied": False}
                if supplemental is not None:
                    result["adapter"]["subject"] = "ibkr_performance.apply_daily_close"
                    result["adapter"]["supplemental_sha256"] = audit_digest if audit_digest != report.sha256 else None
                    result["adapter"]["daily_close"] = close_state
                    result["adapter"]["portfolio_identity"] = portfolio_identity
                return result
    except Exception as exc:
        return _unavailable("read_failed", type(exc).__name__)
