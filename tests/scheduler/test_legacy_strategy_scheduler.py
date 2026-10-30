from __future__ import annotations

import importlib.util
import sys
import types
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEDULER_PATH = REPO_ROOT / "backend" / "scheduler" / "scheduler.py"


def _load_scheduler(monkeypatch):
    monkeypatch.syspath_prepend(str(SCHEDULER_PATH.parent))
    fake_schedule = types.SimpleNamespace(
        every=lambda *_args, **_kwargs: types.SimpleNamespace(
            day=types.SimpleNamespace(at=lambda _time, _tz=None: types.SimpleNamespace(do=lambda _fn: None)),
            friday=types.SimpleNamespace(at=lambda _time, _tz=None: types.SimpleNamespace(do=lambda _fn: None)),
            minutes=types.SimpleNamespace(do=lambda _fn: None),
        ),
        run_pending=lambda: None,
    )
    monkeypatch.setitem(sys.modules, "schedule", fake_schedule)
    spec = importlib.util.spec_from_file_location("pams_scheduler_under_test", SCHEDULER_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module






def test_post_close_investment_scheduler_job_calls_price_endpoints(monkeypatch) -> None:
    module = _load_scheduler(monkeypatch)
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(module, "_call", lambda endpoint, label: calls.append((endpoint, label)) or {"status": "ok", "stored": 1, "found": 0, "updated": 0, "processed": 0, "rows": []})
    monkeypatch.setattr(module, "_get", lambda endpoint, label: calls.append((endpoint, label)))
    monkeypatch.setattr(module, "_post_root", lambda endpoint, label: calls.append((endpoint, label)) or {"status": "ok", "written": 1})
    monkeypatch.setattr(module, "run_flex_confirmation", lambda: calls.append(("ibkr/flex/sync", "IBKR Flex")) or {"status": "ok", "statement_as_of": "2099-01-01"})

    module.refresh_post_close_investments()

    assert [endpoint for endpoint, _label in calls] == [
        "fx-rates/refresh",
        "ibkr/flex/sync",
        "prices/refresh/a-stock",
        "prices/refresh/us-stock?market=kr&priceBasis=close",
        "prices/refresh/fund-nav-estimates?useIntradayEstimate=true",
        "prices/refresh/us-stock?market=hk&priceBasis=estimate",
        "prices/refresh/us-stock?market=us&priceBasis=estimate&scheduled=true",
        "prices/refresh/precious-metal",
        "investment/section-return-series/snapshot?section=a_stock&start=2026-01-01&interval=daily&baseCurrency=CNY",
        "investment/section-return-series/snapshot?section=a_fund&start=2026-01-01&interval=daily&baseCurrency=CNY",
        "investment/section-return-series/snapshot?section=us_stock&start=2026-01-01&interval=daily&baseCurrency=CNY",
        "investment/section-return-series/snapshot?section=precious_metal&start=2026-01-01&interval=daily&baseCurrency=CNY",
        "investment/return-series/snapshot?start=2026-01-01&interval=daily&baseCurrency=CNY",
    ]
    assert all("refreshSnapshot" not in endpoint for endpoint, _label in calls)


def test_north_america_post_close_scheduler_job_calls_us_endpoints(monkeypatch) -> None:
    module = _load_scheduler(monkeypatch)
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(module, "_call", lambda endpoint, label: calls.append((endpoint, label)) or {"status": "ok", "stored": 1, "found": 0, "updated": 0, "processed": 0, "rows": []})
    monkeypatch.setattr(module, "_get", lambda endpoint, label: calls.append((endpoint, label)))
    monkeypatch.setattr(module, "_post_root", lambda endpoint, label: calls.append((endpoint, label)) or {"status": "ok", "written": 1})
    monkeypatch.setattr(module, "run_flex_confirmation", lambda: calls.append(("ibkr/flex/sync", "IBKR Flex")) or {"status": "ok", "statement_as_of": "2099-01-01"})

    module.refresh_north_america_post_close_investments()

    assert [endpoint for endpoint, _label in calls] == [
        "fx-rates/refresh",
        "ibkr/flex/sync",
        "prices/refresh/fund-nav-estimates?useIntradayEstimate=false",
        "prices/refresh/us-stock?market=hk&priceBasis=close",
        "prices/refresh/us-stock?market=us&priceBasis=close&scheduled=true",
        "investment/section-return-series/snapshot?section=a_fund&start=2026-01-01&interval=daily&baseCurrency=CNY",
        "investment/section-return-series/snapshot?section=us_stock&start=2026-01-01&interval=daily&baseCurrency=CNY",
        "investment/section-return-series/snapshot?section=precious_metal&start=2026-01-01&interval=daily&baseCurrency=CNY",
        "investment/return-series/snapshot?start=2026-01-01&interval=daily&baseCurrency=CNY",
    ]
    assert all("refreshSnapshot" not in endpoint for endpoint, _label in calls)


def test_post_close_investment_startup_catch_up_uses_asia_shanghai_cutoff(monkeypatch) -> None:
    module = _load_scheduler(monkeypatch)

    assert module.should_catch_up_post_close_investments(datetime(2026, 6, 1, 7, 4, tzinfo=timezone.utc)) is False
    assert module.should_catch_up_post_close_investments(datetime(2026, 6, 1, 7, 5, tzinfo=timezone.utc)) is True
    assert module.should_catch_up_post_close_investments(datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc)) is True


def test_north_america_startup_catch_up_uses_new_york_cutoff(monkeypatch) -> None:
    module = _load_scheduler(monkeypatch)

    assert module.should_catch_up_north_america_investments(datetime(2026, 6, 1, 20, 29, tzinfo=timezone.utc)) is False
    assert module.should_catch_up_north_america_investments(datetime(2026, 6, 1, 20, 30, tzinfo=timezone.utc)) is True
    assert module.should_catch_up_north_america_investments(datetime(2026, 12, 1, 21, 29, tzinfo=timezone.utc)) is False
    assert module.should_catch_up_north_america_investments(datetime(2026, 12, 1, 21, 30, tzinfo=timezone.utc)) is True
