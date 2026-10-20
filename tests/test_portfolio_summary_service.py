from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from services.portfolio_summary import (  # noqa: E402
    apply_return_attribution_pnl_to_summary,
    build_holdings_by_account_payload,
    build_holdings_summary_payload,
    build_summary2_from_holdings_latest_payload,
    build_summary2_latest_payload,
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


def test_build_holdings_summary_payload_preserves_legacy_shape():
    cur = FakeCursor(
        [
            {"as_of": date(2026, 5, 31)},
            [
                {
                    "major_category": "A股",
                    "holdings_count": 2,
                    "total_value_base": Decimal("100.25"),
                    "total_cost_base": Decimal("90.00"),
                    "total_pnl_base": Decimal("10.25"),
                },
                {
                    "major_category": "美股",
                    "holdings_count": 1,
                    "total_value_base": Decimal("50.00"),
                    "total_cost_base": Decimal("45.00"),
                    "total_pnl_base": Decimal("5.00"),
                },
            ],
        ]
    )

    payload = build_holdings_summary_payload(
        cur,
        base_currency="CNY",
        view_as_of=date(2026, 6, 1),
        owner_ratio_sql="1",
        owner_ratio_params=[],
    )

    assert set(payload) == {"as_of", "base_currency", "total", "by_category"}
    assert payload["as_of"] == "2026-06-01"
    assert payload["total"] == {"value": "150.25", "cost": "135.0", "pnl": "15.25"}
    assert payload["by_category"][0]["total_value_base"] == "100.25"
    sql, params = cur.executed[1]
    assert "JOIN latest_account la" in sql
    assert "latest_position" not in sql
    assert params == (date(2026, 6, 1), "CNY")


def test_build_holdings_by_account_payload_preserves_legacy_shape():
    cur = FakeCursor(
        [
            {"as_of": date(2026, 5, 31)},
            [
                {
                    "account_code": "ibkr",
                    "display_name": "IBKR",
                    "account_currency": "USD",
                    "holdings_count": 3,
                    "total_value_base": Decimal("200.00"),
                }
            ],
        ]
    )

    payload = build_holdings_by_account_payload(
        cur,
        base_currency="CNY",
        view_as_of=date(2026, 6, 1),
        owner_ratio_sql="1",
        owner_ratio_params=[],
    )

    assert set(payload) == {"as_of", "base_currency", "by_account"}
    assert payload["as_of"] == "2026-06-01"
    assert payload["by_account"][0]["account_code"] == "ibkr"
    assert payload["by_account"][0]["total_value_base"] == "200.00"
    sql, params = cur.executed[1]
    assert "JOIN latest_account la" in sql
    assert "latest_position" not in sql
    assert params == (date(2026, 6, 1), "CNY")


def test_build_summary2_latest_payload_preserves_legacy_shape():
    cur = FakeCursor(
        [
            {"as_of": date(2026, 5, 31)},
            [
                {
                    "asset_class": "A股",
                    "market_value": Decimal("100.00"),
                    "holding_pnl": Decimal("-5.00"),
                    "daily_pnl": Decimal("0"),
                },
                {
                    "asset_class": "LIABILITY",
                    "market_value": Decimal("-20.00"),
                    "holding_pnl": Decimal("0"),
                    "daily_pnl": Decimal("0"),
                },
            ],
            [
                {
                    "asset_class": "A股",
                    "venue": "tonghuashun",
                    "account": "tonghuashun",
                    "type": "A股",
                    "symbol": "601899",
                    "name": "紫金矿业",
                    "currency": "CNY",
                    "market_value": Decimal("100.00"),
                    "holding_pnl": Decimal("-5.00"),
                    "daily_pnl": Decimal("0"),
                }
            ],
        ]
    )

    payload = build_summary2_latest_payload(
        cur,
        base_currency="CNY",
        view_as_of=date(2026, 6, 1),
        owner_ratio_sql="1",
        owner_ratio_params=[],
        excluded_accounts=[],
    )

    assert set(payload) == {"as_of", "base_currency", "totals", "by_asset_class", "children"}
    assert payload["as_of"] == "2026-06-01"
    assert payload["totals"] == {"market_value": "80.0", "holding_pnl": "-5.0", "daily_pnl": "0.0"}
    assert payload["children"][0]["symbol"] == "601899"


def test_build_summary2_from_holdings_latest_payload_nets_liabilities():
    payload = build_summary2_from_holdings_latest_payload(
        {
            "as_of": "2026-06-16",
            "rows": [
                {
                    "account": "ibkr",
                    "asset_liability": "资产",
                    "major_category": "美股",
                    "symbol": "QQQ",
                    "market_value_cny": "300.00",
                    "holding_pnl_cny": "10.00",
                    "daily_total_pnl_cny": "3.00",
                },
                {
                    "account": "ibkr",
                    "asset_liability": "负债",
                    "major_category": "借款",
                    "symbol": "CASH_USD",
                    "market_value_cny": "-120.00",
                    "holding_pnl_cny": "0",
                    "daily_total_pnl_cny": "0",
                },
            ],
        },
        base_currency="CNY",
        account_balances=[
            {
                "account_code": "ibkr",
                "display_name": "IBKR",
                "account_type": "brokerage",
                "currency": "CNY",
                "amount_cny": "180.00",
                "include_in_nav": True,
            },
            {
                "account_code": "ccb_7332",
                "display_name": "建行7332",
                "account_type": "funding",
                "currency": "CNY",
                "amount_cny": "80.00",
                "include_in_nav": True,
            },
        ],
    )

    assert payload["totals"]["market_value"] == "260.00"
    assert {row["asset_class"]: row["market_value"] for row in payload["by_asset_class"]} == {
        "美股": "300.00",
        "现金": "80.00",
        "LIABILITY": "-120.00",
    }


def test_build_summary2_from_holdings_latest_payload_adds_represented_account_residual():
    payload = build_summary2_from_holdings_latest_payload(
        {
            "as_of": "2026-06-17",
            "rows": [
                {
                    "account": "tonghuashun",
                    "asset_liability": "资产",
                    "major_category": "A股",
                    "symbol": "601899",
                    "market_value_cny": "240.00",
                    "holding_pnl_cny": "12.00",
                    "daily_total_pnl_cny": "2.00",
                },
            ],
        },
        base_currency="CNY",
        account_balances=[
            {
                "account_code": "tonghuashun",
                "display_name": "同花顺",
                "account_type": "brokerage",
                "currency": "CNY",
                "amount_cny": "246.18",
                "include_in_nav": True,
            },
        ],
    )

    assert payload["totals"]["market_value"] == "246.18"
    assert {row["asset_class"]: row["market_value"] for row in payload["by_asset_class"]} == {
        "A股": "240.00",
        "现金": "6.18",
    }


def test_apply_return_attribution_pnl_to_summary_splits_total_and_current_float():
    payload = {
        "as_of": "2026-06-18",
        "base_currency": "CNY",
        "totals": {"market_value": "300.00", "holding_pnl": "12.00", "daily_pnl": "3.00"},
        "by_asset_class": [],
        "children": [],
    }
    attribution = {
        "total_pnl_cny": "17.00",
        "dimensions": {
            "holding": [
                {"name": "Current", "market_value_cny": "300.00", "pnl_cny": "12.00"},
                {"name": "Closed", "market_value_cny": "0", "pnl_cny": "5.00"},
            ]
        },
    }

    out = apply_return_attribution_pnl_to_summary(payload, attribution)

    assert out["totals"]["holding_pnl"] == "17.00"
    assert out["totals"]["current_float_pnl"] == "12.00"
    assert out["totals"]["closed_realized_pnl"] == "5.00"


def test_apply_return_attribution_pnl_to_summary_prefers_canonical_total():
    payload = {
        "as_of": "2026-06-19",
        "base_currency": "CNY",
        "totals": {"market_value": "300.00", "holding_pnl": "12.00", "daily_pnl": "0"},
        "by_asset_class": [],
        "children": [],
    }
    attribution = {
        "total_pnl_cny": "17.00",
        "canonical_total_pnl_cny": "20.00",
        "dimensions": {
            "holding": [
                {"name": "Current", "market_value_cny": "300.00", "pnl_cny": "12.00"},
            ]
        },
    }

    out = apply_return_attribution_pnl_to_summary(payload, attribution)

    assert out["totals"]["holding_pnl"] == "20.00"
    assert out["totals"]["current_float_pnl"] == "12.00"
    assert out["totals"]["closed_realized_pnl"] == "8.00"
