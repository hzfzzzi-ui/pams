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


def test_session_estimate_and_historical_close_have_distinct_daily_buckets():
    today = "2026-11-27"
    qqq = dict(symbol="QQQ", major_category="美股", currency="USD", market_value_cny=120,
               daily_return_status="pending", daily_pnl_stale=True, daily_return_status_as_of="2026-11-25",
               recent_confirmed_return={"status": "confirmed", "as_of": "2026-11-25",
                                        "fields": {"daily_total_pnl_cny": -15}})
    fund = dict(symbol="OTC", minor_category="A股基金", market_value_cny=80, daily_total_pnl_cny=0,
                daily_return_status="confirmed", daily_return_status_as_of=today)
    stock = dict(major_category="A股", currency="CNY", daily_return_status="confirmed",
                 daily_return_status_as_of=today)
    payload = {"rows": [qqq, fund, dict(stock, symbol="A", daily_total_pnl_cny=2, market_value_cny=50),
                        dict(stock, symbol="B", daily_total_pnl_cny=-5, market_value_cny=70)]}

    def read():
        return personal_holdings(payload, {"QQQ": "etf_us"}, active_date=today)

    data = read()
    funds = data["filter_summaries"]["基金"]
    assert funds[""]["market_value_cny"] == 200
    assert funds[""]["daily_pnl_valued_count"] == 1
    assert funds[""]["daily_pnl_total_count"] == 2
    assert funds["场外"]["daily_pnl_cny"] == 0
    assert funds["场内"]["daily_pnl_cny"] is None
    assert data["rows"][0]["recent_confirmed_return"] == qqq["recent_confirmed_return"]
    a_shares = data["filter_summaries"]["股票"]["A股"]
    assert a_shares["market_value_cny"] == 120
    assert data["personal_summary"]["daily_pnl_cny"] == a_shares["daily_pnl_cny"] == -3
    qqq.update(daily_return_status="estimated", daily_pnl_stale=False, daily_total_pnl_cny=3)
    assert read()["filter_summaries"]["基金"]["场内"]["daily_pnl_cny"] is None
    qqq["daily_return_status_as_of"] = today
    assert read()["filter_summaries"]["基金"][""]["daily_pnl_cny"] == 3

def test_open_us_daily_bar_is_not_persisted_as_a_close():
    from datetime import date, datetime
    from zoneinfo import ZoneInfo
    from domain.market.price_snapshots import _completed_yfinance_closes

    prior, holiday, session = (date(2026, 11, d) for d in (25, 26, 27))
    rows = [{"symbol": "QQQ", "major_category": "美股", "currency": "USD"}]
    history = {"QQQ": [(prior, 100), (holiday, 100), (session, 101)]}
    ny = ZoneInfo("America/New_York")
    assert _completed_yfinance_closes(rows, history, now=datetime(2026, 11, 27, 9, 35, tzinfo=ny)) == {
        ("QQQ", prior): 100}
    assert _completed_yfinance_closes(rows, history, now=datetime(2026, 11, 27, 13, 16, tzinfo=ny)) == {
        ("QQQ", prior): 100, ("QQQ", session): 101}
