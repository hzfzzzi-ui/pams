from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

import domain.investments.portfolio_valuation_state as pvs  # noqa: E402
from domain.investments.portfolio_valuation_state import (  # noqa: E402
    build_portfolio_valuation_state_from_parts,
    latest_published_portfolio_valuation_state,
    publish_current_portfolio_valuation_state,
)


class FakeCursor:
    def __init__(self, results):
        self.results = list(results)
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self.results.pop(0)

    def fetchall(self):
        return self.results.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeConn:
    def __init__(self, cur):
        self.cur = cur

    def cursor(self, *args, **kwargs):
        return self.cur

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_build_portfolio_valuation_state_from_parts_uses_net_liquidation_formula():
    state = build_portfolio_valuation_state_from_parts(
        base_currency="CNY",
        cycle="asia_close",
        status="draft",
        account_balances=[
            {"account_code": "ibkr", "amount_cny": Decimal("180"), "include_in_nav": True, "as_of": date(2026, 6, 18)},
            {
                "account_code": "cash",
                "account_type": "funding",
                "amount_cny": Decimal("80"),
                "include_in_nav": True,
                "as_of": date(2026, 6, 17),
            },
        ],
        investment_positions=[
            {
                "account_code": "ibkr",
                "asset_type": "us_stock",
                "market_value_cny": Decimal("300"),
                "holding_pnl_cny": Decimal("20"),
                "as_of": date(2026, 6, 18),
            },
            {
                "account_code": "ibkr",
                "asset_type": "liability",
                "market_value_cny": Decimal("-120"),
                "holding_pnl_cny": Decimal("0"),
                "as_of": date(2026, 6, 18),
            },
        ],
    )

    assert state["valuation_date"] == "2026-06-18"
    assert state["assets_cny"] == Decimal("380")
    assert state["liabilities_cny"] == Decimal("120")
    assert state["net_liquidation_value"] == Decimal("260")
    assert state["gross_exposure_cny"] == Decimal("300")
    assert state["investment_value_cny"] == Decimal("300")
    assert state["cash_cny"] == Decimal("80")
    assert state["total_pnl_cny"] == Decimal("20")
    assert state["raw_json"]["formula"] == "net_liquidation_value = assets_cny - liabilities_cny"
    assert state["raw_json"]["account_values_cny"] == {"ibkr": Decimal("180"), "cash": Decimal("80")}
    assert state["source_hash"]


def test_build_portfolio_valuation_state_marks_missing_sources_partial():
    state = build_portfolio_valuation_state_from_parts(
        account_balances=[
            {
                "account_code": "cash",
                "amount_cny": Decimal("0"),
                "include_in_nav": True,
                "review_status": "missing_data",
            }
        ],
        investment_positions=[
            {
                "account_code": "broker",
                "asset_id": "QQQ",
                "asset_type": "us_stock",
                "market_value_cny": Decimal("100"),
                "holding_pnl_cny": Decimal("0"),
                "as_of": None,
            }
        ],
    )

    assert state["data_completeness"] == "partial"
    assert state["raw_json"]["missing_accounts"] == ["cash"]
    assert state["raw_json"]["missing_positions"] == ["QQQ"]


def test_latest_published_portfolio_valuation_state_reads_latest_published_row():
    cur = FakeCursor(
        [
            {"table_name": "valuation_results"},
            {"valuation_date": date(2026, 6, 18), "net_liquidation_value": Decimal("260")},
        ]
    )

    row = latest_published_portfolio_valuation_state(cur, base_currency="CNY")

    assert row["net_liquidation_value"] == Decimal("260")
    sql, params = cur.executed[-1]
    assert "status = 'published'" in sql
    assert "ORDER BY valuation_date DESC" in sql
    assert params == ("CNY",)


def test_publish_current_portfolio_valuation_state_requires_schema_table():
    cur = FakeCursor([{"table_name": None}])

    try:
        publish_current_portfolio_valuation_state(cur, base_currency="CNY")
    except RuntimeError as exc:
        assert "valuation_results is missing" in str(exc)
    else:
        raise AssertionError("Expected missing schema table to fail")


def test_portfolio_valuation_state_schema_is_idempotent_and_indexed():
    schema = (ROOT / "database" / "postgres" / "schema" / "025_valuation_results.sql").read_text(
        encoding="utf-8"
    )

    assert "CREATE TABLE IF NOT EXISTS public.valuation_results" in schema
    assert "ON public.valuation_results (valuation_date,cycle,base_currency)" in schema
    assert "idx_valuation_results_portfolio_latest" in schema


def test_build_portfolio_valuation_state_payload_ignores_stale_published_row(monkeypatch):
    current_state = {
        "valuation_date": "2026-06-21",
        "cycle": "manual_publish",
        "base_currency": "CNY",
        "status": "draft",
        "data_completeness": "complete",
        "net_liquidation_value": Decimal("120"),
        "assets_cny": Decimal("140"),
        "liabilities_cny": Decimal("20"),
        "cash_cny": Decimal("10"),
        "source_hash": "current-hash",
        "raw_json": {},
    }
    stale_published = {
        "valuation_date": date(2026, 6, 18),
        "cycle": "manual_publish",
        "base_currency": "CNY",
        "status": "published",
        "data_completeness": "complete",
        "net_liquidation_value": Decimal("100"),
        "assets_cny": Decimal("110"),
        "liabilities_cny": Decimal("10"),
        "cash_cny": Decimal("8"),
        "source_hash": "old-hash",
        "published_at": None,
    }

    monkeypatch.setattr(pvs, "_table_exists", lambda cur, table: True)
    monkeypatch.setattr(pvs, "_build_current_state", lambda cur, **kwargs: current_state)
    monkeypatch.setattr(pvs, "latest_published_portfolio_valuation_state", lambda cur, **kwargs: stale_published)

    payload = pvs.build_portfolio_valuation_state_payload(conn_factory=lambda: FakeConn(FakeCursor([])))

    assert payload["source"] == "computed_current"
    assert payload["net_liquidation_value"] == "120"
    assert payload["published_state"]["net_liquidation_value"] == "100"
    assert payload["published_state"]["overlay_applied"] is False
    assert payload["published_state"]["overlay_reason"] == "stale_or_changed_valuation_state"


def test_build_portfolio_valuation_state_payload_keeps_matching_published_row(monkeypatch):
    current_state = {
        "valuation_date": "2026-06-21",
        "cycle": "manual_publish",
        "base_currency": "CNY",
        "status": "draft",
        "data_completeness": "complete",
        "net_liquidation_value": Decimal("120"),
        "assets_cny": Decimal("140"),
        "liabilities_cny": Decimal("20"),
        "cash_cny": Decimal("10"),
        "source_hash": "same-hash",
        "raw_json": {},
    }
    published = {
        **current_state,
        "valuation_date": date(2026, 6, 21),
        "status": "published",
        "source_hash": "same-hash",
        "published_at": None,
    }

    monkeypatch.setattr(pvs, "_table_exists", lambda cur, table: True)
    monkeypatch.setattr(pvs, "_build_current_state", lambda cur, **kwargs: current_state)
    monkeypatch.setattr(pvs, "latest_published_portfolio_valuation_state", lambda cur, **kwargs: published)

    payload = pvs.build_portfolio_valuation_state_payload(conn_factory=lambda: FakeConn(FakeCursor([])))

    assert payload["source"] == "published"
    assert payload["net_liquidation_value"] == "120"
    assert "published_state" not in payload


def test_daily_portfolio_requires_all_accounts_same_period_and_applies_ownership_once(monkeypatch):
    from datetime import timedelta
    from domain.accounts import flow_coverage, official_flow_coverage

    class Cursor:
        def execute(self, *_args):
            pass
        def fetchone(self):
            return {'versions':True,'batches':True,'items':True,'files':True}

    end=date(2026,6,18); start=end-timedelta(days=1)
    accounts=[{'account_code':'a','amount_cny':Decimal(60),'include_in_nav':True,'as_of':end},
              {'account_code':'b','amount_cny':Decimal(80),'include_in_nav':True,'as_of':end}]
    state=build_portfolio_valuation_state_from_parts(account_balances=accounts,investment_positions=[])
    cfg={'accounts':{'a':{'owner_ratio':'0.5'},'b':{'owner_ratio':'1'}}}
    monkeypatch.setattr(official_flow_coverage,'latest_verified_ibkr_period',lambda *a,**kw:None)
    def proof(cur,*,account_code,end,ending_gross_cny):
        return {'start':start,'end':end,'opening_gross_cny':Decimal(100 if account_code=='a' else 90),
                'closing_gross_cny':ending_gross_cny,'external_flow_gross_cny':Decimal(0)}
    monkeypatch.setattr(flow_coverage,'latest_verified_period',proof)
    aggregate=pvs._verified_portfolio_daily_pnl(Cursor(),state=state,account_cfg=cfg,
        account_balances=accounts,investment_positions=[])
    assert aggregate[0]==0  # +10 from half-owned a, -10 from b.
    monkeypatch.setattr(flow_coverage,'latest_verified_period',lambda cur,**kw:
        None if kw['account_code']=='b' else proof(cur,**kw))
    assert pvs._verified_portfolio_daily_pnl(Cursor(),state=state,account_cfg=cfg,
        account_balances=accounts,investment_positions=[]) is None
    def shifted(cur,**kw):
        result=proof(cur,**kw)
        if kw['account_code']=='b': result['start']-=timedelta(days=1)
        return result
    monkeypatch.setattr(flow_coverage,'latest_verified_period',shifted)
    assert pvs._verified_portfolio_daily_pnl(Cursor(),state=state,account_cfg=cfg,
        account_balances=accounts,investment_positions=[]) is None


def test_legacy_published_zero_without_daily_proof_is_unknown():
    cur=FakeCursor([{'table_name':'valuation_results'},
                    {'valuation_date':date(2026,6,18),'daily_pnl_cny':Decimal(0),'raw_json':{}}])
    result=latest_published_portfolio_valuation_state(cur)
    assert result['daily_pnl_cny'] is None
