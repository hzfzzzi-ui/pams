import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "api"))

from domain.market.freshness_guard import business_days_between, compute_ths_trade_projection


def test_business_days_between_excludes_weekends():
    # 2026-11-20 is Friday, 2026-11-23 is Monday -> 1 trading day
    assert business_days_between(date(2026, 11, 20), date(2026, 11, 23)) == 1
    # 2026-11-20 (Fri) to 2026-11-26 (Thu) -> Mon, Tue, Wed, Thu = 4 trading days
    assert business_days_between(date(2026, 11, 20), date(2026, 11, 26)) == 4


def test_ths_trade_projection_rolls_forward_post_snapshot_buys():
    snapshot = [
        {"symbol": "601899", "name": "紫金矿业", "quantity": 1000.0, "as_of": date(2026, 11, 10)},
        {"symbol": "002281", "name": "光迅科技", "quantity": 500.0, "as_of": date(2026, 11, 10)},
    ]
    trades = [
        {"symbol": "601899", "asset_name": "紫金矿业", "side": "buy", "quantity": 500.0, "trade_date": date(2026, 11, 19)},
        {"symbol": "601899", "asset_name": "紫金矿业", "side": "buy", "quantity": 300.0, "trade_date": date(2026, 11, 24)},
        {"symbol": "002281", "asset_name": "光迅科技", "side": "dividend", "quantity": 0.0, "trade_date": date(2026, 11, 18)},
    ]
    adj = compute_ths_trade_projection(snapshot, trades)
    assert len(adj) == 1
    assert adj[0]["symbol"] == "601899"
    assert adj[0]["snapshot_qty"] == 1000.0
    assert adj[0]["delta_qty"] == 800.0
    assert adj[0]["projected_qty"] == 1800.0
    assert adj[0]["latest_trade_date"] == "2026-11-24"
