from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from services.trade_attribution import _build_trade_asset_heatmap  # noqa: E402


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
