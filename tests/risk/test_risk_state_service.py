from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from domain.risk.risk_state import (  # noqa: E402
    _build_risk_actions,
    _build_user_risk_metrics,
    _concentration_from_exposure_payload,
    _concentration_rules_payload,
    _split_style_exposure_groups,
    build_risk_latest_payload,
)
import pandas as pd


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


def test_risk_does_not_aggregate_foreign_holding_without_fx(monkeypatch):
    from domain.risk import risk_state
    state = {
        "account_balances": [{"account_code": "broker", "account_type": "brokerage", "include_in_nav": True,
                              "amount": "100", "amount_cny": None}],
        "investment_positions": [{"account_code": "broker", "symbol": "ABC", "asset_name": "ABC",
                                  "currency": "USD", "market_value": "100", "market_value_cny": None,
                                  "cost_cny": None, "fx_status": "unavailable"}],
        "fx_status": "incomplete", "fx_unavailable_accounts": ["broker"], "fx_rates": [],
    }
    monkeypatch.setattr(risk_state, "build_portfolio_state", lambda **kwargs: state)
    result = risk_state.build_risk_state()
    assert result['reason'] == 'missing_fx' and result['metrics']['total_nav'] is None
    assert result['holdings'][0]['market_value'] == 100
    assert result['holdings'][0]['market_value_cny'] is None


def test_account_risk_amount_does_not_invent_fx_or_missing_cny(monkeypatch):
    from domain.risk import risk_state
    account = {"account_code": "wallet", "account_type": "funding", "currency": "USD", "include_in_nav": True,
               "amount": "100", "amount_cny": None}
    row = risk_state._account_balance_position(account, {})
    assert row["market_value"] == 100 and row["market_value_cny"] is None and row["fx_rate_to_cny"] is None
    zero = risk_state._account_balance_position({**account, "amount": 0}, {})
    assert zero["market_value_cny"] == 0 and zero["fx_rate_to_cny"] is None
    known = risk_state._account_balance_position({**account, "amount_cny": 700}, {})
    assert known["market_value_cny"] == 700 and known["fx_rate_to_cny"] == 7
    monkeypatch.setattr(risk_state, "build_portfolio_state", lambda **kwargs: {
        "account_balances": [account], "investment_positions": [], "fx_rates": [], "fx_status": "ok"})
    result = risk_state.build_risk_state()
    assert result["reason"] == "missing_fx" and result["metrics"]["total_nav"] is None
    assert result["holdings"][0]["market_value_cny"] is None


def test_risk_report_marks_unexplained_published_nav_without_creating_cash(monkeypatch):
    from domain.risk import risk_state
    state = {
        "account_balances": [{"account_code": "broker", "display_name": "Broker", "account_type": "brokerage",
                              "include_in_nav": True, "amount": "100", "amount_cny": "100", "currency": "CNY"}],
        "investment_positions": [{"account_code": "broker", "symbol": "TEST", "asset_name": "Test",
                                  "currency": "CNY", "market_value": "100", "market_value_cny": "100",
                                  "cost": "90", "cost_cny": "90", "as_of": "2026-10-22"}],
        "fx_status": "complete", "snapshot_scope_status": "complete", "fx_rates": [],
    }
    monkeypatch.setattr(risk_state, "build_portfolio_state", lambda **kwargs: state)
    monkeypatch.setattr(risk_state, "_latest_published_valuation_state", lambda *args: {
        "valuation_date": date(2026, 10, 22), "status": "published", "data_completeness": "complete",
        "net_liquidation_value": 150, "assets_cny": 150, "liabilities_cny": 0})
    monkeypatch.setattr(risk_state, "_load_db_industry_lookup", lambda positions: {})
    monkeypatch.setattr(risk_state, "_load_latest_exposure_payload", lambda *args: None)
    monkeypatch.setattr(risk_state, "_latest_ibkr_performance", lambda: None)
    result = risk_state.build_risk_state()
    assert result["status"] == "review" and result["metrics"]["total_nav"] == 150
    assert result["valuation_state"]["reconciliation_status"] == "unexplained"
    assert result["valuation_state"]["nav_reconciliation_cny"] == 50
    assert result["metrics"]["gross_exposure_ratio"] == 100 / 150
    assert all(row.get("asset_id") != "VALUATION_STATE_ADJUSTMENT" for row in result["positions"])
    assert result["data_quality"]["status"] == "NEEDS_REVIEW"


def test_build_risk_latest_payload_handles_empty_snapshot():
    payload = build_risk_latest_payload(FakeCursor([{"as_of": None}]), limit=10)

    assert payload == {"as_of": None, "events": [], "rows": []}


def test_build_risk_latest_payload_preserves_legacy_shape_and_query_contract():
    cur = FakeCursor(
        [
            {"as_of": date(2026, 5, 31)},
            [
                {
                    "as_of": date(2026, 5, 31),
                    "severity": "crit",
                    "code": "CONCENTRATION",
                    "title": "集中度过高",
                    "details": {"weight": 0.5},
                    "created_at": datetime(2026, 5, 31, 12, 0, 0),
                }
            ],
        ]
    )

    payload = build_risk_latest_payload(cur, limit=10)

    assert set(payload) == {"as_of", "events", "rows"}
    assert payload["as_of"] == "2026-05-31"
    assert payload["events"] == payload["rows"]
    assert payload["events"][0]["created_at"] == "2026-05-31T12:00:00"
    assert "CASE severity WHEN 'crit' THEN 3 WHEN 'warn' THEN 2 ELSE 1 END DESC" in cur.executed[1][0]
    assert cur.executed[1][1] == (date(2026, 5, 31), 10)


def test_split_style_exposure_groups_separates_exposure_dimensions():
    rows = [
        {"label": "科技成长", "kind": "style", "exposure_ratio": 0.30},
        {"label": "美股权益", "kind": "style", "exposure_ratio": 0.20},
        {"label": "美元资产", "kind": "style", "exposure_ratio": 0.20},
        {"label": "单一公司风险", "kind": "style", "exposure_ratio": 0.10},
    ]

    grouped = _split_style_exposure_groups(rows)

    assert [r["label"] for r in grouped["investment_style"]] == ["科技成长"]
    assert [r["label"] for r in grouped["region"]] == ["美股权益"]
    assert [r["label"] for r in grouped["currency"]] == ["美元资产"]
    assert [r["label"] for r in grouped["special"]] == ["单一公司风险"]
    assert grouped["region"][0]["kind"] == "region"


def test_concentration_rules_payload_exposes_split_risk_surfaces():
    payload = _concentration_rules_payload(
        {
            "limits": {
                "concentration": {
                    "style": {"yellow": 0.45, "red": 0.60},
                    "region": {"yellow": 0.75, "red": 0.90},
                    "currency": {"yellow": 0.70, "red": 0.85},
                    "special": {"yellow": 0.30, "red": 0.45},
                }
            }
        }
    )

    assert payload["style"]["label"] == "多标签风险暴露"
    assert [g["key"] for g in payload["style_groups"]] == ["investment_style", "region", "currency", "special"]
    assert payload["region"]["label"] == "区域暴露"
    assert payload["region"]["yellow"] == 0.75
    assert payload["currency"]["red"] == 0.85
    assert payload["special"]["red"] == 0.45


def test_exposure_engine_payload_builds_risk_concentration_surfaces():
    payload = {
        "status": "ok",
        "model_version": "exposure_engine_v1",
        "quality": {"uncovered_value_cny": 50.0},
        "validation": {"totals": {"industry": {"ok": True}}},
        "exposures": {
            "industry": [
                {
                    "name": "科技TMT",
                    "value_cny": 300.0,
                    "source_count": 1,
                    "source_details": [
                        {
                            "source_asset_name": "NVIDIA",
                            "source_symbol": "NVDA",
                            "contribution_to_bucket_cny": 300.0,
                            "weight": 1.0,
                        }
                    ],
                }
            ],
            "theme": [
                {
                    "name": "AI / 算力",
                    "value_cny": 250.0,
                    "source_details": [
                        {
                            "source_asset_name": "NVIDIA",
                            "source_symbol": "NVDA",
                            "contribution_to_bucket_cny": 250.0,
                            "weight": 1.0,
                        }
                    ],
                }
            ],
            "region": [{"name": "美国", "value_cny": 400.0, "source_details": []}],
            "currency": [{"name": "USD", "value_cny": 400.0, "source_details": []}],
            "asset_type": [
                {
                    "name": "非上市股权",
                    "value_cny": 120.0,
                    "source_details": [
                        {
                            "source_asset_name": "Huawei ESOP",
                            "source_symbol": "HUAWEI_ESOP",
                            "contribution_to_bucket_cny": 120.0,
                        }
                    ],
                }
            ],
        },
    }

    result = _concentration_from_exposure_payload(payload, nav=1000.0, limits={"limits": {"concentration": {}}})

    assert result is not None
    assert result["source"] == "exposure_engine"
    assert result["industry"][0]["label"] == "科技TMT"
    assert result["industry"][0]["exposure_ratio"] == 0.3
    assert result["industry"][0]["top_positions"][0]["asset_name"] == "NVIDIA"
    assert result["style_groups"]["investment_style"][0]["label"] == "AI / 算力"
    assert result["style_groups"]["region"][0]["label"] == "美国"
    assert result["style_groups"]["currency"][0]["label"] == "USD"
    assert {row["label"] for row in result["style_groups"]["special"]} == {"非上市股权", "未穿透基金"}
    assert result["validation"] == payload["validation"]


def test_build_risk_actions_groups_alerts_and_adds_contributors():
    positions = pd.DataFrame(
        [
            {
                "account_code": "ibkr",
                "account_name": "IBKR",
                "asset_id": "CASH_USD",
                "asset_name": "USD Cash",
                "asset_type": "cash",
                "market_value_cny": -100.0,
            },
            {
                "account_code": "ibkr",
                "account_name": "IBKR",
                "asset_id": "QQQ",
                "asset_name": "QQQ",
                "asset_type": "us_stock",
                "market_value_cny": 1000.0,
            },
        ]
    )
    alerts = [
        {"level": "red", "rule": "negative_cash_ratio", "metric": 0.1, "threshold": 0.03, "message": "Negative cash ratio is 10.00%"},
        {"level": "yellow", "rule": "factor_exposure:US_EQUITY", "metric": 0.5, "threshold": 0.3, "message": "Factor US_EQUITY exposure is 50.00%"},
        {"level": "green", "rule": "total_nav", "metric": 900.0, "threshold": 0, "message": "Total NAV is 900.00 CNY"},
    ]

    payload = _build_risk_actions(
        alerts,
        positions,
        900.0,
        {"asset_types": {"us_stock": {"US_EQUITY": 1.0}}},
        {},
        {},
    )

    assert payload["summary"] == {"must_handle": 1, "watch": 1, "info": 1}
    assert payload["must_handle"]["items"][0]["contributors"][0]["asset_name"] == "USD Cash"
    assert payload["watch"]["items"][0]["contributors"][0]["asset_name"] == "QQQ"


def test_build_user_risk_metrics_prioritizes_configured_exposures():
    limits = {
        "limits": {
            "gross_exposure": {"yellow": 0.95, "red": 1.10},
            "negative_cash_ratio": {"yellow": 0.01, "red": 0.03},
            "position_weight": {
                "default": {"yellow": 0.10, "red": 0.15},
                "by_asset_type": {"esop": {"yellow": 0.20, "red": 0.30}},
            },
            "factor_exposure": {
                "CN_EQUITY": {"yellow": 0.30, "red": 0.45},
                "US_EQUITY": {"yellow": 0.35, "red": 0.55},
                "ESOP_SINGLE_COMPANY": {"yellow": 0.20, "red": 0.30},
            },
            "stress_loss": {"yellow": 0.08, "red": 0.15},
            "concentration": {"industry": {"yellow": 0.30, "red": 0.40}},
        }
    }
    metrics = {"gross_exposure_ratio": 0.94, "negative_cash_ratio": 0.04}
    factor_rows = [
        {"factor": "CN_EQUITY", "exposure_ratio": 0.32, "abs_exposure_ratio": 0.32, "exposure_cny": 320.0},
        {"factor": "US_EQUITY", "exposure_ratio": 0.20, "abs_exposure_ratio": 0.20, "exposure_cny": 200.0},
        {"factor": "ESOP_SINGLE_COMPANY", "exposure_ratio": 0.29, "abs_exposure_ratio": 0.29, "exposure_cny": 290.0},
    ]
    position_rows = [
        {"asset_id": "HUAWEI_ESOP", "asset_name": "华为 ESOP", "asset_type": "esop", "position_weight": 0.29},
        {"asset_id": "601899", "asset_name": "紫金矿业", "asset_type": "a_share_stock", "position_weight": 0.12},
    ]
    industry_rows = [
        {
            "label": "科技TMT",
            "exposure_ratio": 0.31,
            "yellow_threshold": 0.30,
            "red_threshold": 0.40,
            "level": "yellow",
            "top_positions": [{"asset_name": "NVIDIA", "exposure_ratio": 0.08}],
        }
    ]
    stress_rows = [{"scenario": "equity_global_down_20", "label": "Global equity -20%", "loss_ratio": 0.16}]

    payload = _build_user_risk_metrics(
        limits=limits,
        metrics=metrics,
        factor_rows=factor_rows,
        position_rows=position_rows,
        industry_rows=industry_rows,
        stress_rows=stress_rows,
    )

    by_key = {row["key"]: row for row in payload}
    assert by_key["equity_exposure"]["level"] == "yellow"
    assert by_key["equity_exposure"]["components"][0]["label"] == "中国权益"
    assert by_key["negative_cash_ratio"]["level"] == "red"
    assert by_key["single_position"]["sources"][0]["asset_name"] == "华为 ESOP"
    assert by_key["industry_concentration"]["note"] == "当前最大行业：科技TMT。"
    assert by_key["stress_loss"]["level"] == "red"
