"""Publication contracts use synthetic reports, never production financial data."""
from datetime import date, datetime, timezone
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "backend/api"))
from domain.brokers.ibkr import ibkr_report_state as state


def report(day="20261022", cash="100"):
    return f'''<FlexQueryResponse><FlexStatement accountId="TEST">
    <OpenPositions />
    <CashReportCurrency currency="BASE_SUMMARY" toDate="{day}"
        endingCash="{cash}" depositWithdrawalsYTD="100" />
    </FlexStatement></FlexQueryResponse>'''


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


def test_global_flex_can_cover_us_holiday_but_not_an_unfinished_day():
    args = (date(2026, 6, 19), date(2026, 6, 18), None)
    with pytest.raises(state.ReportRejected, match="unclosed"):
        state.validate_candidate(*args, now=datetime(2026, 6, 19, 15, tzinfo=timezone.utc))
    state.validate_candidate(*args, now=datetime(2026, 6, 19, 21, tzinfo=timezone.utc))


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
