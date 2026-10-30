"""Independent normal-event retries must not reopen official close confirmation."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import pytest

from tests.scheduler.test_legacy_strategy_scheduler import _load_scheduler

NOW = datetime(2026, 10, 23, 22, tzinfo=timezone.utc)


def payload(**changes):
    return {"status": "ready", "confirmed_day": "2026-10-23", "source_sha256": "a" * 64,
            "queued_day_count": 0, "retryable_pending_count": 0, "pending": [], **changes}


def test_missing_fx_retries_without_touching_official_report_or_close_jobs(tmp_path, monkeypatch):
    module = _load_scheduler(monkeypatch)
    monkeypatch.setenv("PAMS_IBKR_CASH_EVENTS_SYNC_ENABLED", "true")
    state_path = tmp_path / "refresh.json"
    monkeypatch.setattr(module, "STATE_PATH", state_path)
    official = {"status": "success", "last_success_date": "2026-10-23", "recent_runs": []}
    state_path.write_text(json.dumps({"version": 1, "jobs": {
        "ibkr_flex": official, "ibkr_daily_close": official}}))
    data = payload(queued_day_count=1, retryable_pending_count=1,
                   pending=[{"reason": "missing_or_invalid_dated_fx", "retryable": True}])
    calls = []
    available = [False]
    def request(method, url, label, **kwargs):
        assert "/ibkr/cash-events/" in url
        calls.append(method)
        if method == "post":
            assert kwargs["json"] == {}
            if available[0]:
                data.update(queued_day_count=0, retryable_pending_count=0, pending=[])
            return {"status": "success", "facts_written": int(available[0])}
        return deepcopy(data)
    monkeypatch.setattr(module, "_request_json", request)
    assert not module.run_cash_event_confirmation(now=NOW)["success"]
    assert not module.run_cash_event_confirmation(now=NOW + timedelta(minutes=10))["success"]
    assert calls.count("post") == 1
    available[0] = True
    assert module.run_cash_event_confirmation(now=NOW + timedelta(minutes=31))["success"]
    assert calls.count("post") == 2
    jobs = json.loads(state_path.read_text())["jobs"]
    assert jobs["ibkr_flex"] == jobs["ibkr_daily_close"] == official
    assert jobs["ibkr_cash_events"]["scope"] == "post_cutoff_cash_facts"
    assert jobs["ibkr_cash_events"]["reason"] == "cash_events_confirmed"


def test_pending_bank_link_does_not_require_manual_approval_of_broker_fact(tmp_path, monkeypatch):
    module = _load_scheduler(monkeypatch)
    monkeypatch.setenv("PAMS_IBKR_CASH_EVENTS_SYNC_ENABLED", "true")
    monkeypatch.setattr(module, "STATE_PATH", tmp_path / "state.json")
    data = payload(pending=[{"reason": "bank_match_pending", "retryable": False}])
    def request(method, *args, **kwargs):
        assert method == "get"
        return data
    monkeypatch.setattr(module, "_request_json", request)
    assert module.run_cash_event_confirmation(now=NOW)["success"]


@pytest.mark.parametrize("change", [
    {"status": "unavailable"}, {"confirmed_day": None}, {"source_sha256": None},
    {"queued_day_count": 1}, {"retryable_pending_count": 1},
    {"pending": [{"reason": "unsupported_cash_transaction_type"}]},
])
def test_incomplete_cash_work_cannot_be_green(monkeypatch, change):
    module = _load_scheduler(monkeypatch)
    assert not module._cash_event_info(payload(**change))[2]


def test_cash_default_disabled_has_no_network_or_journal_io(tmp_path, monkeypatch):
    module = _load_scheduler(monkeypatch)
    monkeypatch.delenv("PAMS_IBKR_CASH_EVENTS_SYNC_ENABLED", raising=False)
    monkeypatch.setattr(module, "_request_json", lambda *a, **k: pytest.fail("disabled"))
    assert module.run_cash_event_confirmation()["status"] == "skipped"


def test_new_jobs_visible_in_existing_health_and_history_not_cleared(tmp_path):
    from domain.operations.sync_health import scheduler_health
    path = tmp_path / "state.json"
    job = {"status": "waiting", "last_checked_at": NOW.isoformat(), "target_date": "2026-10-23",
           "reason": "report_unconfirmed", "next_retry_at": (NOW + timedelta(minutes=30)).isoformat()}
    path.write_text(json.dumps({"version": 1, "jobs": {"ibkr_daily_close": job, "ibkr_cash_events": job}}))
    before = path.read_bytes()
    result = scheduler_health(path, NOW)
    assert "ibkr_daily_close:pending" in result["issues"]
    assert "ibkr_cash_events:pending" in result["issues"]
    assert "asia:history_unverified" in result["issues"]
    assert "IBKR 资金事实" in result["jobs"]["ibkr_cash_events"]["display_detail"]
    assert path.read_bytes() == before
