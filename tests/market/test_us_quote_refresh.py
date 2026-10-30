"""Independent quote polling must not bypass evidence or trade-date gates."""
from datetime import date, datetime, timedelta, timezone
import json

import pytest

from tests.scheduler.test_legacy_strategy_scheduler import _load_scheduler


NOW = datetime(2026, 10, 26, 20, 40, tzinfo=timezone.utc)


@pytest.mark.parametrize("failed", ["flex", "fund", "hk"])
def test_unrelated_failure_does_not_skip_us_but_blocks_combined_publication(monkeypatch, failed):
    module = _load_scheduler(monkeypatch)
    calls = []
    monkeypatch.setattr(module, "refresh_investment_return_snapshots", lambda _: pytest.fail("incomplete publication"))
    def step(name):
        calls.append(name)
        return {"status": "blocked"} if name == failed else {"status": "ok", "found": 1, "updated": 1}
    result = module._refresh_window(date(2026, 10, 26), [
        (name, lambda name=name: step(name)) for name in ("flex", "fund", "hk", "us")
    ], ("total",))
    assert calls == ["flex", "fund", "hk", "us"]
    assert result["status"] == "blocked"
    assert result["steps"][-1] == {"name": "us", "issues": []}


def test_invalid_flex_reference_still_allows_independent_quotes(monkeypatch):
    module = _load_scheduler(monkeypatch)
    calls = []
    result = module._refresh_window(date(2026, 10, 26), [
        ("flex", lambda: {"expected_statement_as_of": "invalid"}),
        ("us", lambda: calls.append("us") or {"found": 1, "updated": 1}),
    ], ("total",))
    assert calls == ["us"]
    assert result["steps"][0]["issues"] == ["invalid_expected_date"]
    assert result["status"] == "blocked"


def test_provider_exception_does_not_skip_independent_market(monkeypatch):
    module = _load_scheduler(monkeypatch)
    def fail():
        raise ValueError("token=secret")
    result = module._refresh_window(date(2026, 10, 26), [
        ("fund", fail), ("us", lambda: {"found": 1, "updated": 1}),
    ], ("total",))
    assert result["steps"] == [{"name": "fund", "issues": ["ValueError"]}, {"name": "us", "issues": []}]
    assert "secret" not in json.dumps(result)


def test_quote_poll_survives_restart_and_preserves_other_jobs(monkeypatch, tmp_path):
    module = _load_scheduler(monkeypatch)
    path = tmp_path / "state.json"
    official = {"status": "waiting", "target_date": "2026-10-26", "recent_runs": []}
    path.write_text(json.dumps({"version": 1, "jobs": {"ibkr_flex": official, "asia": official}}))
    monkeypatch.setattr(module, "STATE_PATH", path)
    calls = []
    def quote(endpoint, label):
        calls.append(endpoint)
        return {"as_of": "2026-10-26", "found": 3, "updated": 3, "quote_sources": {"databento_estimate": 3}}
    monkeypatch.setattr(module, "_call", quote)
    monkeypatch.setattr(module, "refresh_investment_return_snapshots", lambda _: pytest.fail("no all-market publication"))
    assert module.refresh_us_quotes(NOW)["status"] == "success"
    assert module.refresh_us_quotes(NOW + timedelta(minutes=4))["reason"] == "quote_poll_not_due"
    assert module.refresh_us_quotes(NOW + timedelta(minutes=5))["status"] == "success"
    assert calls == ["prices/refresh/us-stock?market=us&scheduled=true"] * 2
    jobs = json.loads(path.read_text())["jobs"]
    assert jobs["ibkr_flex"] == jobs["asia"] == official
    assert jobs["us_quotes"]["last_success_date"] == "2026-10-26"


@pytest.mark.parametrize("response", [None, {}, {"found": 2, "updated": 1, "as_of": "2026-10-26"},
    {"found": 1, "updated": 1}, {"found": 1, "updated": 0, "skipped_estimates": [{"symbol": "QQQ"}]}])
def test_incomplete_quote_coverage_cannot_advance_success(monkeypatch, tmp_path, response):
    module = _load_scheduler(monkeypatch)
    monkeypatch.setattr(module, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(module, "_call", lambda *_: response)
    result = module.refresh_us_quotes(NOW)
    assert result["status"] == "blocked"
    assert not result.get("last_success_date")


def test_quote_exception_does_not_store_credentials(monkeypatch, tmp_path):
    module = _load_scheduler(monkeypatch)
    path = tmp_path / "state.json"
    monkeypatch.setattr(module, "STATE_PATH", path)
    def failure(*_):
        raise ValueError("token=secret")
    monkeypatch.setattr(module, "_call", failure)
    assert module.refresh_us_quotes(NOW)["status"] == "error"
    assert "secret" not in path.read_text()


def test_corrupt_checkpoint_prevents_quote_write(monkeypatch, tmp_path):
    module = _load_scheduler(monkeypatch)
    path = tmp_path / "state.json"
    path.write_text("invalid")
    monkeypatch.setattr(module, "STATE_PATH", path)
    monkeypatch.setattr(module, "_call", lambda *_: pytest.fail("no quote write"))
    assert module.refresh_us_quotes(NOW)["reason"] == "checkpoint_unavailable"


@pytest.mark.parametrize("stamp,target,expected", [
    ("2026-10-26T15:00:00-04:00", "2026-10-26", "estimate"),
    ("2026-10-26T16:30:00-04:00", "2026-10-26", "close"),
    ("2026-10-26T20:00:00-04:00", "2026-10-27", "estimate"),
    ("2026-12-01T16:30:00-05:00", "2026-12-01", "close"),
    ("2026-10-26T19:00:00-04:00", "2026-10-27", None),
    ("2026-10-24T12:00:00-04:00", "2026-10-24", None),
    ("2026-12-25T12:00:00-05:00", "2026-12-25", None),
])
def test_scheduled_quote_uses_exchange_calendar_and_existing_label(monkeypatch, stamp, target, expected):
    from endpoints import prices
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat(stamp).astimezone(tz)
    monkeypatch.setattr(prices, "datetime", Clock)
    monkeypatch.setattr(prices, "_price_refresh_as_of", lambda *_: date.fromisoformat(target))
    calls = []
    monkeypatch.setattr(prices, "_refresh_category", lambda *a, **k: calls.append(k) or {"found": 0, "updated": 0})
    payload = json.loads(prices.refresh_us_stock(market="us", price_basis="close", scheduled=True).body)
    if expected is None:
        assert not calls
        assert payload["reason"] == "outside_us_quote_session"
    else:
        assert calls[0]["price_basis"] == expected
        assert calls[0]["require_current_close"] is True


def test_scheduled_close_never_stamps_prior_close_as_today(monkeypatch):
    from endpoints import prices
    holdings = [{"id": 1, "symbol": "QQQ", "name": "QQQ", "quantity": 2, "as_of": date(2026, 10, 23)}]
    monkeypatch.setattr(prices, "_get_holdings_with_symbol", lambda *a, **k: holdings)
    monkeypatch.setattr(prices, "_price_refresh_as_of", lambda *_: date(2026, 10, 26))
    monkeypatch.setattr(prices, "_upsert_yfinance_closes", lambda *_: 1)
    stored = {"QQQ": 100}
    monkeypatch.setattr(prices, "_latest_stored_yfinance_prices", lambda *a, **k: dict(stored))
    monkeypatch.setattr(prices, "_YFINANCE_CLOSE_CACHE", {"QQQ": [(date(2026, 10, 23), 100)]})
    saved = []
    monkeypatch.setattr(prices, "_upsert_price_snapshots", lambda *a: pytest.fail("quote job must not change positions"))
    monkeypatch.setattr(prices, "_upsert_scheduled_us_quotes", lambda rows, marks, day, basis: saved.append(dict(marks)) or len(marks))
    result = prices._refresh_category("美股", lambda _: {"QQQ": 100}, require_current_close=True)
    assert saved == [{}]
    assert result["updated"] == 0 and result["errors"]
    prices._YFINANCE_CLOSE_CACHE["QQQ"].append((date(2026, 10, 26), 101.000012207))
    stored["QQQ"] = 101
    result = prices._refresh_category("美股", lambda _: {"QQQ": 101}, require_current_close=True)
    assert saved[-1] == {"QQQ": 101}
    assert result["updated"] == result["daily_quote_rows"] == 1
