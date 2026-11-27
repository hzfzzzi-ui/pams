import sys

import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "api"))

from domain.investments.returns.benchmark_summary import (
    _normalize_return_series,
    summarize_open_holdings_leaderboard,
)


def test_open_holdings_leaderboard_excludes_esop_and_ranks_top3():
    holdings = [
        {"symbol": "HUAWEI_ESOP", "name": "华为 ESOP", "minor_category": "非上市股权", "market_value_cny": 700000, "cost_cny": 500000, "holding_pnl_cny": 200000},
        {"symbol": "601899", "name": "紫金矿业", "account_code": "tonghuashun", "market_value_cny": 120000, "cost_cny": 100000, "holding_pnl_cny": 20000},
        {"symbol": "QQQ", "name": "QQQ ETF", "account_code": "ibkr", "market_value_cny": 110000, "cost_cny": 100000, "holding_pnl_cny": 10000},
        {"symbol": "MU", "name": "Micron", "account_code": "ibkr", "market_value_cny": 45000, "cost_cny": 50000, "holding_pnl_cny": -5000},
        {"symbol": "CBRS", "name": "Cerebras", "account_code": "ibkr", "market_value_cny": 8000, "cost_cny": 10000, "holding_pnl_cny": -2000},
        {"symbol": "CASH_CNY", "name": "现金", "minor_category": "现金", "market_value_cny": 20000, "cost_cny": 20000, "holding_pnl_cny": 0},
    ]
    res = summarize_open_holdings_leaderboard(holdings)
    p = res["portfolio"]
    assert p["esop_pnl_cny"] == 200000.0
    assert p["ex_esop_pnl_cny"] == 23000.0
    assert p["ex_esop_cost_cny"] == 260000.0
    assert p["ex_esop_return_pct"] == round(23000.0 / 260000.0 * 100, 2)
    assert [w["symbol"] for w in res["leaderboard"]["winners"]] == ["601899", "QQQ"]
    assert [l["symbol"] for l in res["leaderboard"]["losers"]] == ["MU", "CBRS"]
    assert [h["symbol"] for h in res["heatmap_items"]] == ["HUAWEI_ESOP", "601899", "QQQ", "MU", "CBRS"]


def test_normalize_return_series_rebases_to_zero():
    rows = [
        {"date": "2025-12-31", "close": "3900"},
        {"date": "2026-01-05", "close": "4000"},
        {"date": "2026-01-06", "close": "4200"},
        {"date": "2026-01-07", "close": "3800"},
    ]
    series = _normalize_return_series(rows, "2026-01-01")
    assert series == [
        {"date": "2026-01-05", "return_pct": 0.0},
        {"date": "2026-01-06", "return_pct": 5.0},
        {"date": "2026-01-07", "return_pct": -5.0},
    ]


def test_completed_local_session_needs_no_provider(monkeypatch):
    from datetime import date
    from domain.investments.returns import benchmark_summary as summary

    day = date(2026, 11, 26)  # US Thanksgiving
    closes = {"2026-11-24": 100, "2026-11-25": 101}
    monkeypatch.setattr(summary, "local_benchmark", lambda cur, start, end, symbol: closes)
    monkeypatch.setattr(summary, "missing_index_closes", lambda *args: pytest.fail("unexpected download"))
    monkeypatch.setattr(summary, "overseas_history", lambda *args: pytest.fail("unexpected fallback"))
    monkeypatch.setattr("domain.market.benchmark_history.latest_completed_session", lambda bucket: date(2026, 11, 27))
    item = summary._load_benchmark_item(None, key="sp500", name="test", symbol="^GSPC",
                                        today=day, bucket="us", secid="100.SPX")
    assert item["as_of"] == "2026-11-25"
    assert item["ytd_return_pct"] == 1
