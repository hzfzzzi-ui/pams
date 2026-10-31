"""Official Flex acquisition must not invoke current-price or historical NAV writers."""
from datetime import datetime, timedelta, timezone
import json
import multiprocessing
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend/scheduler"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend/api"))
from flex_confirmation import FlexConfirmationJournal
from refresh_state import RefreshJournal, read_state, write_state
from tests.scheduler.test_asset_refresh_scheduler import _load_scheduler

NOW = datetime(2026, 10, 23, 0, tzinfo=timezone.utc)


def status(day="2026-10-21", *, due="2026-10-22", verified=False):
    return {"configured": True, "report_quality": {
        "statement_as_of": day, "expected_statement_as_of": due,
        "legacy_unverified": not verified, "active_run_id": 10,
        "status": "ok" if verified else "stale",
    }}


def test_late_report_recovers_without_rewriting_history_state(tmp_path):
    path = tmp_path / "state.json"
    old = {"status": "blocked", "history_gap_since": "2026-10-20", "history_coverage": "gap",
           "last_success_date": None, "recent_runs": [], "user_note": "keep"}
    write_state(path, {"version": 1, "jobs": {"asia": old, "north_america": old}})
    probe = {"value": status()}
    calls = []
    def sync(target):
        calls.append(str(target))
        return {"success": False, "status": "blocked", "reason": "stale_statement_date"}
    task = FlexConfirmationJournal(path)
    result = task.run(lambda: probe["value"], sync, now=NOW)
    assert result["status"] == "waiting" and result["success"] is False
    assert read_state(path)["jobs"]["ibkr_flex"]["last_success_date"] is None
    task.run(lambda: probe["value"], sync, now=NOW + timedelta(minutes=10))
    assert len(calls) == 1
    def arrived(target):
        calls.append(str(target))
        probe["value"] = status("2026-10-22", verified=True)
        return {"success": True, "status": "success"}
    assert task.run(lambda: probe["value"], arrived, now=NOW + timedelta(minutes=31))["success"]
    assert task.run(lambda: probe["value"], arrived, now=NOW + timedelta(hours=1))["success"]
    assert len(calls) == 2
    jobs = read_state(path)["jobs"]
    assert jobs["asia"] == jobs["north_america"] == old
    assert jobs["ibkr_flex"]["last_success_date"] == "2026-10-22"


def test_lost_sync_response_is_recovered_from_committed_publication(tmp_path):
    probe = {"value": status()}
    def sync(_):
        probe["value"] = status("2026-10-22", verified=True)
        raise TimeoutError("token=do-not-store")
    path = tmp_path / "state.json"
    result = FlexConfirmationJournal(path).run(lambda: probe["value"], sync, now=NOW)
    assert result["success"] and "do-not-store" not in path.read_text()


def test_unverified_legacy_report_cannot_advance_success(tmp_path):
    path = tmp_path / "state.json"
    result = FlexConfirmationJournal(path).run(lambda: status("2026-10-22"), lambda _: {"success": True}, now=NOW)
    assert not result["success"]
    assert read_state(path)["jobs"]["ibkr_flex"]["last_success_date"] is None


@pytest.mark.parametrize("http_status", [401, 403, 429, 503])
def test_http_failure_is_persisted_with_bounded_retry_and_no_secrets(tmp_path, http_status):
    path = tmp_path / "state.json"
    response = {"success": False, "status": "error", "http_status": http_status,
                "retry_after": (NOW + timedelta(hours=8)).isoformat() if http_status == 429 else None,
                "error": "token=secret"}
    calls = []
    def sync(_):
        calls.append(1)
        return response
    task = FlexConfirmationJournal(path)
    result = task.run(lambda: status(), sync, now=NOW)
    assert not result["success"] and result["next_retry_at"]
    task.run(lambda: status(), sync, now=NOW + timedelta(minutes=10))
    assert len(calls) == 1 and "secret" not in path.read_text()
    if http_status == 429:
        assert datetime.fromisoformat(result["next_retry_at"]) >= NOW + timedelta(hours=8)


def test_checkpoint_write_failure_prevents_sync(tmp_path, monkeypatch):
    import flex_confirmation
    def fail(*a):
        raise OSError("disk full")
    monkeypatch.setattr(flex_confirmation, "store_job", fail)
    with pytest.raises(OSError):
        FlexConfirmationJournal(tmp_path / "state.json").run(
            lambda: status(), lambda _: pytest.fail("must not write before checkpoint"), now=NOW,
        )


def test_window_checkpoint_does_not_overwrite_nested_flex_confirmation(tmp_path):
    path = tmp_path / "state.json"
    def callback(_):
        assert FlexConfirmationJournal(path).run(lambda: status("2026-10-22", verified=True),
            lambda _: pytest.fail("already committed"), now=NOW)["success"]
        return {"status": "blocked", "steps": [{"name": "history", "issues": ["historical_backfill_required"]}]}
    RefreshJournal(path).run("north_america", NOW.date(), callback)
    jobs = read_state(path)["jobs"]
    assert jobs["ibkr_flex"]["last_success_date"] == "2026-10-22"
    assert jobs["north_america"]["status"] == "blocked"


def test_late_task_runs_independently_of_historical_quote_guard(tmp_path, monkeypatch):
    module = _load_scheduler(monkeypatch)
    monkeypatch.setattr(module, "STATE_PATH", tmp_path / "state.json")
    def no_quotes(*a, **k):
        pytest.fail("official confirmation must not call price/NAV writers")
    monkeypatch.setattr(module, "_call", no_quotes)
    monkeypatch.setattr(module, "_post_root", no_quotes)
    monkeypatch.setattr(module, "_flex_status", lambda: status("2026-10-22", verified=True))
    assert module.run_due_window("north_america", NOW)["status"] == "blocked"
    assert module.run_flex_confirmation(now=NOW)["success"]


def test_corrupt_checkpoint_fails_closed(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"version": 1, "jobs": {"ibkr_flex": {"attempt_count": -1}}}))
    with pytest.raises(ValueError):
        FlexConfirmationJournal(path).run(lambda: pytest.fail("must not read source"), lambda _: None, now=NOW)


def _process_sync(path, started, release, crash=False):
    def sync(_):
        with path.with_suffix(".calls").open("a") as stream:
            stream.write("POST\n")
        started.set()
        if crash:
            path.with_suffix(".published").touch()
            os._exit(17)
        assert release.wait(15)
        return {"status": "blocked", "reason": "stale_statement_date"}
    FlexConfirmationJournal(path).run(lambda: status(), sync, now=NOW)


def test_cross_process_mutual_exclusion_and_crash_recovery(tmp_path):
    context = multiprocessing.get_context("spawn")
    path = tmp_path / "state.json"
    for crash in (False, True):
        started, release = context.Event(), context.Event()
        process = context.Process(target=_process_sync, args=(path, started, release, crash))
        if path.exists():
            path.unlink()
        process.start()
        try:
            assert started.wait(15)
            if crash:
                process.join(15)
                assert process.exitcode == 17 and path.with_suffix(".published").exists()
                result = FlexConfirmationJournal(path).run(
                    lambda: status("2026-10-22", verified=True),
                    lambda _: pytest.fail("committed report must not be fetched again"), now=NOW,
                )
                assert result["success"]
                assert read_state(path)["jobs"]["ibkr_flex"]["recovered_interrupted"]
            else:
                result = FlexConfirmationJournal(path).run(
                    lambda: pytest.fail("must not enter locked task"), lambda _: None, now=NOW,
                )
                assert result["reason"] == "job_busy"
        finally:
            release.set()
            process.join(15)
            if process.is_alive():
                process.terminate()
                process.join(5)
        assert process.exitcode == (17 if crash else 0)
    assert path.with_suffix(".calls").read_text().splitlines() == ["POST", "POST"]


@pytest.mark.parametrize("bad", [True, -1, "1"])
def test_invalid_attempt_count_cannot_contact_source(tmp_path, bad):
    path = tmp_path / "state.json"
    write_state(path, {"version": 1, "jobs": {"ibkr_flex": {"attempt_count": bad}}})
    with pytest.raises(ValueError):
        FlexConfirmationJournal(path).run(lambda: pytest.fail("invalid checkpoint"), lambda _: None, now=NOW)


@pytest.mark.parametrize("value", ["2026-10-23T00:00:00", "bad", 123])
def test_invalid_retry_time_fails_closed(tmp_path, value):
    path = tmp_path / "state.json"
    write_state(path, {"version": 1, "jobs": {"ibkr_flex": {"next_retry_at": value}}})
    with pytest.raises(ValueError):
        FlexConfirmationJournal(path).run(lambda: pytest.fail("invalid checkpoint"), lambda _: None, now=NOW)


def test_retry_backoff_is_bounded_and_audit_retention_is_capped(tmp_path):
    path = tmp_path / "state.json"
    task = FlexConfirmationJournal(path)
    current = NOW
    delays = []
    for _ in range(25):
        result = task.run(lambda: status(), lambda _: {"reason": "stale_statement_date"}, now=current)
        retry = datetime.fromisoformat(result["next_retry_at"])
        delays.append((retry - current).total_seconds() / 60)
        current = retry
    assert delays[:6] == [30, 60, 120, 240, 360, 360]
    assert max(delays) == 360
    assert len(read_state(path)["jobs"]["ibkr_flex"]["recent_runs"]) == 20


def test_boolean_publication_id_and_regressed_report_never_confirm(tmp_path):
    path = tmp_path / "state.json"
    value = status("2026-10-22", verified=True)
    value["report_quality"]["active_run_id"] = True
    assert not FlexConfirmationJournal(path).run(lambda: value, lambda _: {"success": True}, now=NOW)["success"]
    write_state(path, {"version": 1, "jobs": {"ibkr_flex": {"last_success_date": "2026-10-23"}}})
    result = FlexConfirmationJournal(path).run(lambda: status("2026-10-22", verified=True),
        lambda _: pytest.fail("regression must not download"), now=NOW)
    assert result["reason"] == "report_date_regression"
    assert read_state(path)["jobs"]["ibkr_flex"]["last_success_date"] == "2026-10-23"


def test_http_retry_after_is_preserved_without_immediate_post_retry(monkeypatch):
    import requests
    module = _load_scheduler(monkeypatch)
    monkeypatch.setattr(module, "_flex_status", lambda: {"configured": True})
    calls = []
    def fail(*a, **k):
        calls.append(k["json"])
        response = requests.Response()
        response.status_code = 429
        response.headers["Retry-After"] = "Fri, 23 Oct 2026 18:00:00 GMT"
        raise requests.HTTPError("token=secret", response=response)
    monkeypatch.setattr(module.requests, "post", fail)
    monkeypatch.setattr(module.time, "sleep", lambda _: pytest.fail("must not retry POST immediately"))
    result = module.sync_ibkr_flex(NOW.date())
    assert len(calls) == 1 and result["http_status"] == 429
    assert result["retry_after"] == "2026-10-23T18:00:00+00:00"
    assert "secret" not in str(result) and "as_of" not in calls[0]
    retry = datetime.fromisoformat(module._retry_after("3600"))
    assert 3595 <= (retry - datetime.now(timezone.utc)).total_seconds() <= 3600
    assert module._retry_after("invalid") is None


@pytest.mark.parametrize("failure,reason", [
    ({"configured": False}, "authorization_required"),
    ({"http_status": 401}, "authorization_required"),
    ({"http_status": 503}, "source_unavailable"),
    ({"report_quality": {"expected_statement_as_of": "invalid"}}, "source_unavailable"),
])
def test_probe_failure_never_posts(tmp_path, failure, reason):
    result = FlexConfirmationJournal(tmp_path / "state.json").run(lambda: failure,
        lambda _: pytest.fail("failed metadata must not initiate sync"), now=NOW)
    assert result["reason"] == reason and not result["success"]


def test_flex_health_details_preserve_history_warnings_and_do_not_write(tmp_path):
    from domain.operations.sync_health import scheduler_health
    path = tmp_path / "state.json"
    FlexConfirmationJournal(path).run(lambda: status(),
        lambda _: {"http_status": 429}, now=NOW)
    before = path.read_bytes()
    health = scheduler_health(path, NOW)
    assert health["status"] != "ok" and "asia:history_unverified" in health["issues"]
    detail = health["jobs"]["ibkr_flex"]["display_detail"]
    assert "接口限流" in detail and "下次重试" in detail and "不代表历史行情已补齐" in detail
    assert path.read_bytes() == before
    assert "ibkr_flex:stale" in scheduler_health(path, NOW + timedelta(hours=3))["issues"]
