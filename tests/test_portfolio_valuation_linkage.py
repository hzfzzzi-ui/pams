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
