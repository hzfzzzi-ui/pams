"""Automated evidence publication must never import facts or rewrite old closes."""
from contextlib import nullcontext
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend/api"))
sys.path.insert(0, str(ROOT / "backend/scheduler"))
from domain.brokers.ibkr import ibkr_daily_close_sync as service
from domain.brokers.ibkr import ibkr_daily_close_read as reader
from domain.brokers.ibkr.ibkr_daily_close_publish import publish
from domain.brokers.ibkr.ibkr_report_state import PublishedReport
from tests.brokers.ibkr.test_ibkr_daily_close_read import xml, ACCOUNT, DAY
from tests.scheduler.test_asset_refresh_scheduler import _load_scheduler

NEXT = date(2024, 1, 3)


def daily_report():
    root = ET.fromstring(xml())
    next(root.iter("ChangeInNAV")).set("fromDate", "20240102")
    next(root.iter("EquitySummaryByReportDateInBase")).set("reportDate", "20240101")
    return ET.tostring(root, encoding="utf-8")


def advanced():
    root = ET.fromstring(daily_report())
    statement = deepcopy(list(root)[0])
    statement.set("fromDate", "20240103")
    statement.set("toDate", "20240103")
    rows = list(statement.iter("EquitySummaryByReportDateInBase"))
    equity = next(statement.iter("EquitySummaryInBase"))
    equity.remove(rows[0])
    previous = deepcopy(rows[1])
    rows[1].set("reportDate", "20240103")
    equity.insert(0, previous)
    flow = next(statement.iter("ChangeInNAV"))
    flow.set("fromDate", "20240103")
    flow.set("toDate", "20240103")
    flow.set("startingValue", "99")
    flow.set("endingValue", "99")
    flow.set("depositsWithdrawals", "0")
    root.append(statement)
    return ET.tostring(root, encoding="utf-8")


@pytest.fixture
def configured(tmp_path, monkeypatch):
    output = publish(daily_report(), directory=tmp_path, account_id=ACCOUNT, as_of=DAY, period_start=DAY)
    monkeypatch.setenv(reader.MANIFEST_ENV, output["manifest"])
    monkeypatch.setenv("IBKR_FLEX_TOKEN", "SECRET_TEST_TOKEN")
    monkeypatch.setenv("IBKR_FLEX_POSITIONS_QUERY_ID", "100")
    monkeypatch.setenv(service.QUERY_ENV, "200")
    monkeypatch.setattr(service, "expected_statement_date", lambda reference=None: NEXT)
    production = tmp_path / "production.xml"
    production.write_bytes(advanced())
    report = PublishedReport(production, NEXT, 1, hashlib.sha256(production.read_bytes()).hexdigest(), None)
    quality = {"status": "ok", "legacy_unverified": False, "statement_as_of": str(NEXT),
               "expected_statement_as_of": str(NEXT)}
    monkeypatch.setattr(service.performance, "get_conn", lambda: nullcontext(None))
    monkeypatch.setattr(service, "select_report", lambda *a: (report, {}))
    monkeypatch.setattr(service.performance.report_state, "report_quality", lambda *a, **k: quality)
    monkeypatch.setattr(service.performance, "published_report_status", lambda: quality)
    monkeypatch.setattr(service, "request_flex_reference", lambda *a: {"reference_code": "reference"})
    monkeypatch.setattr(service, "fetch_flex_statement", lambda *a: advanced().decode())
    monkeypatch.setattr(service.performance, "_parse_flex_performance", lambda *a, **k: {
        "status": "ok", "as_of": str(NEXT), "base_currency": "USD", "cash_base": 100,
        "position_value_base": 0, "unrealized_pnl_base": 0, "realized_pnl_base": 0,
        "gross_exposure_base": 0, "negative_cash_base": 0})
    return tmp_path


def test_advance_is_atomic_idempotent_and_ledger_free(configured, monkeypatch):
    before = Path(configured / "daily-close.json").read_bytes()
    assert not service.daily_close_status()["publication_verified"]
    result = service.sync_daily_close()
    assert result["success"] and result["facts_written"] == 0 and result["unchanged"] is False, result
    assert service.daily_close_status()["publication_verified"]
    after = Path(configured / "daily-close.json").read_bytes()
    assert before != after
    monkeypatch.setattr(service, "request_flex_reference", lambda *a: pytest.fail("already confirmed"))
    assert service.sync_daily_close()["unchanged"] is True
    assert Path(configured / "daily-close.json").read_bytes() == after


def test_historical_revision_is_not_an_automatic_repair(configured, monkeypatch):
    before = Path(configured / "daily-close.json").read_bytes()
    root = ET.fromstring(advanced())
    first = list(root)[0]
    row = list(first.iter("EquitySummaryByReportDateInBase"))[-1]
    row.set("interestAccruals", "-3")
    row.set("interestAccrualsShort", "-3")
    row.set("total", "98")
    row.set("totalLong", "98")
    next(first.iter("ChangeInNAV")).set("endingValue", "98")
    second = list(root)[1]
    prior = list(second.iter("EquitySummaryByReportDateInBase"))[0]
    prior.attrib.update(row.attrib)
    next(second.iter("ChangeInNAV")).set("startingValue", "98")
    monkeypatch.setattr(service, "fetch_flex_statement", lambda *a: ET.tostring(root, encoding="unicode"))
    result = service.sync_daily_close()
    assert not result["success"] and result["reason"] == "publication_historical_revision_requires_review", result
    assert Path(configured / "daily-close.json").read_bytes() == before


@pytest.mark.parametrize("failure", ["download", "scope", "changed_production", "disk"])
def test_failed_acquisition_preserves_previous_close(configured, monkeypatch, failure):
    manifest = configured / "daily-close.json"
    before = manifest.read_bytes()
    if failure == "download":
        def fail(*a):
            raise RuntimeError("SECRET_TEST_TOKEN in provider URL")
        monkeypatch.setattr(service, "fetch_flex_statement", fail)
    elif failure == "scope":
        monkeypatch.setattr(service, "fetch_flex_statement", lambda *a: advanced().decode().replace(ACCOUNT, "OTHER"))
    elif failure == "changed_production":
        (configured / "production.xml").write_bytes(b"changed")
    else:
        monkeypatch.setattr(service, "publish", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    result = service.sync_daily_close()
    assert not result["success"] and result["facts_written"] == 0
    assert "SECRET_TEST_TOKEN" not in json.dumps(result)
    assert manifest.read_bytes() == before


def test_disabled_or_production_query_cannot_download(configured, monkeypatch):
    monkeypatch.setattr(service, "request_flex_reference", lambda *a: pytest.fail("must not download"))
    monkeypatch.setenv(service.QUERY_ENV, "00100")
    assert service.sync_daily_close()["reason"] == "daily_close_not_configured"
    monkeypatch.delenv(service.QUERY_ENV)
    assert not service.daily_close_status()["configured"]


def test_close_confirmation_reuses_journal_and_never_calls_ledger_writers(tmp_path, monkeypatch):
    module = _load_scheduler(monkeypatch)
    monkeypatch.setenv("PAMS_IBKR_DAILY_CLOSE_SYNC_ENABLED", "true")
    monkeypatch.setattr(module, "STATE_PATH", tmp_path / "state.json")
    payload = {"source": "ibkr_daily_close", "configured": True, "publication_verified": False,
        "source_sha256": "a" * 64, "report_quality": {"status": "waiting",
        "expected_statement_as_of": str(NEXT), "statement_as_of": str(DAY)}}
    calls = []
    def request(method, url, label, **kwargs):
        calls.append((method, url))
        assert "/ibkr/daily-close/" in url
        if method == "post":
            payload["publication_verified"] = True
            payload["report_quality"].update(status="ok", statement_as_of=str(NEXT))
            return {"success": True}
        return deepcopy(payload)
    monkeypatch.setattr(module, "_request_json", request)
    now = datetime(2024, 1, 4, tzinfo=timezone.utc)
    assert module.run_daily_close_confirmation(now=now)["success"]
    assert module.run_daily_close_confirmation(now=now + timedelta(minutes=30))["success"]
    assert sum(method == "post" for method, _ in calls) == 1
    state = json.loads((tmp_path / "state.json").read_text())
    assert set(state["jobs"]) == {"ibkr_daily_close"}


def test_default_disabled_close_task_does_no_io(tmp_path, monkeypatch):
    module = _load_scheduler(monkeypatch)
    monkeypatch.delenv("PAMS_IBKR_DAILY_CLOSE_SYNC_ENABLED", raising=False)
    monkeypatch.setattr(module, "_request_json", lambda *a, **k: pytest.fail("disabled"))
    assert module.run_daily_close_confirmation()["reason"] == "daily_close_sync_disabled"


def test_unconfirmed_production_cannot_advance_close(configured, monkeypatch):
    monkeypatch.setattr(service, "select_report", lambda *a: (None, None))
    monkeypatch.setattr(service, "request_flex_reference", lambda *a: pytest.fail("production not confirmed"))
    before = (configured / "daily-close.json").read_bytes()
    assert service.sync_daily_close()["reason"] == "production_report_not_confirmed"
    assert (configured / "daily-close.json").read_bytes() == before


def test_close_recovery_uses_its_own_checkpoint_and_does_not_hide_failures(tmp_path, monkeypatch):
    module = _load_scheduler(monkeypatch)
    monkeypatch.setenv("PAMS_IBKR_DAILY_CLOSE_SYNC_ENABLED", "true")
    monkeypatch.setattr(module, "STATE_PATH", tmp_path / "state.json")
    now = datetime(2024, 1, 4, tzinfo=timezone.utc)
    calls = []
    def request(method, url, label, **kwargs):
        calls.append(method)
        if method == "post":
            return {"success": False, "status": "waiting", "reason": "report_delayed"}
        return {"source": "ibkr_daily_close", "configured": True, "publication_verified": False,
                "report_quality": {"expected_statement_as_of": str(NEXT), "statement_as_of": str(DAY)}}
    monkeypatch.setattr(module, "_request_json", request)
    assert not module.run_daily_close_confirmation(now=now)["success"]
    assert not module.run_daily_close_confirmation(now=now + timedelta(minutes=10))["success"]
    assert calls.count("post") == 1
    assert not module.run_daily_close_confirmation(now=now + timedelta(minutes=31))["success"]
    assert calls.count("post") == 2


def test_legacy_get_routes_do_not_create_schema():
    import ast
    tree = ast.parse((ROOT / "backend/api/endpoints/ibkr.py").read_text(encoding="utf-8-sig"))
    for function in tree.body:
        if not isinstance(function, ast.FunctionDef):
            continue
        is_get = any(isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                     and decorator.func.attr == "get" for decorator in function.decorator_list)
        if is_get:
            assert not any(isinstance(node, ast.Name) and node.id.startswith("ensure_") for node in ast.walk(function))


@pytest.mark.parametrize("same_day", [False, True])
def test_equal_amounts_from_another_account_never_confirm_or_publish(configured, monkeypatch, same_day):
    target = DAY if same_day else NEXT
    content = daily_report() if same_day else advanced()
    path = configured / "production.xml"
    path.write_bytes(content.replace(ACCOUNT.encode(), b"OTHER_ACCOUNT"))
    report = PublishedReport(path, target, 1, hashlib.sha256(path.read_bytes()).hexdigest(), None)
    quality = {"status": "ok", "legacy_unverified": False, "statement_as_of": str(target),
               "expected_statement_as_of": str(target)}
    monkeypatch.setattr(service, "select_report", lambda *a: (report, {}))
    monkeypatch.setattr(service.performance.report_state, "report_quality", lambda *a, **k: quality)
    monkeypatch.setattr(service, "expected_statement_date", lambda reference=None: target)
    monkeypatch.setattr(service, "request_flex_reference", lambda *a: pytest.fail("foreign account"))
    before = (configured / "daily-close.json").read_bytes()
    assert not service.daily_close_status()["publication_verified"]
    assert service.sync_daily_close()["reason"] == "production_account_or_base_mismatch"
    assert (configured / "daily-close.json").read_bytes() == before


def test_changed_old_external_flow_cannot_hide_behind_unchanged_nav(configured, monkeypatch):
    root = ET.fromstring(advanced())
    next(root.iter("ChangeInNAV")).set("depositsWithdrawals", "30")
    monkeypatch.setattr(service, "fetch_flex_statement", lambda *a: ET.tostring(root, encoding="unicode"))
    before = (configured / "daily-close.json").read_bytes()
    result = service.sync_daily_close()
    assert result["reason"] == "publication_historical_revision_requires_review", result
    assert (configured / "daily-close.json").read_bytes() == before


def test_history_block_reason_survives_in_checkpoint_and_existing_health(tmp_path, monkeypatch):
    module = _load_scheduler(monkeypatch)
    monkeypatch.setenv("PAMS_IBKR_DAILY_CLOSE_SYNC_ENABLED", "true")
    path = tmp_path / "state.json"
    monkeypatch.setattr(module, "STATE_PATH", path)
    reason = "publication_historical_revision_requires_review"
    def request(method, *a, **k):
        if method == "post":
            return {"status": "blocked", "reason": reason}
        return {"source": "ibkr_daily_close", "configured": True, "publication_verified": False,
            "report_quality": {"expected_statement_as_of": str(NEXT), "statement_as_of": str(DAY)}}
    monkeypatch.setattr(module, "_request_json", request)
    now = datetime(2024, 1, 4, tzinfo=timezone.utc)
    result = module.run_daily_close_confirmation(now=now)
    assert result["status"] == "blocked" and result["reason"] == reason
    job = json.loads(path.read_text())["jobs"]["ibkr_daily_close"]
    assert job["recent_runs"][-1]["reason"] == reason
    from domain.operations.sync_health import scheduler_health
    assert "历史资金流或收益修订已隔离" in scheduler_health(path, now)["jobs"]["ibkr_daily_close"]["display_detail"]
