"""Publication contracts use synthetic reports, never production financial data."""
from datetime import date, datetime, timezone
import hashlib
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend/api"))
from collectors import ibkr_flex as collector
from services import ibkr_report_state as state


def report(day="20261022", cash="100"):
    return f'''<FlexQueryResponse><FlexStatement accountId="TEST">
    <OpenPositions />
    <CashReportCurrency currency="BASE_SUMMARY" toDate="{day}"
        endingCash="{cash}" depositWithdrawalsYTD="100" />
    </FlexStatement></FlexQueryResponse>'''


def record(tmp_path, run_id, day="2026-10-22", *, status="success", legacy=False):
    path = tmp_path / f"{run_id}.xml"
    path.write_text(report(day.replace("-", "")), encoding="utf-8")
    summary = {"statement_as_of": day, "raw_file": str(path)}
    if not legacy:
        summary["publication"] = {
            "version": 1, "account_code": "ibkr", "state": "published", "report_date": day,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    return {"id": run_id, "status": status, "summary_json": summary}


def test_same_second_raw_files_are_unique_and_complete(tmp_path):
    first = collector.save_raw_flex_xml(report(), "q", raw_dir=tmp_path)
    second = collector.save_raw_flex_xml(report(cash="200"), "q", raw_dir=tmp_path)
    assert first != second
    assert first.read_text(encoding="utf-8") == report()
    assert second.read_text(encoding="utf-8") == report(cash="200")
    assert len(list(tmp_path.iterdir())) == 2


def test_selects_committed_business_date_not_mtime(tmp_path, monkeypatch):
    old = record(tmp_path, 1, "2026-10-21")
    valid = record(tmp_path, 2)
    failed = record(tmp_path, 3, "2026-10-23", status="failed")
    os.utime(tmp_path / "1.xml", (9999999999, 9999999999))
    (tmp_path / "uncommitted.xml").write_text(report("20261026"))
    monkeypatch.setattr(state, "_records", lambda conn: [failed, old, valid])
    selected, latest = state.select_report(None, tmp_path)
    assert selected.run_id == 2
    assert latest["id"] == 3
    quality = state.report_quality(selected, latest, expected=date(2026, 10, 22))
    assert quality["using_previous"] and quality["status"] == "review"


def test_corrupt_published_file_falls_back_and_is_flagged(tmp_path, monkeypatch):
    previous = record(tmp_path, 1, "2026-10-21")
    damaged = record(tmp_path, 2)
    (tmp_path / "2.xml").write_text(report(cash="999"))
    monkeypatch.setattr(state, "_records", lambda conn: [damaged, previous])
    selected, latest = state.select_report(None, tmp_path)
    assert selected.run_id == 1
    assert state.report_quality(selected, latest, expected=selected.as_of)["status"] == "review"


def test_legacy_is_not_silently_certified(tmp_path, monkeypatch):
    old = record(tmp_path, 1, legacy=True)
    monkeypatch.setattr(state, "_records", lambda conn: [old])
    selected, latest = state.select_report(None, tmp_path)
    quality = state.report_quality(selected, latest, expected=selected.as_of)
    assert quality["legacy_unverified"] and quality["status"] == "review"


def test_newer_legacy_attempt_cannot_supersede_verified_publication(tmp_path, monkeypatch):
    published = record(tmp_path, 1)
    legacy = record(tmp_path, 2, "2026-10-23", legacy=True)
    monkeypatch.setattr(state, "_records", lambda conn: [legacy, published])
    selected, latest = state.select_report(None, tmp_path)
    assert selected.run_id == 1 and not selected.legacy
    assert state.report_quality(selected, latest, expected=selected.as_of)["status"] == "review"


@pytest.mark.parametrize("reference,expected", [
    ("2026-06-19", "2026-06-18"),  # Juneteenth, not a session.
    ("2026-06-21", "2026-06-18"),
    ("2026-11-27", "2026-11-27"),  # Half day is still a session.
])
def test_expected_date_uses_exchange_calendar(reference, expected):
    assert state.expected_statement_date(date.fromisoformat(reference)).isoformat() == expected


@pytest.mark.parametrize("instant,day", [
    ("2026-03-09T20:29:00+00:00", "2026-03-06"),
    ("2026-03-09T20:30:00+00:00", "2026-03-09"),
    ("2026-11-02T21:29:00+00:00", "2026-10-30"),
    ("2026-11-02T21:30:00+00:00", "2026-11-02"),
])
def test_refresh_cutoff_follows_dst(monkeypatch, instant, day):
    monkeypatch.setenv("PAMS_NORTH_AMERICA_REFRESH_TIME", "16:30")
    assert state.expected_statement_date(now=datetime.fromisoformat(instant)).isoformat() == day


@pytest.mark.parametrize("instant,accepted", [
    ("2026-11-27T17:59:00+00:00", False),
    ("2026-11-27T18:01:00+00:00", True),
])
def test_half_day_cannot_publish_before_actual_close(instant, accepted):
    args = (date(2026, 11, 27), date(2026, 11, 25), None)
    if accepted:
        state.validate_candidate(*args, now=datetime.fromisoformat(instant))
    else:
        with pytest.raises(state.ReportRejected, match="unclosed"):
            state.validate_candidate(*args, now=datetime.fromisoformat(instant))


@pytest.mark.parametrize("xml", [
    "<FlexQueryResponse/>", report(cash="NaN"),
    report().replace("<OpenPositions />", ""),
    report().replace('depositWithdrawalsYTD="100"', ""),
    report().replace("<OpenPositions />", '<OpenPosition reportDate="20261021"/>'),
])
def test_incomplete_reports_are_rejected(xml):
    with pytest.raises(ValueError):
        state.inspect_report(xml)


def test_regression_and_stale_are_distinct(tmp_path):
    previous = state.PublishedReport(tmp_path / "x.xml", date(2026, 10, 22), 1, "x", "x")
    now = datetime(2026, 10, 23, 23, tzinfo=timezone.utc)
    with pytest.raises(state.ReportRejected, match="regression"):
        state.validate_candidate(date(2026, 10, 21), date(2026, 10, 20), previous, now=now)
    with pytest.raises(state.ReportRejected, match="stale"):
        state.validate_candidate(date(2026, 10, 21), date(2026, 10, 22), None, now=now)


def test_missing_file_does_not_lower_publication_floor(tmp_path, monkeypatch):
    old = record(tmp_path, 1, "2026-10-21")
    latest = record(tmp_path, 2)
    (tmp_path / "2.xml").unlink()
    monkeypatch.setattr(state, "_records", lambda conn: [latest, old])
    selected, _ = state.select_report(None, tmp_path)
    floor = state.publication_floor(None)
    assert selected.as_of == date(2026, 10, 21) and floor == date(2026, 10, 22)
    with pytest.raises(state.ReportRejected, match="regression"):
        state.validate_candidate(selected.as_of, selected.as_of, selected, minimum=floor)


def test_global_flex_can_cover_us_holiday_but_not_an_unfinished_day():
    args = (date(2026, 6, 19), date(2026, 6, 18), None)
    with pytest.raises(state.ReportRejected, match="unclosed"):
        state.validate_candidate(*args, now=datetime(2026, 6, 19, 15, tzinfo=timezone.utc))
    state.validate_candidate(*args, now=datetime(2026, 6, 19, 21, tzinfo=timezone.utc))


def test_atomic_file_failure_leaves_no_partial_xml(tmp_path, monkeypatch):
    def fail(*args):
        raise OSError("disk failure")
    monkeypatch.setattr(collector.os, "replace", fail)
    with pytest.raises(OSError, match="disk failure"):
        collector.save_raw_flex_xml(report(), "q", raw_dir=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_missing_and_malformed_audit_do_not_use_untracked_files(tmp_path, monkeypatch):
    (tmp_path / "uncommitted.xml").write_text(report())
    monkeypatch.setattr(state, "_records", lambda conn: [
        {"id": 1, "status": "success", "summary_json": []},
        {"id": 2, "status": "success", "summary_json": {"publication": "bad"}},
    ])
    selected, latest = state.select_report(None, tmp_path)
    assert selected is None
    assert state.report_quality(selected, latest)["status"] == "missing"


@pytest.mark.parametrize("attributes", [
    'symbol="TEST" currency="USD" positionValue="100"',
    'symbol="TEST" currency="USD" position="NaN" positionValue="100"',
    'symbol="TEST" currency="HKD" position="1" positionValue="100" fxRateToBase="0"',
    'symbol="TEST" currency="USD" position="1" positionValue="100" fifoPnlUnrealized="NaN"',
    'symbol="TEST" currency="USD" position="1" positionValue="100" accountId="OTHER"',
])
def test_missing_position_fields_and_nonfinite_amounts_fail_closed(attributes):
    xml = report().replace("<OpenPositions />", f'<OpenPosition reportDate="20261022" {attributes}/>')
    with pytest.raises(state.ReportRejected):
        state.inspect_report(xml)
