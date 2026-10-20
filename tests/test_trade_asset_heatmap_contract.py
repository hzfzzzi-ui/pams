from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from services.trade_attribution import _build_trade_asset_heatmap, _build_trade_review_workbench  # noqa: E402


def test_trade_asset_heatmap_area_uses_decision_effect_not_exposure() -> None:
    heatmap = _build_trade_asset_heatmap(
        [
            {
                "trade_id": 1,
                "trade_date": date(2026, 6, 1),
                "name": "NVDA - NVIDIA CORP",
                "symbol": "NVDA",
                "side": "buy",
                "estimated_decision_effect_cny": Decimal("300"),
                "gross_exposure_cny": Decimal("10000"),
            },
            {
                "trade_id": 2,
                "trade_date": date(2026, 6, 2),
                "name": "NVDA - NVIDIA CORP",
                "symbol": "NVDA",
                "side": "sell",
                "estimated_decision_effect_cny": Decimal("-120"),
                "gross_exposure_cny": Decimal("6000"),
            },
        ]
    )

    asset = heatmap["assets"][0]

    assert asset["estimated_decision_effect_cny"] == "180"
    assert asset["gross_exposure_cny"] == "16000"
    assert asset["weight_cny"] == "180"


def test_trade_asset_heatmap_bridges_actual_pnl_and_action_attribution() -> None:
    heatmap = _build_trade_asset_heatmap(
        [
            {
                "trade_id": 1,
                "trade_date": date(2026, 6, 1),
                "name": "紫金矿业",
                "symbol": "601899",
                "side": "buy",
                "estimated_decision_effect_cny": Decimal("-300"),
                "gross_exposure_cny": Decimal("10000"),
                "fee_drag_cny": Decimal("-15"),
                "realized_pnl_cny": Decimal("0"),
            },
            {
                "trade_id": 2,
                "trade_date": date(2026, 6, 2),
                "name": "紫金矿业",
                "symbol": "601899",
                "side": "sell",
                "estimated_decision_effect_cny": Decimal("120"),
                "gross_exposure_cny": Decimal("6000"),
                "fee_drag_cny": Decimal("-10"),
                "realized_pnl_cny": Decimal("500"),
                "trade_plan": "reduce_cycle_exposure",
            },
        ],
        actual_pnl_by_asset={
            "601899": {
                "actual_total_pnl_cny": Decimal("1000"),
                "actual_daily_pnl_cny": Decimal("50"),
                "actual_daily_price_pnl_cny": Decimal("48"),
                "actual_daily_fx_pnl_cny": Decimal("2"),
                "market_value_cny": Decimal("20000"),
            }
        },
    )

    asset = heatmap["assets"][0]
    breakdown = {item["key"]: item for item in asset["attribution_breakdown"]}

    assert asset["actual_total_pnl_cny"] == "1000"
    assert asset["estimated_decision_effect_cny"] == "-180"
    assert asset["buy_timing_effect_cny"] == "-300"
    assert asset["sell_timing_effect_cny"] == "120"
    assert asset["fee_drag_cny"] == "-25"
    assert asset["unexplained_pnl_cny"] == "1205"
    assert asset["weight_cny"] == "1000"
    assert asset["impact_tone"] == "gain"
    assert asset["attribution_status"] == "partial_with_actual_pnl"
    assert breakdown["actual_total"]["label"] == "真实总盈亏"
    assert breakdown["unexplained"]["status"] == "needs_benchmark"
    assert any(item["title"] == "真实盈亏与动作贡献方向不同" for item in asset["rule_insights"])


def test_trade_review_pattern_examples_explain_asset_reasons() -> None:
    def event(trade_id: int, effect: str) -> dict:
        return {
            "trade_id": trade_id,
            "trade_date": date(2026, 6, trade_id),
            "name": f"TEST{trade_id}",
            "symbol": f"T{trade_id}",
            "side": "buy",
            "buy_reason": "trend_signal",
            "review_tags": [],
            "current_return_pct": Decimal(effect) / Decimal("100"),
            "estimated_decision_effect_cny": Decimal(effect),
            "priority_score": 1,
            "auto_review": {
                "level": "neutral",
                "kind": "validated_buy",
                "label": "买入有效",
                "summary": "买入后形成正贡献",
                "needs_human_review": False,
                "queue_kinds": [],
            },
        }

    workbench = _build_trade_review_workbench(
        [
            event(1, "500"),
            event(2, "1200"),
            event(3, "-300"),
        ],
        {
            "buy_reason": [
                {
                    "key": "trend_signal",
                    "label": "trend_signal",
                    "sample_count": 3,
                    "avg_return_pct": "3",
                    "win_rate": "0.67",
                    "estimated_decision_effect_cny": "1400",
                    "gross_exposure_cny": "30000",
                    "missed_gain_count": 0,
                    "avoided_loss_count": 0,
                    "quality": "low_sample",
                }
            ],
            "sell_reason": [],
            "review_tags": [],
        },
        sample_count=3,
        missing_price=0,
    )

    trend_row = next(group for group in workbench["pattern_universe"] if group["key"] == "buy_reason")["rows"][0]

    assert [item["trade_id"] for item in trend_row["examples"]] == [2, 1, 3]
    assert trend_row["examples"][0]["contribution_reason"]
    assert trend_row["examples"][0]["auto_summary"] == "买入后形成正贡献"
