"""Tests for personal_summary daily and floating PnL rollups in personal_holdings."""

from domain.investments.holdings.personal_holdings import personal_holdings


def test_personal_summary_aggregates_daily_and_floating_pnl():
    payload = {
        "rows": [
            {
                "symbol": "QQQ",
                "name": "QQQ",
                "major_category": "美股",
                "market_value_cny": 10000.0,
                "cost_cny": 8000.0,
                "holding_pnl_cny": 2000.0,
                "daily_total_pnl_cny": 200.0,
                "daily_pnl_stale": False,
            },
            {
                "symbol": "600519",
                "name": "贵州茅台",
                "major_category": "A股",
                "market_value_cny": 20000.0,
                "cost_cny": 22000.0,
                "holding_pnl_cny": -2000.0,
                "daily_pnl_cny": -500.0,
                "daily_pnl_stale": False,
            },
            {
                "symbol": "ESOP",
                "name": "员工持股",
                "major_category": "ESOP",
                "market_value_cny": 50000.0,
                "cost_cny": None,
                "daily_pnl_cny": None,
                "daily_pnl_stale": True,
            },
            {
                "symbol": "CASH",
                "name": "现金",
                "major_category": "现金/活期",
                "market_value_cny": 5000.0,
                "cost_cny": 5000.0,
            },
        ]
    }
    result = personal_holdings(payload, classes={})
    summary = result["personal_summary"]
    assert summary["market_value_cny"] == 80000.0
    assert summary["floating_pnl_cny"] == 0.0
    assert summary["floating_pnl_missing"] == 1
    assert summary["daily_pnl_cny"] == -300.0
    assert summary["daily_pnl_valued_count"] == 2
    assert summary["daily_pnl_total_count"] == 3
    assert summary["daily_pnl_ratio"] is not None
    assert round(summary["daily_pnl_ratio"], 6) == round(-300.0 / 30300.0, 6)


def test_personal_summary_handles_all_missing_daily():
    payload = {
        "rows": [
            {
                "symbol": "ESOP",
                "name": "员工持股",
                "major_category": "ESOP",
                "market_value_cny": 50000.0,
                "cost_cny": None,
                "daily_pnl_cny": None,
            }
        ]
    }
    summary = personal_holdings(payload, classes={})["personal_summary"]
    assert summary["daily_pnl_cny"] is None
    assert summary["daily_pnl_ratio"] is None
    assert summary["daily_pnl_valued_count"] == 0
    assert summary["daily_pnl_total_count"] == 1
    assert summary["floating_pnl_cny"] is None
