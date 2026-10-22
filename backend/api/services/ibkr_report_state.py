"""Committed Flex report selection. Raw files are evidence, not publication pointers."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import hashlib
import os
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import psycopg2.extras


class ReportRejected(ValueError):
    """A downloaded report must not replace the current published state."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@lru_cache(maxsize=1)
def _calendar():
    return xcals.get_calendar("XNYS")


def expected_statement_date(reference: date | None = None, *, now: datetime | None = None) -> date:
    current = (now or utc_now()).astimezone(ZoneInfo("America/New_York"))
    if reference is None:
        hour, minute = map(int, os.environ.get("PAMS_NORTH_AMERICA_REFRESH_TIME", "16:30").split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ReportRejected("invalid_refresh_cutoff")
        reference = current.date() - timedelta(days=(current.hour, current.minute) < (hour, minute))
    return _calendar().date_to_session(reference.isoformat(), direction="previous").date()


def _report_date(value: Any) -> date:
    text = str(value or "")
    if len(text) == 8 and text.isdigit():
        return datetime.strptime(text, "%Y%m%d").date()
    return date.fromisoformat(text)


def inspect_report(xml_text: str) -> date:
    """Validate fields consumed by the existing performance parser, without calculating amounts."""
    root = ET.fromstring(xml_text)
    cash = [e.attrib for e in root.iter("CashReportCurrency") if e.get("currency") == "BASE_SUMMARY"]
    if not cash:
        raise ReportRejected("missing_base_cash")
    latest = max(cash, key=lambda row: _report_date(row.get("toDate")))
    observed = _report_date(latest.get("toDate"))
    current_cash = [row for row in cash if _report_date(row.get("toDate")) == observed]
    if any(row != latest for row in current_cash):
        raise ReportRejected("ambiguous_base_cash")

    def require_number(row: dict[str, str], key: str) -> None:
        try:
            value = Decimal(row[key].replace(",", ""))
            if not value.is_finite():
                raise ValueError()
        except (KeyError, InvalidOperation, ValueError):
            raise ReportRejected("missing_or_invalid_" + key) from None

    for field in ("endingCash", "depositWithdrawalsYTD"):
        require_number(latest, field)
    positions = list(root.iter("OpenPosition"))
    current = [e.attrib for e in positions if _report_date(e.get("reportDate")) == observed]
    if positions and not current:
        raise ReportRejected("cash_position_date_mismatch")
    if not positions and not list(root.iter("OpenPositions")):
        raise ReportRejected("missing_open_positions_section")
    accounts = {row.get("accountId") for row in [*current_cash, *current] if row.get("accountId")}
    accounts.update(e.get("accountId") for e in root.iter("FlexStatement") if e.get("accountId"))
    if len(accounts) > 1:
        raise ReportRejected("multiple_source_accounts_require_review")
    for row in current:
        if not row.get("symbol") or not row.get("currency"):
            raise ReportRejected("missing_position_identity")
        require_number(row, "position" if "position" in row else "quantity")
        require_number(row, "positionValue")
        if row.get("currency") != "USD":
            require_number(row, "fxRateToBase")
        for key in ("fxRateToBase", "fifoPnlUnrealized"):
            if key in row:
                require_number(row, key)
        if "fxRateToBase" in row and Decimal(row["fxRateToBase"].replace(",", "")) <= 0:
            raise ReportRejected("invalid_fx_rate")
    for elem in root.iter("Trade"):
        for key in ("fifoPnlRealized", "fxRateToBase"):
            if key in elem.attrib:
                require_number(elem.attrib, key)
    return observed


@dataclass(frozen=True)
class PublishedReport:
    path: Path
    as_of: date
    run_id: int
    sha256: str
    data_sha256: str | None
    legacy: bool = False
    row_count: int | None = None


def _records(conn) -> list[dict[str, Any]]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT to_regclass('public.pams_data_sync_runs') IS NOT NULL AS exists")
        if not cur.fetchone()["exists"]:
            return []
        cur.execute("""
            SELECT id, status, target_trade_date, summary_json, started_at, finished_at
            FROM pams_data_sync_runs WHERE sync_key = 'ibkr_flex' AND asset_scope = 'ibkr'
            ORDER BY id DESC
        """)
        return [dict(row) for row in cur.fetchall()]


def _resolve_file(raw_file: str, raw_dir: Path) -> Path:
    # Legacy records contain container paths even when inspected on the host.
    name = raw_file.replace("\\", "/").rsplit("/", 1)[-1]
    path = (raw_dir / name).resolve()
    if path.parent != raw_dir.resolve() or path.suffix.lower() != ".xml" or not path.is_file():
        raise ValueError("unavailable_report_file")
    return path


def _eligible_records(records: list[dict[str, Any]]) -> list[tuple[date, int, dict[str, Any]]]:
    eligible = []
    for row in records:
        summary = row.get("summary_json") or {}
        if not isinstance(summary, dict):
            continue
        publication = summary.get("publication")
        if row["status"] != "success" or (publication is not None and (
            not isinstance(publication, dict) or publication.get("state") != "published"
            or publication.get("version") != 1 or publication.get("account_code") != "ibkr"
        )):
            continue
        try:
            observed = _report_date(summary.get("statement_as_of"))
            eligible.append((observed, int(row["id"]), row))
        except (TypeError, ValueError):
            continue
    return sorted(eligible, key=lambda item: item[:2], reverse=True)


def publication_floor(conn) -> date | None:
    """Missing or corrupt files cannot lower the committed publication high-water mark."""
    eligible = _eligible_records(_records(conn))
    return eligible[0][0] if eligible else None


def select_report(conn, raw_dir: Path) -> tuple[PublishedReport | None, dict[str, Any] | None]:
    records = _records(conn)
    eligible = _eligible_records(records)
    # Legacy successes bootstrap old installations; they cannot supersede a verified publication.
    eligible = [item for item in eligible if item[2]["summary_json"].get("publication")] + [
        item for item in eligible if not item[2]["summary_json"].get("publication")
    ]
    for observed, run_id, row in eligible:
        summary = row["summary_json"]
        publication = summary.get("publication") or {}
        try:
            path = _resolve_file(summary["raw_file"], raw_dir)
            content = path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            if publication and (publication.get("sha256") != digest or publication.get("report_date") != observed.isoformat()):
                continue
            if inspect_report(content.decode("utf-8")) != observed:
                continue
            count = summary.get("row_count")
            count = count if isinstance(count, int) and count >= 0 else None
            report = PublishedReport(path, observed, run_id, digest, publication.get("data_sha256"), not bool(publication), count)
            return report, records[0] if records else None
        except (OSError, ValueError, KeyError, TypeError, ET.ParseError):
            continue
    return None, records[0] if records else None


def report_quality(report: PublishedReport | None, latest: dict[str, Any] | None, *, expected: date | None = None) -> dict[str, Any]:
    try:
        due = expected or expected_statement_date()
    except (ValueError, TypeError):
        due = None
    using_previous = bool(report and latest and report.run_id != latest["id"])
    summary = (latest or {}).get("summary_json")
    publication = summary.get("publication") if isinstance(summary, dict) else None
    last_state = publication.get("state") if isinstance(publication, dict) else None
    status, label = "ok", "有效报告"
    if report is None:
        status, label = "missing", "无有效报告"
    elif due is None or report.legacy:
        status, label = "review", "历史报告待核准"
    if report is not None and due is not None and report.as_of < due:
        status, label = "stale", "上一有效值已过期"
    elif report is not None and latest and (
        latest["status"] not in {"success", "ok"}
        or (using_previous and last_state != "unchanged")
    ):
        status, label = "review", "沿用上一有效值"
    return {
        "status": status, "label": label, "using_previous": using_previous,
        "statement_as_of": report.as_of.isoformat() if report else None,
        "expected_statement_as_of": due.isoformat() if due else None,
        "active_run_id": report.run_id if report else None,
        "active_row_count": report.row_count if report else None,
        "legacy_unverified": bool(report and report.legacy),
        "last_attempt_status": latest.get("status") if latest else None,
    }


def validate_candidate(observed: date, expected: date, previous: PublishedReport | None, *, now: datetime | None = None, minimum: date | None = None) -> None:
    if observed < expected:
        raise ReportRejected("stale_statement_date")
    if (previous and observed < previous.as_of) or (minimum and observed < minimum):
        raise ReportRejected("report_date_regression")
    current = now or utc_now()
    local = current.astimezone(ZoneInfo("America/New_York"))
    session = _calendar().date_to_session(local.date().isoformat(), direction="previous")
    if _calendar().session_close(session).to_pydatetime() > current:
        session = _calendar().previous_session(session)
    latest_closed = session.date()
    # Flex covers a global account: a US holiday may still contain Asian-market activity.
    if not _calendar().is_session(local.date().isoformat()):
        hour, minute = map(int, os.environ.get("PAMS_NORTH_AMERICA_REFRESH_TIME", "16:30").split(":"))
        if (local.hour, local.minute) >= (hour, minute):
            latest_closed = local.date()
    if observed > latest_closed:
        raise ReportRejected("future_or_unclosed_statement_date")
