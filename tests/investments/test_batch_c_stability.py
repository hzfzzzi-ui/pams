"""Regression tests for Batch C backend stability, quotes, scheduler, and serialization fixes."""
from __future__ import annotations

import inspect
import sys
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[2]
API_DIR = ROOT / "backend" / "api"
SCHEDULER_DIR = ROOT / "backend" / "scheduler"
for path in (str(API_DIR), str(SCHEDULER_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


def test_refresh_fund_nav_history_imports_source_enabled(monkeypatch):
    from endpoints import prices
    from domain.imports import isolation

    class _FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _FakeConn:
        def set_session(self, readonly=False):
            self.readonly = readonly

        def cursor(self, cursor_factory=None):
            return _FakeCursor()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(isolation, "source_mode", lambda: "production")
    monkeypatch.setattr(isolation, "source_enabled", lambda cur: False)
    monkeypatch.setattr(prices, "_get_conn", lambda: _FakeConn())

    with pytest.raises(HTTPException) as exc_info:
        prices.refresh_fund_nav_history(start=date.today(), preview=True)
    assert exc_info.value.status_code == 409
    assert "unavailable" in str(exc_info.value.detail).lower()


def test_portfolio_state_asset_type_classifies_a_share_fund():
    from domain.investments.valuation.portfolio_state import _asset_type

    assert _asset_type({"major_category": "A股", "minor_category": "A股基金", "asset_liability": "资产"}) == "a_share_fund"
    assert _asset_type({"major_category": "A股", "minor_category": "A股", "asset_liability": "资产"}) == "a_share_stock"
    assert _asset_type({"major_category": "美股", "minor_category": "科技", "asset_liability": "资产"}) == "us_stock"
    assert _asset_type({"major_category": "贵金属", "minor_category": "黄金", "asset_liability": "资产"}) == "precious_metal"
    assert _asset_type({"major_category": "现金", "minor_category": "活期", "asset_liability": "负债"}) == "liability"


def test_portfolio_state_uses_position_prices_and_ths_rollforward(monkeypatch):
    from domain.investments.valuation import portfolio_state

    captured = {"position_prices": None, "rollforward_called": False}

    monkeypatch.setattr(
        portfolio_state,
        "snapshot_tables",
        lambda cur: {"table_name": "pams_holdings_snapshot", "quote_available": True},
    )
    monkeypatch.setattr(portfolio_state, "_table_exists", lambda cur, name: name == "source_records")
    monkeypatch.setattr(portfolio_state, "_latest_fx_rates", lambda cur, base: {"CNY": (Decimal("1"), date(2026, 11, 26))})

    real_cte = portfolio_state.quote_valuation_cte

    def _spy_cte(enabled, *, position_prices=False):
        captured["position_prices"] = position_prices
        return real_cte(enabled, position_prices=position_prices)

    monkeypatch.setattr(portfolio_state, "quote_valuation_cte", _spy_cte)
    monkeypatch.setattr(portfolio_state, "annotate_snapshot_coverage", lambda cur, rows: rows)
    monkeypatch.setattr(portfolio_state, "_cash_like_carry_forward_rows", lambda *a, **kw: [])

    def _spy_rollforward(cur, rows, *, view_as_of, account_code="tonghuashun"):
        captured["rollforward_called"] = True
        out = []
        for r in rows:
            copy = dict(r)
            if copy.get("account_code") == "tonghuashun" and copy.get("symbol") == "601899":
                copy["quantity"] = Decimal("3800")
                copy["market_value"] = Decimal("146984.00")
                copy["cost"] = Decimal("120000.00")
                copy["holding_pnl"] = Decimal("26984.00")
            out.append(copy)
        return out

    monkeypatch.setattr(portfolio_state, "apply_ths_post_snapshot_trades", _spy_rollforward)

    class _FakeCur:
        def execute(self, sql, params=None):
            self.last_sql = sql

        def fetchall(self):
            return [
                {
                    "id": 1,
                    "account_code": "tonghuashun",
                    "symbol": "601899",
                    "name": "紫金矿业",
                    "asset_liability": "资产",
                    "major_category": "A股",
                    "minor_category": "A股",
                    "currency": "CNY",
                    "quantity": Decimal("3000"),
                    "market_value": Decimal("116040.00"),
                    "cost": Decimal("90000.00"),
                    "holding_pnl": Decimal("26040.00"),
                    "as_of": date(2026, 11, 10),
                    "asset_id": "ASSET_601899",
                }
            ]

    cfg = {
        "defaults": {"include_in_nav": True, "owner_ratio": 1},
        "accounts": {"tonghuashun": {"account_type": "brokerage", "is_investment": True, "owner_ratio": 0.5}},
    }
    positions = portfolio_state.load_investment_positions(_FakeCur(), "CNY", cfg)
    assert captured["position_prices"] is True
    assert captured["rollforward_called"] is True
    assert len(positions) == 1
    assert positions[0]["quantity"] == Decimal("3800")
    assert positions[0]["market_value_cny"] == Decimal("73492.000")


def test_import_inbox_scan_and_pull_use_savepoints(monkeypatch, tmp_path):
    from endpoints import import_inbox

    bad = tmp_path / "bad.csv"
    good = tmp_path / "good.csv"
    bad.write_text("bad", encoding="utf-8")
    good.write_text("good", encoding="utf-8")

    sql_log: list[str] = []

    class _FakeCur:
        def execute(self, sql, params=None):
            sql_log.append(sql)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _FakeConn:
        def __init__(self):
            self.committed = False

        def cursor(self, cursor_factory=None):
            return _FakeCur()

        def commit(self):
            self.committed = True

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    conn = _FakeConn()

    @contextmanager
    def _noop_scope():
        yield

    monkeypatch.setattr(import_inbox.access, "same_origin", lambda req: None)
    monkeypatch.setattr(import_inbox.isolation, "account_import_scope", _noop_scope)
    monkeypatch.setattr(import_inbox.store, "get_conn", lambda: conn)
    monkeypatch.setattr(import_inbox, "_inbox_dir", lambda: tmp_path)
    monkeypatch.setattr(import_inbox, "_known_source_hashes", lambda cur: set())
    monkeypatch.setattr(
        import_inbox,
        "scan_inbox_directory",
        lambda path, known_hashes: {
            "new_items": [
                {"filename": "bad.csv", "rel_path": "bad.csv"},
                {"filename": "good.csv", "rel_path": "good.csv"},
            ],
            "duplicate_count": 0,
            "unrecognized": [],
        },
    )
    monkeypatch.setattr(import_inbox, "route_file_to_accounts", lambda filename, rel="": {"account_code": "tonghuashun"})

    def _stage(cur, *, content, filename, decision):
        if filename == "bad.csv":
            raise RuntimeError("db error during stage")
        return [{"filename": filename, "status": "draft"}]

    monkeypatch.setattr(import_inbox, "_stage_bytes_for_decision", _stage)

    result = import_inbox.inbox_scan_local(SimpleNamespace())
    assert conn.committed is True
    assert result["staged_count"] == 1
    assert len(result["errors"]) == 1
    assert "SAVEPOINT inbox_stage_local_one" in sql_log
    assert "ROLLBACK TO SAVEPOINT inbox_stage_local_one" in sql_log
    assert "RELEASE SAVEPOINT inbox_stage_local_one" in sql_log

    # Same check for pull-email
    sql_log.clear()
    conn2 = _FakeConn()
    monkeypatch.setattr(import_inbox.store, "get_conn", lambda: conn2)
    monkeypatch.setattr(import_inbox, "imap_status", lambda: {"configured": True})
    monkeypatch.setattr(
        import_inbox,
        "fetch_gmail_bill_artifacts",
        lambda known_hashes: {
            "folder": "INBOX",
            "fetched_messages": 2,
            "skipped_duplicates": 0,
            "artifacts": [
                {"filename": "bad.csv", "content": b"bad"},
                {"filename": "good.csv", "content": b"good"},
            ],
        },
    )
    email_result = import_inbox.inbox_pull_email(SimpleNamespace())
    assert conn2.committed is True
    assert email_result["staged_count"] == 1
    assert len(email_result["errors"]) == 1
    assert "SAVEPOINT inbox_stage_email_one" in sql_log
    assert "ROLLBACK TO SAVEPOINT inbox_stage_email_one" in sql_log
    assert "RELEASE SAVEPOINT inbox_stage_email_one" in sql_log


def test_scheduler_imap_poll_sends_origin_and_registers_projection_early(monkeypatch):
    import importlib.util
    import types

    if importlib.util.find_spec("schedule") is None:
        monkeypatch.setitem(sys.modules, "schedule", types.ModuleType("schedule"))
    import scheduler

    captured = {}

    def _fake_request_json(method, url, label, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = kwargs.get("headers")
        return {"ok": True}

    monkeypatch.setattr(scheduler, "_request_json", _fake_request_json)
    res = scheduler.run_daily_imap_bill_poll()
    assert res == {"ok": True}
    assert captured["method"] == "post"
    assert captured["url"].endswith("/import-inbox/pull-email")
    assert captured["headers"] == {"Origin": scheduler._api_origin()}

    src = inspect.getsource(scheduler.main)
    idx_proj_sched = src.index("schedule.every(30).seconds.do(refresh_investment_page_projection)")
    idx_startup_sleep = src.index("time.sleep(10)")
    idx_startup_us = src.index("refresh_us_quotes()")
    assert idx_proj_sched < idx_startup_sleep < idx_startup_us


def test_price_refresh_persists_crypto_and_precious_metal_quotes(monkeypatch):
    from domain.market import price_refresh

    persisted: list[tuple[str, str, str]] = []

    monkeypatch.setattr(price_refresh, "_upsert_gold_spot_closes", lambda rows: 1)
    monkeypatch.setattr(price_refresh, "_fetch_sge_au9999_history", lambda d: [])
    monkeypatch.setattr(price_refresh, "_price_refresh_as_of", lambda cat, basis: date(2026, 11, 26))

    def _fake_upsert(holdings, prices, as_of, *, source, market, currency="CNY"):
        persisted.append((source, market, currency))
        return len([h for h in holdings if h["symbol"] in prices])

    monkeypatch.setattr(price_refresh, "_upsert_daily_return_quote_snapshots", _fake_upsert)

    monkeypatch.setattr(
        price_refresh,
        "_get_holdings_with_symbol",
        lambda category, **kw: [
            {"id": 1, "symbol": "BTC", "name": "Bitcoin", "quantity": 0.5, "as_of": date(2026, 11, 25)}
        ],
    )
    crypto_res = price_refresh._refresh_category("加密", lambda syms: {"BTC": 95000.0})
    assert crypto_res["updated"] == 1
    assert crypto_res["daily_quote_rows"] == 1

    monkeypatch.setattr(
        price_refresh,
        "_get_holdings_with_symbol",
        lambda category, **kw: [
            {"id": 2, "symbol": "000217", "name": "华安黄金ETF联接C", "quantity": 1000, "as_of": date(2026, 11, 25)}
        ],
    )
    metal_res = price_refresh._refresh_category("贵金属", lambda syms: {"000217": 2.45})
    assert metal_res["updated"] == 1
    assert metal_res["daily_quote_rows"] == 1
    assert persisted == [
        ("crypto_quote", "CRYPTO", "USD"),
        ("precious_metal_fund_nav", "CN_FUND", "CNY"),
    ]


def test_freshness_guard_filters_unverified_trades_and_handles_trade_type(monkeypatch):
    from domain.market import freshness_guard
    from domain.investments.trades import trade_read_v2

    monkeypatch.setattr(
        trade_read_v2,
        "load_trade_rows",
        lambda cur, **kw: [
            {
                "symbol": "601899",
                "asset_name": "紫金矿业",
                "side": None,
                "trade_type": "buy",
                "quantity": Decimal("500"),
                "trade_date": date(2026, 11, 19),
                "source_evidence_status": "verified",
            },
            {
                "symbol": "601899",
                "asset_name": "紫金矿业",
                "side": "buy",
                "trade_type": "buy",
                "quantity": Decimal("9999"),
                "trade_date": date(2026, 11, 20),
                "source_evidence_status": "unverified",
            },
        ],
    )

    class _FakeCur:
        def __init__(self):
            self._result = None

        def execute(self, sql, params=None):
            if "to_regclass('public.investment_page_projection')" in sql:
                self._result = {"available": False}
            elif "market_observations" in sql:
                self._result = {"max_as_of": date(2026, 11, 26)}
            elif "to_regclass('public.source_records')" in sql:
                self._result = {"redesigned": True}
            elif "WITH latest AS" in sql:
                self._result = [
                    {"symbol": "601899", "name": "紫金矿业", "quantity": Decimal("1000"), "as_of": date(2026, 11, 10)}
                ]
            else:
                self._result = {"max_as_of": date(2026, 11, 26)}

        def fetchone(self):
            return self._result if isinstance(self._result, dict) else None

        def fetchall(self):
            return self._result if isinstance(self._result, list) else []

    payload = freshness_guard.build_holdings_freshness_and_projection(_FakeCur(), today=date(2026, 11, 27))
    adjustments = payload["ths_projected_adjustments"]
    assert len(adjustments) == 1
    assert adjustments[0]["delta_qty"] == 500.0
    assert adjustments[0]["projected_qty"] == 1500.0


def test_page_projection_stamps_and_checks_schema_version():
    from domain.investments.valuation import page_projection

    written = {}

    class _FakeReadCur:
        def execute(self, sql, params=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _FakeReader:
        def set_session(self, **kw):
            pass

        def cursor(self, cursor_factory=None):
            return _FakeReadCur()

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _FakeWriteCur:
        def __init__(self):
            self._last = None

        def execute(self, sql, params=None):
            if "to_regclass" in sql:
                self._last = {"available": True}
            elif "pg_try_advisory_xact_lock" in sql:
                self._last = {"locked": True}
            elif "SELECT published_revision" in sql:
                assert "_schema_version" in sql
                assert params[-1] == page_projection.PROJECTION_SCHEMA_VERSION
                self._last = {"published_revision": 4, "dirty": False, "recent": False}
            elif "pg_export_snapshot" in sql:
                self._last = {"snapshot": "snap-1"}
            elif "UPDATE public.investment_page_projection" in sql:
                written["payload"] = params[0].adapted
                written["revision"] = params[1]

        def fetchone(self):
            return self._last

    result = page_projection.refresh(
        _FakeWriteCur(),
        business_date=date(2026, 11, 27),
        build=lambda cur: {"holdings": {"ok": True}},
        connect=lambda: _FakeReader(),
    )
    assert result == {"status": "published", "revision": 5}
    assert written["payload"]["_schema_version"] == page_projection.PROJECTION_SCHEMA_VERSION
    assert written["payload"]["holdings"] == {"ok": True}


def test_monthly_fact_book_does_not_serialize_none_as_string():
    from domain.accounts.monthly import monthly_fact_book

    row = {
        "date": date(2026, 1, 31),
        "_account_contributions": {},
        "_comparison_basis": {},
        "latest_known_components": {},
    }
    values = {"tonghuashun": {"rows": {}, "series": []}}
    entries = {
        "tonghuashun": {
            "method": "opening_and_cash_movements",
            "opening_date": date(2026, 1, 1),
            "native": Decimal("1000"),
            "personal": None,
            "currency": "CNY",
            "owner_ratio": None,
            "rate": None,
            "fx_date": None,
            "price_dates": {},
            "status": "unpriced",
            "personal_assets": None,
            "personal_debt": None,
            "native_debt": None,
        }
    }
    monthly_fact_book.apply_month(row, values, entries, [])
    comp = row["latest_known_components"]["tonghuashun"]
    assert comp["owner_ratio"] is None
    assert comp["fx_rate"] is None


def test_batch_c_modules_avoid_naive_utc_date_today():
    paths = [
        API_DIR / "domain" / "market" / "freshness_guard.py",
        API_DIR / "domain" / "ledger" / "cashflow" / "ledger_view.py",
        API_DIR / "domain" / "accounts" / "balances" / "account_balance.py",
        API_DIR / "domain" / "accounts" / "views" / "accounts_view.py",
        API_DIR / "domain" / "investments" / "attribution" / "return_attribution.py",
        API_DIR / "domain" / "investments" / "valuation" / "portfolio_equity_bridge.py",
    ]
    for path in paths:
        content = path.read_text(encoding="utf-8")
        assert "date.today()" not in content, f"{path.name} still uses date.today()"
        assert "Date.today()" not in content, f"{path.name} still uses Date.today()"
        assert "Asia/Shanghai" in content, f"{path.name} missing Asia/Shanghai timezone"


def test_account_contribution_ratios_falls_back_to_verified_trade_facts(monkeypatch):
    from domain.investments.valuation import capital_ownership
    from domain.investments.trades import nav_trade_source_v2, trade_read_v2

    d1, d2 = date(2026, 1, 1), date(2026, 10, 19)
    cfg = {"funding_history": {"as_of": "2026-11-21", "complete_personal_flows": True}}

    # Simulate _read_event_prefix stopping early at a missing-balance-column export
    monkeypatch.setattr(nav_trade_source_v2, "_read_event_prefix", lambda cur, code: ([], date(2026, 9, 21)))
    verified_facts = [
        {"trade_date": d1, "trade_type": "deposit", "currency": "CNY", "net_cash": Decimal("100"), "source_evidence_status": "verified"},
        {"trade_date": d2, "trade_type": "deposit", "currency": "CNY", "net_cash": Decimal("50"), "source_evidence_status": "verified"},
    ]
    monkeypatch.setattr(trade_read_v2, "load_trade_rows", lambda cur, **kw: verified_facts)

    captured = {}

    def _fake_load(cur, c, events, dates, *, unverified_from=None):
        if unverified_from is not None:
            return {}
        captured["events"] = events
        return {d1: Decimal("0.5"), d2: Decimal("0.6667")}

    monkeypatch.setattr(capital_ownership, "load_contribution_ratios", _fake_load)

    class _FakeCur:
        def execute(self, sql, params=None):
            pass

        def fetchone(self):
            return {"ready": True}

    res = capital_ownership.account_contribution_ratios(_FakeCur(), "tonghuashun", cfg, [d1, d2])
    assert res == {d1: Decimal("0.5"), d2: Decimal("0.6667")}
    assert len(captured["events"]) == 2

    # Fail-closed when any trade fact is unverified
    unverified_facts = [
        *verified_facts,
        {"trade_date": d2, "trade_type": "buy", "currency": "CNY", "net_cash": Decimal("-10"), "source_evidence_status": "unverified"},
    ]
    monkeypatch.setattr(trade_read_v2, "load_trade_rows", lambda cur, **kw: unverified_facts)
    assert capital_ownership.account_contribution_ratios(_FakeCur(), "tonghuashun", cfg, [d1, d2]) == {}
