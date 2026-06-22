from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from services.portfolio_summary import apply_published_valuation_state_to_summary  # noqa: E402
from services.portfolio_valuation_state import (  # noqa: E402
    apply_holdings_category_pnl_to_section_return_series_payload,
    apply_summary_pnl_to_return_series_payload,
    apply_published_valuation_state_to_return_series_payload,
)
from services.risk_state import _positions_reconciled_to_published_nav  # noqa: E402


def test_apply_published_valuation_state_to_summary_overrides_total_only():
    payload = {
        "as_of": "2026-06-17",
        "base_currency": "CNY",
        "totals": {"market_value": "100", "holding_pnl": "12", "daily_pnl": "3"},
        "by_asset_class": [{"asset_class": "A-share", "market_value": "100"}],
        "children": [],
    }

    out = apply_published_valuation_state_to_summary(
        payload,
        {
            "valuation_date": date(2026, 6, 18),
            "cycle": "manual_publish",
            "status": "published",
            "data_completeness": "complete",
            "net_liquidation_value": Decimal("260.00"),
            "assets_cny": Decimal("380.00"),
            "liabilities_cny": Decimal("120.00"),
            "cash_cny": Decimal("80.00"),
            "source_hash": "abc",
        },
    )

    assert out["as_of"] == "2026-06-18"
    assert out["totals"]["market_value"] == "260.00"
    assert out["totals"]["holding_pnl"] == "12"
    assert out["by_asset_class"] == payload["by_asset_class"]
    assert out["valuation_state"]["source"] == "published"
    assert out["valuation_state"]["net_liquidation_value"] == "260.00"
    assert out["valuation_state"]["overlay_applied"] is True


def test_apply_published_valuation_state_to_summary_skips_stale_state():
    payload = {
        "as_of": "2026-06-19",
        "data_as_of_max": "2026-06-19",
        "base_currency": "CNY",
        "totals": {"market_value": "300", "holding_pnl": "12", "daily_pnl": "3"},
        "by_asset_class": [{"asset_class": "A-share", "market_value": "300"}],
        "children": [],
    }

    out = apply_published_valuation_state_to_summary(
        payload,
        {
            "valuation_date": date(2026, 6, 18),
            "cycle": "manual_publish",
            "status": "published",
            "data_completeness": "complete",
            "net_liquidation_value": Decimal("260.00"),
            "assets_cny": Decimal("380.00"),
            "liabilities_cny": Decimal("120.00"),
            "cash_cny": Decimal("80.00"),
            "source_hash": "abc",
        },
    )

    assert out["as_of"] == "2026-06-19"
    assert out["totals"]["market_value"] == "300"
    assert out["valuation_state"]["overlay_applied"] is False
    assert out["valuation_state"]["overlay_reason"] == "stale_valuation_state"
    assert out["valuation_state"]["target_as_of"] == "2026-06-19"


def test_positions_reconciled_to_published_nav_adds_transparent_denominator_row():
    positions = pd.DataFrame(
        [
            {
                "account_code": "broker",
                "account_name": "Broker",
                "asset_id": "QQQ",
                "asset_name": "QQQ",
                "asset_type": "us_stock",
                "market_value_cny": 100.0,
            }
        ]
    )

    out, nav, meta = _positions_reconciled_to_published_nav(
        positions,
        {},
        {
            "valuation_date": date(2026, 6, 18),
            "cycle": "manual_publish",
            "status": "published",
            "data_completeness": "complete",
            "net_liquidation_value": 150.0,
            "assets_cny": 150.0,
            "liabilities_cny": 0.0,
        },
    )

    assert nav == 150.0
    assert meta["raw_positions_nav_cny"] == 100.0
    assert meta["nav_reconciliation_cny"] == 50.0
    adjustment = out.loc[out["asset_id"] == "VALUATION_STATE_ADJUSTMENT"].iloc[0]
    assert adjustment["asset_type"] == "cash"
    assert adjustment["market_value_cny"] == 50.0


def test_positions_reconciled_to_published_nav_skips_stale_state():
    positions = pd.DataFrame(
        [
            {
                "as_of": "2026-06-19",
                "account_code": "broker",
                "account_name": "Broker",
                "asset_id": "QQQ",
                "asset_name": "QQQ",
                "asset_type": "us_stock",
                "market_value_cny": 100.0,
            }
        ]
    )

    out, nav, meta = _positions_reconciled_to_published_nav(
        positions,
        {},
        {
            "valuation_date": date(2026, 6, 18),
            "cycle": "manual_publish",
            "status": "published",
            "data_completeness": "complete",
            "net_liquidation_value": 150.0,
            "assets_cny": 150.0,
            "liabilities_cny": 0.0,
        },
    )

    assert nav == 100.0
    assert "VALUATION_STATE_ADJUSTMENT" not in set(out["asset_id"])
    assert meta["overlay_applied"] is False
    assert meta["overlay_reason"] == "stale_valuation_state"
    assert meta["raw_positions_nav_cny"] == 100.0
    assert meta["nav_reconciliation_cny"] == 0.0


def test_apply_published_valuation_state_to_return_series_payload_overlays_terminal_value_only():
    payload = {
        "method": "holding_return_snapshot",
        "rows": [
            {"date": "2026-06-17", "market_value": "100", "total_pnl": "8"},
            {"date": "2026-06-18", "market_value": "110", "total_pnl": "9"},
        ],
    }

    out = apply_published_valuation_state_to_return_series_payload(
        payload,
        {
            "valuation_date": date(2026, 6, 18),
            "cycle": "manual_publish",
            "status": "published",
            "data_completeness": "complete",
            "net_liquidation_value": Decimal("260.00"),
            "assets_cny": Decimal("380.00"),
            "liabilities_cny": Decimal("120.00"),
        },
    )

    assert out["rows"][0]["market_value"] == "100"
    assert out["rows"][1]["market_value"] == "260.00"
    assert out["rows"][1]["net_liquidation_value"] == "260.00"
    assert out["rows"][1]["total_pnl"] == "9"
    assert out["valuation_state"]["overlay_applied"] is True


def test_apply_published_valuation_state_to_return_series_payload_skips_date_mismatch():
    payload = {
        "method": "holding_return_snapshot",
        "rows": [
            {"date": "2026-06-18", "market_value": "100", "total_pnl": "8"},
            {"date": "2026-06-19", "market_value": "110", "total_pnl": "9"},
        ],
    }

    out = apply_published_valuation_state_to_return_series_payload(
        payload,
        {
            "valuation_date": date(2026, 6, 18),
            "cycle": "manual_publish",
            "status": "published",
            "data_completeness": "complete",
            "net_liquidation_value": Decimal("260.00"),
            "assets_cny": Decimal("380.00"),
            "liabilities_cny": Decimal("120.00"),
        },
    )

    assert out["rows"][1]["market_value"] == "110"
    assert out["rows"][1].get("net_liquidation_value") is None
    assert out["valuation_state"]["overlay_applied"] is False
    assert out["valuation_state"]["overlay_reason"] == "date_mismatch"


def test_apply_summary_pnl_to_return_series_payload_uses_summary2_terminal_pnl():
    payload = {
        "method": "holding_return_snapshot",
        "rows": [
            {
                "date": "2026-06-20",
                "market_value": "1000",
                "cost": "900",
                "position_pnl": "80",
                "realized_income": "5",
                "total_pnl": "85",
                "return_rate": "0.094444444444444444",
            },
            {
                "date": "2026-06-21",
                "market_value": "1100",
                "cost": "1000",
                "position_pnl": "90",
                "realized_income": "5",
                "total_pnl": "95",
                "return_rate": "0.095",
            },
        ],
    }

    out = apply_summary_pnl_to_return_series_payload(
        payload,
        {
            "as_of": "2026-06-21",
            "totals": {
                "market_value": "1200",
                "holding_pnl": "123.45",
                "current_float_pnl": "100.00",
                "closed_realized_pnl": "23.45",
            },
        },
    )

    assert out["rows"][0]["total_pnl"] == "85"
    assert out["rows"][1]["market_value"] == "1200"
    assert out["rows"][1]["net_liquidation_value"] == "1200"
    assert out["rows"][1]["total_pnl"] == "123.45"
    assert out["rows"][1]["position_pnl"] == "100.00"
    assert out["rows"][1]["realized_income"] == "23.45"
    assert out["rows"][1]["return_rate"] == "0.12345"
    assert out["rows"][1]["return_pnl_source"] == "summary2.latest"
    assert out["return_pnl_state"]["overlay_applied"] is True


def test_apply_summary_pnl_to_return_series_payload_skips_date_mismatch():
    payload = {
        "method": "holding_return_snapshot",
        "rows": [
            {"date": "2026-06-20", "market_value": "1000", "total_pnl": "85"},
            {"date": "2026-06-21", "market_value": "1100", "total_pnl": "95"},
        ],
    }

    out = apply_summary_pnl_to_return_series_payload(
        payload,
        {
            "as_of": "2026-06-22",
            "totals": {"holding_pnl": "123.45"},
        },
    )

    assert out["rows"][1]["total_pnl"] == "95"
    assert out["return_pnl_state"]["overlay_applied"] is False
    assert out["return_pnl_state"]["overlay_reason"] == "date_mismatch"


def test_apply_holdings_category_pnl_to_section_return_series_payload_uses_category_rows():
    payload = {
        "start": "2026-01-01",
        "summary": {"total_return": "10", "total_return_rate": "0.1", "annualized_return_rate": "0.2"},
        "rows": [
            {"date": "2026-06-20", "market_value": "90", "display_return_rate": "0.1"},
            {"date": "2026-06-21", "market_value": "100", "display_return_rate": "0.2"},
        ],
    }
    holdings_payload = {
        "as_of": "2026-06-21",
        "rows": [
            {
                "market_value_cny": "120",
                "cost_cny": "100",
                "holding_pnl_cny": "20",
                "daily_total_pnl_cny": "2",
                "is_closed": False,
            },
            {
                "market_value_cny": "0",
                "cost_cny": "50",
                "holding_pnl_cny": "5",
                "daily_total_pnl_cny": "0",
                "is_closed": True,
            },
        ],
    }

    out = apply_holdings_category_pnl_to_section_return_series_payload(payload, holdings_payload, source="test")

    latest = out["rows"][-1]
    assert latest["market_value"] == "120"
    assert latest["total_pnl"] == "25"
    assert latest["position_pnl"] == "20"
    assert latest["realized_income"] == "5"
    assert latest["cost"] == "150"
    assert latest["return_rate"] == "0.1666666666666666666666666667"
    assert latest["daily_return_amount"] == "2"
    assert out["summary"]["total_return"] == "25"
    assert out["summary"]["total_return_rate"] == "0.1666666666666666666666666667"
    assert out["section_pnl_state"]["overlay_applied"] is True
    assert out["section_pnl_state"]["row_count"] == 2


def test_apply_holdings_category_pnl_to_section_return_series_payload_skips_date_mismatch():
    payload = {"rows": [{"date": "2026-06-21", "market_value": "100", "total_pnl": "10"}]}
    holdings_payload = {"as_of": "2026-06-22", "rows": [{"holding_pnl_cny": "25", "cost_cny": "100"}]}

    out = apply_holdings_category_pnl_to_section_return_series_payload(payload, holdings_payload)

    assert out["rows"][-1]["total_pnl"] == "10"
    assert out["section_pnl_state"]["overlay_applied"] is False
    assert out["section_pnl_state"]["overlay_reason"] == "date_mismatch"
