from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "scheduler"))
sys.path.insert(0, str(ROOT / "backend" / "api"))

from refresh_state import RefreshJournal, latest_due_date, read_state, result_issues
from services.sync_health import scheduler_health
from test_legacy_strategy_scheduler import _load_scheduler


@pytest.mark.parametrize("payload", [
    None, {}, [], {"status": "partial"}, {"status": "ok", "errors": ["upstream"]},
    {"found": 3, "updated": 2}, {"found": 1, "updated": 0}, {"skipped": True},
    {"manual_review_items": 1}, {"success": False}, {"skipped_estimates": [{"symbol": "MU"}]},
    {"status": "ok", "rows": [{"status": "missing_nav"}]},
    {"status": "ok", "rows": [{"status": "confirmed", "confirmed_date": "2026-06-18"}]},
    {"status": "success", "as_of": "2026-06-22", "statement_as_of": "2026-06-18"},
])
def test_failed_partial_and_stale_results_block_publication(payload):
    assert result_issues(payload, target=date(2026, 6, 22))


def test_empty_market_and_current_estimate_are_valid():
    assert not result_issues({"found": 0, "updated": 0})
    assert not result_issues({"status": "ok", "rows": [{
        "status": "estimated", "confirmed_date": "2026-06-18", "estimate_time": "2026-06-22T15:05:00",
    }]}, target=date(2026, 6, 22))
    # At US close Beijing is already on the next calendar date. The NAV's
    # actual trade date, rather than its calendar-day status text, is decisive.
    assert not result_issues({"status": "ok", "rows": [{
        "status": "confirmed_stale_no_intraday_estimate", "confirmed_date": "2026-06-22",
        "current_date": "2026-06-23",
    }]}, target=date(2026, 6, 22))


def test_checkpoint_survives_restart_and_skips_completed_window(tmp_path):
    path = tmp_path / "state.json"
    calls = []
    callback = lambda target: calls.append(target) or {"status": "ok"}
    RefreshJournal(path).run("asia", date(2026, 6, 22), callback)
    result = RefreshJournal(path).run("asia", date(2026, 6, 22), callback)
    assert result["reason"] == "already_completed"
    assert len(calls) == 1
    assert read_state(path)["jobs"]["asia"]["history_coverage"] == "unverified"


def test_failed_window_is_retried_without_advancing_success(tmp_path):
    path = tmp_path / "state.json"
    journal = RefreshJournal(path)
    journal.run("asia", date(2026, 6, 22), lambda _: {"status": "blocked"})
    assert read_state(path)["jobs"]["asia"]["last_success_date"] is None
    result = journal.run("asia", date(2026, 6, 22), lambda _: {"status": "ok"})
    assert result["last_success_date"] == "2026-06-22"
    assert len(result["recent_runs"]) == 2


def test_historical_gap_is_preserved_after_latest_window_succeeds(tmp_path):
    journal = RefreshJournal(tmp_path / "state.json")
    journal.run("asia", date(2026, 6, 19), lambda _: {"status": "ok"})
    result = journal.run("asia", date(2026, 6, 24), lambda _: {"status": "ok"})
    assert result["history_gap_since"] == "2026-06-22"
    assert result["history_coverage"] == "gap"
    assert scheduler_health(journal.path)["status"] != "ok"


def test_checkpoint_corruption_prevents_callbacks(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("invalid", encoding="utf-8")
    with pytest.raises(ValueError):
        RefreshJournal(path).run("asia", date(2026, 6, 22), lambda _: pytest.fail("must not run"))
    assert scheduler_health(path)["status"] == "error"


def test_exception_does_not_store_provider_secrets(tmp_path):
    path = tmp_path / "state.json"
    def fail(_):
        raise ValueError("token=secret")
    RefreshJournal(path).run("asia", date(2026, 6, 22), fail)
    assert "secret" not in path.read_text()
    assert read_state(path)["jobs"]["asia"]["status"] == "error"


@pytest.mark.parametrize("now,expected", [
    (datetime(2026, 6, 22, 7, 4, tzinfo=timezone.utc), date(2026, 6, 19)),
    (datetime(2026, 6, 22, 7, 5, tzinfo=timezone.utc), date(2026, 6, 22)),
    (datetime(2026, 6, 21, 22, 0, tzinfo=timezone.utc), date(2026, 6, 19)),
])
def test_latest_due_window_excludes_weekends(now, expected):
    assert latest_due_date("15:05", "Asia/Shanghai", now) == expected


def test_us_cutoff_observes_dst():
    assert latest_due_date("16:30", "America/New_York", datetime(2026, 12, 1, 21, 29, tzinfo=timezone.utc)) == date(2026, 11, 30)
    assert latest_due_date("16:30", "America/New_York", datetime(2026, 6, 1, 20, 30, tzinfo=timezone.utc)) == date(2026, 6, 1)


def test_window_failure_never_builds_snapshots(monkeypatch):
    module = _load_scheduler(monkeypatch)
    monkeypatch.setattr(module, "refresh_investment_return_snapshots", lambda _: pytest.fail("must not publish"))
    result = module._refresh_window(date(2026, 6, 22), [("prices", lambda: {"found": 2, "updated": 1})], ("total",))
    assert result["status"] == "blocked"


def test_failed_section_snapshot_prevents_total_snapshot(monkeypatch):
    module = _load_scheduler(monkeypatch)
    calls = []
    monkeypatch.setattr(module, "_post_root", lambda endpoint, _label: calls.append(endpoint))
    result = module.refresh_investment_return_snapshots(("a_stock", "total"))
    assert result["status"] == "blocked"
    assert len(calls) == 1


def test_network_retry_is_bounded_and_recovers(monkeypatch):
    module = _load_scheduler(monkeypatch)
    calls = []
    def request(*_args, **_kwargs):
        calls.append(1)
        if len(calls) < 3:
            raise requests.Timeout()
        return type("Response", (), {"raise_for_status": lambda _: None, "json": lambda _: {"status": "ok"}})()
    monkeypatch.setattr(module.requests, "post", request)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    assert module._call("prices/test", "test") == {"status": "ok"}
    assert len(calls) == 3


def test_no_retry_on_bad_credentials(monkeypatch):
    module = _load_scheduler(monkeypatch)
    calls = []
    def request(*_args, **_kwargs):
        calls.append(1)
        response = requests.Response()
        response.status_code = 401
        raise requests.HTTPError(response=response)
    monkeypatch.setattr(module.requests, "post", request)
    assert module._call("test", "test") is None
    assert len(calls) == 1


def test_past_window_is_not_filled_with_current_quotes(monkeypatch, tmp_path):
    module = _load_scheduler(monkeypatch)
    monkeypatch.setattr(module, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(module, "refresh_post_close_investments", lambda _: pytest.fail("must not fetch current prices"))
    result = module.run_due_window("asia", datetime(2026, 6, 22, 7, 0, tzinfo=timezone.utc))
    assert result["status"] == "blocked"
    assert result["steps"][0]["issues"] == ["historical_backfill_required"]


def test_asia_flex_accepts_previous_trading_day(monkeypatch):
    module = _load_scheduler(monkeypatch)
    monkeypatch.setattr(module, "refresh_investment_return_snapshots", lambda _: {"status": "ok"})
    result = module._refresh_window(date(2026, 6, 22), [("flex", lambda: {"statement_as_of": "2026-06-19"})], ("a_stock", "total"))
    assert result["status"] == "ok"


def test_sync_center_flags_old_and_future_data(monkeypatch):
    from endpoints import sync_center
    monkeypatch.setattr(sync_center, "_now_local", lambda: datetime(2026, 10, 20, tzinfo=timezone.utc))
    assert sync_center._fresh_status("2026-06-22")[0] == "stale"
    assert sync_center._fresh_status("2026-10-21")[0] == "review"
    assert sync_center._fresh_status("2026-10-19")[0] == "ok"


@pytest.mark.parametrize("name", ["fx", "flex", "fund", "a_stock", "us"])
def test_http_success_without_required_data_blocks_publication(monkeypatch, name):
    module = _load_scheduler(monkeypatch)
    monkeypatch.setattr(module, "refresh_investment_return_snapshots", lambda _: pytest.fail("must not publish"))
    result = module._refresh_window(date(2026, 6, 22), [(name, lambda: {"status": "ok"})], ("total",))
    assert result["status"] == "blocked"


def test_empty_snapshot_is_not_a_success(monkeypatch):
    module = _load_scheduler(monkeypatch)
    monkeypatch.setattr(module, "_post_root", lambda *_: {"status": "ok", "written": 0})
    assert module.refresh_investment_return_snapshots(("total",))["status"] == "blocked"


def test_checkpoint_write_failure_blocks_refresh(monkeypatch, tmp_path):
    module = _load_scheduler(monkeypatch)
    path = tmp_path / "state.json"
    path.mkdir()
    monkeypatch.setattr(module, "STATE_PATH", path)
    monkeypatch.setattr(module, "refresh_post_close_investments", lambda _: pytest.fail("must not run"))
    assert module.run_due_window("asia", datetime(2026, 6, 22, 8, tzinfo=timezone.utc))["status"] == "error"
