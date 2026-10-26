"""Advance approved close evidence without importing trades or changing ledgers."""
from __future__ import annotations

from datetime import date
import hashlib
import os
from pathlib import Path
from typing import Any

from collectors.ibkr_flex import fetch_flex_statement, request_flex_reference
from services import ibkr_daily_close_read as daily
from services import ibkr_performance as performance
from services.ibkr_daily_close import parse_daily_close_evidence
from services.ibkr_daily_close_publish import publish
from services.ibkr_report_state import expected_statement_date, select_report, production_scope

QUERY_ENV = "PAMS_IBKR_DAILY_CLOSE_QUERY_ID"


def _credentials() -> tuple[str, str]:
    token, query = os.environ.get("IBKR_FLEX_TOKEN", ""), os.environ.get(QUERY_ENV, "").strip()
    production = {os.environ.get(key, "").strip().lstrip("0") for key in (
        "IBKR_FLEX_QUERY_ID", "IBKR_FLEX_POSITIONS_QUERY_ID", "IBKR_FLEX_OPEN_POSITIONS_QUERY_ID")}
    if (not token or not query.isascii() or not query.isdigit() or not query.lstrip("0")
            or query.lstrip("0") in production or not any(production) or not daily.configured()):
        raise ValueError("daily_close_not_configured")
    return token, query


def daily_close_status() -> dict[str, Any]:
    try:
        _credentials()
        configured = True
    except ValueError:
        configured = False
    try:
        report, quality, scope = _production_context()
    except (OSError, ValueError, KeyError, TypeError):
        report, quality, scope = None, {}, None
    evidence = daily.load_evidence()
    day = evidence.get("as_of") if evidence.get("status") == "ok" else None
    expected = quality.get("expected_statement_as_of")
    verified = bool(configured and day and str(day) == expected
                    and quality.get("status") == "ok" and quality.get("legacy_unverified") is False
                    and quality.get("statement_as_of") == expected and report is not None
                    and scope == (evidence.get("account_id"), "USD")
                    and _matches_production(report, evidence))
    return {"source": "ibkr_daily_close", "configured": configured,
            "publication_verified": verified, "facts_written": 0,
            "source_sha256": evidence.get("sha256"),
            "report_quality": {"status": "ok" if verified else "waiting",
                "statement_as_of": str(day) if day else None,
                "expected_statement_as_of": expected, "legacy_unverified": False},
            "reasons": evidence.get("reasons", [])}


def _production_context():
    with performance.get_conn() as conn:
        report, latest = select_report(conn, performance._raw_dir())
    quality = performance.report_state.report_quality(report, latest)
    if report is None:
        return report, quality, None
    content = daily.read_bounded(report.path, daily.MAX_XML_BYTES)
    if hashlib.sha256(content).hexdigest() != report.sha256:
        raise ValueError("production_report_changed")
    return report, quality, production_scope(content, report.as_of, strict_ancestors=True)


def _matches_production(report, evidence: dict) -> bool:
    close = daily.read_close(as_of=evidence["as_of"], account_id=evidence["account_id"],
                            period_start=evidence["period_start"], evidence=evidence)
    subject = performance._parse_flex_performance(report.path, include_display_fx=False)
    checked = daily.overlay_performance(subject, account_id=evidence["account_id"], close=close)
    return (checked.get("daily_close", {}).get("status") == "ok"
            and hashlib.sha256(daily.read_bounded(report.path, daily.MAX_XML_BYTES)).hexdigest() == report.sha256)


def sync_daily_close(*, expected_statement_reference: date | None = None) -> dict[str, Any]:
    """Only advance the currently approved account and period; never bootstrap it."""
    try:
        token, query = _credentials()
        target = expected_statement_date(expected_statement_reference)
        if target > expected_statement_date():
            raise ValueError("future_reference")
        old = daily.load_evidence()
        if old.get("status") != "ok":
            raise ValueError("approved_close_evidence_unavailable")
        if old["as_of"] > target:
            raise ValueError("report_date_regression")
        report, quality, scope = _production_context()
        if (report is None or report.as_of != target or quality.get("status") != "ok"
                or quality.get("legacy_unverified") is not False):
            return {"success": False, "status": "waiting", "reason": "production_report_not_confirmed",
                    "facts_written": 0}
        if scope != (old["account_id"], "USD"):
            raise ValueError("production_account_or_base_mismatch")
        if old["as_of"] == target:
            if not _matches_production(report, old):
                raise ValueError("production_close_components_mismatch")
            return {"success": True, "status": "success", "publication_verified": True,
                    "statement_as_of": target.isoformat(), "source_sha256": old["sha256"],
                    "unchanged": True, "facts_written": 0}
        before = hashlib.sha256(report.path.read_bytes()).hexdigest()
        if before != report.sha256:
            raise ValueError("production_report_changed")
        reference = request_flex_reference(token, query)
        if not reference.get("reference_code"):
            raise ValueError("missing_flex_reference")
        content = fetch_flex_statement(token, str(reference["reference_code"])).encode("utf-8")
        digest = hashlib.sha256(content).hexdigest()
        close = parse_daily_close_evidence(content.decode("utf-8"), account_id=old["account_id"],
            as_of=target, period_start=old["period_start"], expected_sha256=digest)
        if close.get("status") != "ok":
            return {"success": False, "status": "waiting", "reason": "daily_close_or_period_not_proven",
                    "facts_written": 0}
        subject = performance._parse_flex_performance(report.path, include_display_fx=False)
        checked = daily.overlay_performance(subject, account_id=old["account_id"], close=close)
        if checked.get("daily_close", {}).get("status") != "ok":
            raise ValueError("production_close_components_mismatch")
        if hashlib.sha256(report.path.read_bytes()).hexdigest() != before:
            raise ValueError("production_report_changed")
        manifest = Path(os.environ[daily.MANIFEST_ENV])
        if manifest.name != "daily-close.json":
            raise ValueError("unsupported_manifest_name")
        result = publish(content, directory=manifest.parent, account_id=old["account_id"],
            as_of=target, period_start=old["period_start"], preserve_history=True)
        return {"success": True, "status": "success", "publication_verified": True,
                "statement_as_of": target.isoformat(), "source_sha256": result["source_sha256"],
                "unchanged": False, "facts_written": 0}
    except Exception as exc:
        # Provider URLs and exception bodies can contain the existing Flex token.
        known = {"daily_close_not_configured", "future_reference", "approved_close_evidence_unavailable",
                 "production_account_or_base_mismatch",
                 "report_date_regression", "production_report_changed", "missing_flex_reference",
                 "production_close_components_mismatch", "unsupported_manifest_name",
                 "publication_historical_revision_requires_review", "publication_scope_or_date_regression",
                 "publication_same_date_source_conflict", "publication_closing_date_mismatch",
                 "publication_lock_timeout", "previous_source_integrity_failure"}
        reason = str(exc) if isinstance(exc, ValueError) and str(exc) in known else "source_unavailable"
        return {"success": False, "status": "blocked" if reason in known else "error",
                "reason": reason, "error_type": type(exc).__name__, "facts_written": 0}
