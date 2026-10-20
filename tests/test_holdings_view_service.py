from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from services.holdings_view import (  # noqa: E402
    _adjust_a_stock_daily_pnl_for_same_day_buys,
    _apply_daily_return_status,
    _drop_superseded_account_snapshot_rows,
    _suppress_stale_daily_pnl,
    build_api_holdings_latest_payload,
    build_holdings_by_category_payload,
    build_holdings_history_payload,
    build_holdings_latest_payload,
    latest_complete_snapshot_date,
)
from services.holdings_positions import (  # noqa: E402
    _enrich_realized_trade_pnl,
    _filter_current_holding_rows,
    _tradingview_closed_position_rows,
)


class FakeCursor:
    def __init__(self, results):
        self.results = list(results)
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        if not self.results and self.executed and "to_regclass" in self.executed[-1][0]:
            return {"exists": False}
        return self.results.pop(0)

    def fetchall(self):
        return self.results.pop(0)


def test_latest_complete_snapshot_date_uses_account_completeness_guard():
    cur = FakeCursor([{"as_of": date(2026, 6, 18)}])

    result = latest_complete_snapshot_date(
        cur,
        view_as_of=date(2026, 6, 19),
        categories=["美股"],
    )

    assert result == date(2026, 6, 18)
    sql, params = cur.executed[0]
    assert "complete_account_snapshot" in sql
    assert "reference_row_count" in sql
    assert params == [date(2026, 6, 19), date(2026, 6, 19), ["美股"]]


def test_build_holdings_latest_payload_preserves_legacy_shape_and_owner_ratio():
    cur = FakeCursor(
        [
            {"as_of": date(2026, 5, 31)},
            [
                {
                    "account": "tonghuashun",
                    "type": "A股个股",
                    "symbol": "601899",
                    "asset_id": "A_601899",
                    "name": "紫金矿业",
                    "currency": "CNY",
                    "market_value": Decimal("1000.00"),
                    "market_value_native": Decimal("1000.00"),
                    "market_value_cny": Decimal("1000.00"),
                    "cost": Decimal("900.00"),
                    "cost_cny": Decimal("900.00"),
                    "holding_pnl": Decimal("100.00"),
                    "holding_pnl_native": Decimal("100.00"),
                    "holding_pnl_cny": Decimal("100.00"),
                    "daily_pnl": Decimal("0"),
                    "daily_pnl_cny": Decimal("0"),
                    "major_category": "A股",
                    "minor_category": "A股个股",
                    "as_of": date(2026, 5, 31),
                    "snapshot_as_of": date(2026, 5, 31),
                    "quantity": Decimal("100"),
                    "buy_price": Decimal("9"),
                    "current_price": Decimal("10"),
                },
                {
                    "account": "demo",
                    "type": "A股个股",
                    "symbol": "000001",
                    "name": "排除账户",
                    "currency": "CNY",
                    "market_value_cny": Decimal("100.00"),
                    "major_category": "A股",
                    "minor_category": "A股个股",
                    "as_of": date(2026, 5, 31),
                },
            ],
            [],
            {"exists": False},
            [],
            {"exists": False},
        ]
    )

    payload = build_holdings_latest_payload(
        cur,
        limit=500,
        include_closed=False,
        view_as_of=date(2026, 6, 1),
        excluded_accounts={"demo"},
        owner_ratios={"tonghuashun": Decimal("0.625")},
        default_owner_ratio=Decimal("1"),
    )

    assert set(payload) == {"as_of", "data_as_of_min", "data_as_of_max", "rows"}
    assert payload["as_of"] == "2026-06-01"
    assert payload["data_as_of_min"] == "2026-05-31"
    assert payload["data_as_of_max"] == "2026-05-31"
    assert len(payload["rows"]) == 1
    row = payload["rows"][0]
    assert row["account"] == "tonghuashun"
    assert row["asset_id"] == "A_601899"
    assert row["is_closed"] is False
    assert row["owner_ratio"] == "0.625"
    assert Decimal(row["market_value_cny"]) == Decimal("625.00000")
    assert Decimal(row["market_value_gross_cny"]) == Decimal("1000.00")


def test_build_holdings_latest_payload_uses_latest_close_for_estimate_baseline():
    cur = FakeCursor(
        [
            {"as_of": date(2026, 6, 22)},
            [
                {
                    "account": "ibkr",
                    "type": "美股",
                    "symbol": "MU",
                    "name": "MICRON TECHNOLOGY INC",
                    "currency": "USD",
                    "market_value": Decimal("11701.60"),
                    "market_value_native": Decimal("11701.60"),
                    "market_value_cny": Decimal("79600.88"),
                    "cost": Decimal("9499.50"),
                    "cost_cny": Decimal("64596.60"),
                    "holding_pnl": Decimal("2202.10"),
                    "holding_pnl_native": Decimal("2202.10"),
                    "holding_pnl_cny": Decimal("14974.28"),
                    "daily_pnl": Decimal("361.70"),
                    "daily_pnl_cny": Decimal("2459.56"),
                    "major_category": "美股",
                    "minor_category": "美股个股",
                    "as_of": date(2026, 6, 22),
                    "snapshot_as_of": date(2026, 6, 22),
                    "quantity": Decimal("10"),
                    "buy_price": Decimal("949.95"),
                    "current_price": Decimal("1170.16"),
                    "previous_price": Decimal("1133.99"),
                }
            ],
            [],
            {"exists": False},
            [],
            {"exists": False},
        ]
    )

    build_holdings_latest_payload(
        cur,
        limit=500,
        include_closed=False,
        view_as_of=date(2026, 6, 22),
        excluded_accounts=set(),
        owner_ratios={},
        default_owner_ratio=Decimal("1"),
    )

    query = cur.executed[1][0]
    assert "h.current_price IS DISTINCT FROM curr_overseas_px.close" in query
    assert "THEN curr_overseas_px.close" in query
    assert "prev_basis.previous_price AS previous_price" in query
    assert "pams_fund_nav_estimates" in query
    assert "pams_asset_market_daily" in query
    assert "databento_estimate" in query
    assert "ibkr_tws_estimate" in query
    assert "US_OVERNIGHT" in query
    assert "LIKE '%%" in query
    assert "daily_pnl_status" in query
    assert "fund_nav_estimated_unit_nav" in query


def test_build_holdings_latest_payload_includes_closed_rows_when_requested():
    cur = FakeCursor(
        [
            {"as_of": date(2026, 5, 31)},
            [],
            [
                {
                    "account": "tencent_licaitong",
                    "type": "基金",
                    "symbol": "fund-a",
                    "name": "已平仓基金",
                    "currency": "CNY",
                    "market_value": Decimal("0"),
                    "market_value_native": Decimal("0"),
                    "market_value_cny": Decimal("0"),
                    "cost": Decimal("1000"),
                    "cost_cny": Decimal("1000"),
                    "holding_pnl": Decimal("50"),
                    "holding_pnl_native": Decimal("50"),
                    "holding_pnl_cny": Decimal("50"),
                    "daily_pnl": Decimal("0"),
                    "daily_pnl_cny": Decimal("0"),
                    "major_category": "基金",
                    "minor_category": "基金",
                    "as_of": date(2026, 5, 20),
                    "snapshot_as_of": date(2026, 5, 20),
                    "is_closed": True,
                }
            ],
            [],
        ]
    )

    payload = build_holdings_latest_payload(
        cur,
        limit=100,
        include_closed=True,
        view_as_of=date(2026, 6, 1),
        excluded_accounts=set(),
        owner_ratios={},
        default_owner_ratio=Decimal("1"),
    )

    assert payload["as_of"] == "2026-06-01"
    assert payload["data_as_of_min"] == "2026-05-20"
    assert payload["data_as_of_max"] == "2026-05-20"
    assert payload["rows"][0]["is_closed"] is True
    assert payload["rows"][0]["holding_pnl_cny"] == "50"


def test_build_holdings_latest_payload_handles_empty_snapshot():
    cur = FakeCursor([{"as_of": None}])

    payload = build_holdings_latest_payload(
        cur,
        limit=100,
        include_closed=False,
        view_as_of=date(2026, 6, 1),
        excluded_accounts=set(),
        owner_ratios={},
        default_owner_ratio=Decimal("1"),
    )

    assert payload == {"as_of": None, "rows": []}


def test_build_api_holdings_latest_payload_preserves_filters_and_rows():
    cur = FakeCursor(
        [
            {"as_of": date(2026, 5, 31)},
            [
                {
                    "id": 1,
                    "symbol": "QQQ",
                    "asset_id": "ETF_QQQ",
                    "name": "Invesco QQQ",
                    "asset_liability": "资产",
                    "currency": "USD",
                    "account_code": "ibkr",
                    "major_category": "美股",
                    "minor_category": "美股基金",
                    "market_value": Decimal("100.25"),
                    "cost": Decimal("90.00"),
                    "holding_pnl": Decimal("10.25"),
                    "as_of": date(2026, 5, 31),
                    "created_at": date(2026, 5, 31),
                    "updated_at": date(2026, 5, 31),
                }
            ],
        ]
    )

    payload = build_api_holdings_latest_payload(
        cur,
        account_code="ibkr",
        major_category="美股",
        limit=10,
    )

    assert set(payload) == {"as_of", "rows"}
    assert payload["as_of"] == "2026-05-31"
    assert payload["rows"][0]["symbol"] == "QQQ"
    assert payload["rows"][0]["asset_id"] == "ETF_QQQ"
    assert payload["rows"][0]["market_value"] == "100.25"
    assert payload["rows"][0]["daily_return_status"] == "pending"
    assert cur.executed[0][1] == ["ibkr", "美股"]
    assert cur.executed[1][1] == ["ibkr", "美股", date(2026, 5, 31), 10]

def test_build_api_holdings_latest_payload_respects_market_view_date():
    cur = FakeCursor(
        [
            {"as_of": date(2026, 6, 16)},
            [
                {
                    "id": 1,
                    "symbol": "QQQ",
                    "asset_id": "ETF_QQQ",
                    "name": "Invesco QQQ",
                    "asset_liability": "asset",
                    "currency": "USD",
                    "account_code": "ibkr",
                    "major_category": "us_stock",
                    "minor_category": "us_etf",
                    "market_value": Decimal("100.25"),
                    "cost": Decimal("90.00"),
                    "holding_pnl": Decimal("10.25"),
                    "as_of": date(2026, 6, 16),
                    "created_at": date(2026, 6, 16),
                    "updated_at": date(2026, 6, 16),
                }
            ],
        ]
    )

    payload = build_api_holdings_latest_payload(
        cur,
        account_code="ibkr",
        major_category=None,
        limit=10,
        view_as_of=date(2026, 6, 16),
    )

    assert payload["as_of"] == "2026-06-16"
    assert payload["rows"][0]["symbol"] == "QQQ"
    assert payload["rows"][0]["asset_id"] == "ETF_QQQ"
    assert "as_of <= %s" in cur.executed[0][0]
    assert cur.executed[0][1] == ["ibkr", date(2026, 6, 16)]
    assert cur.executed[1][1] == ["ibkr", date(2026, 6, 16), 10]


def test_build_api_holdings_latest_payload_handles_empty_snapshot():
    cur = FakeCursor([{"as_of": None}])

    payload = build_api_holdings_latest_payload(
        cur,
        account_code=None,
        major_category=None,
        limit=10,
    )

    assert payload == {"as_of": None, "rows": []}


def test_build_holdings_history_payload_preserves_series_and_metadata():
    cur = FakeCursor(
        [
            {"as_of": date(2026, 5, 31)},
            {"exists": False},
            {"exists": False},
            [
                {"date": date(2026, 5, 25), "value": Decimal("100.50")},
                {"date": date(2026, 6, 1), "value": Decimal("120.00")},
            ],
        ]
    )

    payload = build_holdings_history_payload(
        cur,
        category="基金",
        days=7,
        interval="weekly",
        minor_category=None,
        view_as_of=date(2026, 6, 1),
        tonghuashun_owner_ratio=Decimal("0.625"),
        owner_ratio_sql="1",
        owner_ratio_params=[],
        excluded_accounts=[],
        base_currency="CNY",
    )

    assert {"category", "start", "end", "interval", "base_currency", "rows", "series"} <= set(payload)
    assert payload["category"] == "基金"
    assert payload["start"] == "2026-05-25"
    assert payload["end"] == "2026-06-01"
    assert payload["interval"] == "weekly"
    assert payload["base_currency"] == "CNY"
    assert payload["rows"] == payload["series"]
    assert payload["series"][0] == {"date": "2026-05-25", "value": 100.5}


def test_build_holdings_history_payload_handles_empty_snapshot():
    cur = FakeCursor([{"as_of": None}])

    payload = build_holdings_history_payload(
        cur,
        category="A股",
        days=90,
        interval="daily",
        minor_category=None,
        view_as_of=date(2026, 6, 1),
        tonghuashun_owner_ratio=Decimal("0.625"),
        owner_ratio_sql="1",
        owner_ratio_params=[],
        excluded_accounts=[],
        base_currency="CNY",
    )

    assert payload["category"] == "A股"
    assert payload["rows"] == []
    assert payload["series"] == []


def _current_holding_row(**overrides):
    row = {
        "account": "tonghuashun",
        "minor_category": "A股个股",
        "symbol": "601899",
        "name": "紫金矿业",
        "currency": "CNY",
        "market_value": Decimal("1000.00"),
        "market_value_native": Decimal("1000.00"),
        "market_value_cny": Decimal("1000.00"),
        "cost": Decimal("900.00"),
        "cost_cny": Decimal("900.00"),
        "holding_pnl": Decimal("100.00"),
        "holding_pnl_native": Decimal("100.00"),
        "holding_pnl_cny": Decimal("100.00"),
        "daily_pnl": Decimal("0"),
        "daily_pnl_cny": Decimal("0"),
        "major_category": "A股",
        "as_of": date(2026, 5, 31),
        "snapshot_as_of": date(2026, 5, 31),
        "quantity": Decimal("100"),
        "buy_price": Decimal("9"),
        "current_price": Decimal("10"),
    }
    row.update(overrides)
    return row


def test_build_holdings_by_category_payload_handles_empty_snapshot():
    payload = build_holdings_by_category_payload(
        FakeCursor([{"as_of": None}]),
        category="A股",
        limit=10,
        minor_category=None,
        include_closed=False,
        view_as_of=date(2026, 6, 1),
        excluded_accounts=set(),
        owner_ratios={},
        default_owner_ratio=Decimal("1"),
    )

    assert payload == {"as_of": None, "rows": []}


def test_build_holdings_by_category_payload_preserves_normal_category_shape():
    cur = FakeCursor(
        [{"as_of": date(2026, 5, 31)}, [_current_holding_row()], [], {"exists": False}, [], {"exists": False}]
    )

    payload = build_holdings_by_category_payload(
        cur,
        category="A股",
        limit=10,
        minor_category=None,
        include_closed=False,
        view_as_of=date(2026, 6, 1),
        excluded_accounts=set(),
        owner_ratios={"tonghuashun": Decimal("0.625")},
        default_owner_ratio=Decimal("1"),
    )

    assert set(payload) == {"as_of", "data_as_of_min", "data_as_of_max", "rows"}
    assert payload["as_of"] == "2026-06-01"
    assert payload["rows"][0]["symbol"] == "601899"
    assert Decimal(payload["rows"][0]["market_value_cny"]) == Decimal("625.00000")
    assert cur.executed[1][1] == [date(2026, 6, 1), date(2026, 6, 1), "A股", 10]


def test_build_holdings_by_category_payload_preserves_minor_category_filter():
    cur = FakeCursor(
        [{"as_of": date(2026, 5, 31)}, [_current_holding_row(minor_category="A股基金")], [], {"exists": False}, []]
    )

    build_holdings_by_category_payload(
        cur,
        category="A股",
        limit=10,
        minor_category="A股基金",
        include_closed=False,
        view_as_of=date(2026, 6, 1),
        excluded_accounts=set(),
        owner_ratios={},
        default_owner_ratio=Decimal("1"),
    )

    assert cur.executed[1][1] == [date(2026, 6, 1), date(2026, 6, 1), "A股", "A股基金", 10]
    assert "AND h.minor_category = %s" in cur.executed[1][0]


def test_drop_superseded_account_snapshot_rows_uses_account_latest_date():
    rows = [
        _current_holding_row(
            account="tonghuashun",
            major_category="现金",
            minor_category="现金/活期",
            symbol="$CASH",
            as_of=date(2026, 6, 3),
            snapshot_as_of=date(2026, 6, 3),
        ),
        _current_holding_row(
            account="tonghuashun",
            major_category="A股",
            minor_category="A股",
            symbol="601899",
            as_of=date(2026, 6, 16),
            snapshot_as_of=date(2026, 6, 16),
        ),
        _current_holding_row(
            account="static_cash",
            major_category="现金",
            minor_category="现金/活期",
            symbol="$CASH",
            as_of=date(2026, 2, 19),
            snapshot_as_of=date(2026, 2, 19),
        ),
    ]

    out = _drop_superseded_account_snapshot_rows(rows)

    assert [row["account"] for row in out] == ["tonghuashun", "static_cash"]
    assert out[0]["symbol"] == "601899"


def test_filter_current_holding_rows_keeps_negative_liabilities():
    rows = [
        {
            "account": "ibkr",
            "asset_liability": "负债",
            "major_category": "借款",
            "market_value_cny": Decimal("-100.00"),
        },
        {
            "account": "empty_asset",
            "asset_liability": "资产",
            "market_value_cny": Decimal("0"),
        },
    ]

    out = _filter_current_holding_rows(rows, excluded_accounts=set())

    assert len(out) == 1
    assert out[0]["account"] == "ibkr"
    assert out[0]["is_closed"] is False


def test_build_holdings_by_category_payload_skips_duplicate_ibkr_previous_snapshot():
    cur = FakeCursor(
        [
            {"as_of": date(2026, 6, 9)},
            [
                _current_holding_row(
                    account="ibkr",
                    major_category="美股",
                    minor_category="美股基金",
                    symbol="QQQ",
                    name="QQQ",
                    currency="USD",
                    as_of=date(2026, 6, 9),
                    snapshot_as_of=date(2026, 6, 9),
                )
            ],
            [],
            {"exists": False},
            [],
            {"exists": False},
        ]
    )

    build_holdings_by_category_payload(
        cur,
        category="美股",
        limit=10,
        minor_category=None,
        include_closed=False,
        view_as_of=date(2026, 6, 9),
        excluded_accounts=set(),
        owner_ratios={},
        default_owner_ratio=Decimal("1"),
    )

    query = cur.executed[1][0]
    assert "h.account_code <> 'ibkr'" in query
    assert "ph.current_price IS DISTINCT FROM h.current_price" in query
    assert "prev_overseas_px.close" in query
    assert "THEN h.current_price" in query
    assert "h.current_price IS DISTINCT FROM curr_overseas_px.close" in query
    assert "THEN curr_overseas_px.close" in query
    assert "THEN curr_overseas_px.trade_date" in query
    assert "THEN h.current_price * h.quantity" in query
    assert "o.trade_date <= h.as_of" in query
    assert "o.trade_date < curr_overseas_px.trade_date" in query
    assert "CASE WHEN UPPER(h.currency) = 'USD' THEN 1 ELSE 0 END" not in query


def test_build_holdings_by_category_payload_suppresses_stale_daily_pnl():
    cur = FakeCursor(
        [
            {"as_of": date(2026, 6, 5)},
            [
                _current_holding_row(
                    minor_category="A股基金",
                    as_of=date(2026, 6, 5),
                    snapshot_as_of=date(2026, 6, 5),
                    daily_pnl=Decimal("-10"),
                    daily_pnl_cny=Decimal("-10"),
                    daily_total_pnl_cny=Decimal("-10"),
                    previous_price=Decimal("11"),
                    price_change_pct=Decimal("-0.10"),
                    price_change_amount=Decimal("-1"),
                )
            ],
            [],
            {"exists": False},
            [],
        ]
    )

    payload = build_holdings_by_category_payload(
        cur,
        category="A股",
        limit=10,
        minor_category="A股基金",
        include_closed=False,
        view_as_of=date(2026, 6, 6),
        excluded_accounts=set(),
        owner_ratios={},
        default_owner_ratio=Decimal("1"),
    )

    row = payload["rows"][0]
    assert row["market_value_cny"] == "1000.00"
    assert row["daily_pnl_cny"] == "0"
    assert row["daily_total_pnl_cny"] == "0"
    assert row["price_change_pct"] == "0"
    assert row["previous_price"] == row["current_price"]
    assert row["daily_pnl_stale"] is True
    assert row["daily_pnl_as_of"] == "2026-06-05"


def test_suppress_stale_daily_pnl_keeps_rows_inside_rollover_window():
    rows = [
        {"as_of": date(2026, 6, 14), "daily_pnl_cny": Decimal("1"), "daily_total_pnl_cny": Decimal("1")},
        {"as_of": date(2026, 6, 15), "daily_pnl_cny": Decimal("2"), "daily_total_pnl_cny": Decimal("2")},
        {"as_of": date(2026, 6, 16), "daily_pnl_cny": Decimal("3"), "daily_total_pnl_cny": Decimal("3")},
    ]

    out = _suppress_stale_daily_pnl(
        rows,
        view_as_of=date(2026, 6, 16),
        daily_pnl_start_as_of=date(2026, 6, 15),
    )

    assert out[0]["daily_total_pnl_cny"] == Decimal("0")
    assert out[0]["daily_pnl_stale"] is True
    assert out[1]["daily_total_pnl_cny"] == Decimal("2")
    assert out[1].get("daily_pnl_stale") is None
    assert out[2]["daily_total_pnl_cny"] == Decimal("3")
    assert out[2].get("daily_pnl_stale") is None


def test_apply_daily_return_status_uses_quote_snapshot_status():
    cur = FakeCursor(
        [
            {"exists": True},
            [
                {
                    "asset_key": "US:MU",
                    "asset_id": "US:MU",
                    "symbol": "MU",
                    "name": "MICRON TECHNOLOGY INC",
                    "quote_date": date(2026, 6, 24),
                    "return_status": "estimated",
                    "source": "databento_estimate",
                    "provider": "databento",
                    "quote_session": "afterhours",
                    "market": "US_EXTENDED",
                }
            ],
            {"exists": False},
        ]
    )

    rows = [
        {
            "symbol": "MU",
            "name": "MICRON TECHNOLOGY INC",
            "as_of": date(2026, 6, 24),
            "daily_total_pnl_cny": Decimal("100"),
        }
    ]

    out = _apply_daily_return_status(cur, rows, date(2026, 6, 24))

    assert out[0]["daily_return_status"] == "estimated"
    assert out[0]["daily_return_status_label"] == "预估收益"
    assert out[0]["daily_return_status_short_label"] == "预估"
    assert out[0]["daily_return_status_as_of"] == date(2026, 6, 24)
    assert out[0]["daily_return_status_source"] == "databento_estimate"


def test_apply_daily_return_status_does_not_confirm_historical_fund_nav():
    cur = FakeCursor([
        {"exists": False},
        {"exists": True},
        [{"symbol": "011613", "quote_date": date(2026, 6, 26),
          "source": "tushare_fund_nav", "market": "CN_FUND"}],
    ])
    row = {"symbol": "011613", "as_of": date(2026, 10, 9),
           "daily_total_pnl_cny": Decimal("0"), "market_value_cny": Decimal("51520.67")}
    out = _apply_daily_return_status(cur, [row.copy()], date(2026, 10, 20))[0]
    assert out["daily_return_status"] == "pending"
    assert out["daily_return_status_label"] == "未刷新"
    assert out["daily_return_status_as_of"] == date(2026, 6, 26)
    assert all(out[key] == value for key, value in row.items())


def test_apply_daily_return_status_does_not_reuse_prior_quote_confirmation():
    cur = FakeCursor([
        {"exists": True},
        [{"symbol": "MU", "quote_date": date(2026, 10, 19),
          "return_status": "confirmed", "source": "ibkr_flex", "market": "US"}],
    ])
    out = _apply_daily_return_status(cur, [{"symbol": "MU"}], date(2026, 10, 20))[0]
    assert out["daily_return_status"] == "pending"
    assert out["daily_return_status_as_of"] == date(2026, 10, 19)


def test_apply_daily_return_status_preserves_same_day_fund_confirmation():
    cur = FakeCursor([
        {"exists": False}, {"exists": True},
        [{"symbol": "011613", "quote_date": date(2026, 10, 20),
          "source": "tushare_fund_nav", "market": "CN_FUND"}],
    ])
    out = _apply_daily_return_status(cur, [{"symbol": "011613"}], date(2026, 10, 20))[0]
    assert out["daily_return_status"] == "confirmed"


def test_apply_daily_return_status_marks_stale_daily_pnl_pending():
    rows = [
        {
            "as_of": date(2026, 6, 23),
            "daily_pnl_stale": True,
            "daily_pnl_as_of": date(2026, 6, 23),
        }
    ]

    out = _apply_daily_return_status(FakeCursor([]), rows, date(2026, 6, 24))

    assert out[0]["daily_return_status"] == "pending"
    assert out[0]["daily_return_status_label"] == "未刷新"
    assert out[0]["daily_return_status_short_label"] == "未刷新"
    assert out[0]["daily_return_status_as_of"] == date(2026, 6, 23)


def test_build_holdings_by_category_payload_includes_closed_rows_when_requested():
    cur = FakeCursor(
        [
            {"as_of": date(2026, 5, 31)},
            [],
            [
                {
                    "account": "tencent_licaitong",
                    "type": "基金",
                    "symbol": "fund-a",
                    "name": "已平仓基金",
                    "currency": "CNY",
                    "market_value": Decimal("0"),
                    "market_value_native": Decimal("0"),
                    "market_value_cny": Decimal("0"),
                    "cost": Decimal("1000"),
                    "cost_cny": Decimal("1000"),
                    "holding_pnl": Decimal("50"),
                    "holding_pnl_native": Decimal("50"),
                    "holding_pnl_cny": Decimal("50"),
                    "daily_pnl": Decimal("0"),
                    "daily_pnl_cny": Decimal("0"),
                    "major_category": "基金",
                    "minor_category": "基金",
                    "as_of": date(2026, 5, 20),
                    "snapshot_as_of": date(2026, 5, 20),
                    "is_closed": True,
                }
            ],
        ]
    )

    payload = build_holdings_by_category_payload(
        cur,
        category="基金",
        limit=10,
        minor_category=None,
        include_closed=True,
        view_as_of=date(2026, 6, 1),
        excluded_accounts=set(),
        owner_ratios={},
        default_owner_ratio=Decimal("1"),
    )

    assert payload["rows"][0]["is_closed"] is True
    assert cur.executed[2][1] == ["基金", 10]


def test_build_holdings_by_category_payload_uses_other_category_branch():
    cur = FakeCursor(
        [
            {"as_of": date(2026, 5, 31)},
            [_current_holding_row(account="manual", major_category="手动资产", minor_category="其他")],
            [],
        ]
    )

    payload = build_holdings_by_category_payload(
        cur,
        category="其他",
        limit=10,
        minor_category=None,
        include_closed=True,
        view_as_of=date(2026, 6, 1),
        excluded_accounts=set(),
        owner_ratios={},
        default_owner_ratio=Decimal("1"),
    )

    assert payload["rows"][0]["major_category"] == "手动资产"
    assert cur.executed[1][1] == (date(2026, 6, 1), date(2026, 6, 1), 10)


def test_adjust_a_stock_daily_pnl_uses_cost_for_same_day_opened_position():
    cur = FakeCursor(
        [
            {"exists": False},
            [
                {
                    "account_code": "tonghuashun",
                    "normalized_symbol": "601138",
                    "as_of": date(2026, 6, 2),
                    "buy_qty": Decimal("400"),
                    "buy_value": Decimal("31200"),
                }
            ]
        ]
    )
    rows = [
        _current_holding_row(
            symbol="601138",
            name="工业富联",
            quantity=Decimal("400"),
            current_price=Decimal("80.01"),
            previous_price=Decimal("73.75"),
            market_value=Decimal("32004"),
            market_value_cny=Decimal("32004"),
            holding_pnl=Decimal("794.01"),
            holding_pnl_cny=Decimal("794.01"),
            daily_pnl=Decimal("2504"),
            daily_pnl_cny=Decimal("2504"),
            as_of=date(2026, 6, 2),
        )
    ]

    adjusted = _adjust_a_stock_daily_pnl_for_same_day_buys(cur, rows)

    assert adjusted[0]["daily_pnl"] == Decimal("804.00")
    assert adjusted[0]["daily_pnl_cny"] == Decimal("804.00")
    assert adjusted[0]["daily_total_pnl_cny"] == Decimal("804.00")


def test_adjust_a_stock_daily_pnl_uses_trade_fact_execution_amount_when_available():
    cur = FakeCursor(
        [
            {"exists": True},
            {"has_rows": True},
            [
                {
                    "account_code": "tonghuashun",
                    "normalized_symbol": "603986",
                    "as_of": date(2026, 6, 4),
                    "buy_qty": Decimal("100"),
                    "buy_value": Decimal("51977"),
                }
            ],
        ]
    )
    rows = [
        _current_holding_row(
            symbol="603986",
            name="兆易创新",
            quantity=Decimal("100"),
            current_price=Decimal("519.77"),
            previous_price=Decimal("492.23"),
            market_value=Decimal("51977"),
            market_value_cny=Decimal("51977"),
            holding_pnl=Decimal("-25.99"),
            holding_pnl_cny=Decimal("-25.99"),
            daily_pnl=Decimal("2754"),
            daily_pnl_cny=Decimal("2754"),
            as_of=date(2026, 6, 4),
        )
    ]

    adjusted = _adjust_a_stock_daily_pnl_for_same_day_buys(cur, rows)

    assert adjusted[0]["daily_pnl"] == Decimal("0")
    assert adjusted[0]["daily_pnl_cny"] == Decimal("0")
    assert adjusted[0]["daily_total_pnl_cny"] == Decimal("0")


def test_enrich_realized_trade_pnl_merges_partial_sell_into_open_holding():
    cur = FakeCursor(
        [
            {"exists": True},
            [
                {
                    "account_code": "tonghuashun",
                    "symbol": "600487",
                    "trade_date": date(2026, 6, 4),
                    "realized_pnl": Decimal("7831.455"),
                    "realized_pnl_cny": Decimal("7831.455"),
                }
            ],
        ]
    )
    rows = [
        _current_holding_row(
            symbol="600487",
            name="Hengtong",
            quantity=Decimal("300"),
            current_price=Decimal("97.87"),
            previous_price=Decimal("91.43"),
            market_value=Decimal("29361"),
            market_value_cny=Decimal("29361"),
            holding_pnl=Decimal("7914.765"),
            holding_pnl_cny=Decimal("7914.765"),
            daily_pnl=Decimal("1932"),
            daily_pnl_cny=Decimal("1932"),
            as_of=date(2026, 6, 4),
        )
    ]

    enriched = _enrich_realized_trade_pnl(cur, rows)

    assert enriched[0]["holding_pnl_cny"] == Decimal("15746.220")
    assert enriched[0]["realized_trade_pnl_cny"] == Decimal("7831.455")
    assert enriched[0]["daily_pnl_cny"] == Decimal("1932")
    assert enriched[0]["daily_total_pnl_cny"] == Decimal("1932")
    assert enriched[0]["daily_price_pnl_cny"] == Decimal("1932")
    assert enriched[0]["daily_realized_trade_pnl_cny"] == Decimal("7831.455")
    assert enriched[0]["daily_realized_trade_pnl_only"] is True


def test_enrich_realized_trade_pnl_normalizes_net_invested_cost_snapshot():
    cur = FakeCursor(
        [
            {"exists": True},
            [],
            [
                {
                    "trade_id": 1,
                    "account_code": "tonghuashun",
                    "symbol": "601899",
                    "trade_date": date(2026, 5, 1),
                    "trade_type": "buy",
                    "side": "buy",
                    "quantity": Decimal("100"),
                    "gross_amount": Decimal("1000"),
                    "fee_amount": Decimal("0"),
                    "tax_amount": Decimal("0"),
                    "net_cash": Decimal("-1000"),
                    "fx_rate_to_cny": Decimal("1"),
                },
                {
                    "trade_id": 2,
                    "account_code": "tonghuashun",
                    "symbol": "601899",
                    "trade_date": date(2026, 5, 10),
                    "trade_type": "sell",
                    "side": "sell",
                    "quantity": Decimal("40"),
                    "gross_amount": Decimal("480"),
                    "fee_amount": Decimal("0"),
                    "tax_amount": Decimal("0"),
                    "net_cash": Decimal("480"),
                    "fx_rate_to_cny": Decimal("1"),
                },
            ],
        ]
    )
    rows = [
        _current_holding_row(
            symbol="601899",
            name="Zijin",
            quantity=Decimal("60"),
            current_price=Decimal("15"),
            market_value=Decimal("900"),
            market_value_cny=Decimal("900"),
            cost=Decimal("520"),
            cost_cny=Decimal("520"),
            holding_pnl=Decimal("380"),
            holding_pnl_cny=Decimal("380"),
            buy_price=Decimal("8.666667"),
            as_of=date(2026, 6, 12),
        )
    ]

    enriched = _enrich_realized_trade_pnl(cur, rows)

    assert enriched[0]["cost_basis_mode"] == "net_invested_snapshot"
    assert enriched[0]["cost_cny"] == Decimal("600")
    assert enriched[0]["buy_price"] == Decimal("10")
    assert enriched[0]["holding_pnl_before_realized_trade_cny"] == Decimal("300")
    assert enriched[0]["realized_trade_pnl_cny"] == Decimal("80")
    assert enriched[0]["holding_pnl_cny"] == Decimal("380")


def test_enrich_realized_trade_pnl_replays_when_some_sells_missing_realized():
    cur = FakeCursor(
        [
            {"exists": True},
            [
                {
                    "account_code": "tonghuashun",
                    "symbol": "601899",
                    "trade_date": date(2026, 5, 10),
                    "realized_pnl": Decimal("80"),
                    "realized_pnl_cny": Decimal("80"),
                    "missing_realized_count": Decimal("0"),
                },
                {
                    "account_code": "tonghuashun",
                    "symbol": "601899",
                    "trade_date": date(2026, 5, 11),
                    "realized_pnl": Decimal("0"),
                    "realized_pnl_cny": Decimal("0"),
                    "missing_realized_count": Decimal("1"),
                },
            ],
            [
                {
                    "trade_id": 1,
                    "account_code": "tonghuashun",
                    "symbol": "601899",
                    "trade_date": date(2026, 5, 1),
                    "trade_type": "buy",
                    "side": "buy",
                    "quantity": Decimal("200"),
                    "gross_amount": Decimal("2000"),
                    "fee_amount": Decimal("0"),
                    "tax_amount": Decimal("0"),
                    "net_cash": Decimal("-2000"),
                    "fx_rate_to_cny": Decimal("1"),
                },
                {
                    "trade_id": 2,
                    "account_code": "tonghuashun",
                    "symbol": "601899",
                    "trade_date": date(2026, 5, 10),
                    "trade_type": "sell",
                    "side": "sell",
                    "quantity": Decimal("40"),
                    "gross_amount": Decimal("480"),
                    "fee_amount": Decimal("0"),
                    "tax_amount": Decimal("0"),
                    "net_cash": Decimal("480"),
                    "fx_rate_to_cny": Decimal("1"),
                },
                {
                    "trade_id": 3,
                    "account_code": "tonghuashun",
                    "symbol": "601899",
                    "trade_date": date(2026, 5, 11),
                    "trade_type": "sell",
                    "side": "sell",
                    "quantity": Decimal("20"),
                    "gross_amount": Decimal("260"),
                    "fee_amount": Decimal("0"),
                    "tax_amount": Decimal("0"),
                    "net_cash": Decimal("260"),
                    "fx_rate_to_cny": Decimal("1"),
                },
            ],
        ]
    )
    rows = [
        _current_holding_row(
            symbol="601899",
            name="Zijin",
            quantity=Decimal("140"),
            current_price=Decimal("11"),
            market_value=Decimal("1540"),
            market_value_cny=Decimal("1540"),
            cost=Decimal("1400"),
            cost_cny=Decimal("1400"),
            holding_pnl=Decimal("140"),
            holding_pnl_cny=Decimal("140"),
            as_of=date(2026, 6, 12),
        )
    ]

    enriched = _enrich_realized_trade_pnl(cur, rows)

    assert enriched[0]["realized_trade_pnl_source"] == "weighted_average_trade_replay"
    assert enriched[0]["realized_trade_pnl_cny"] == Decimal("140")
    assert enriched[0]["holding_pnl_cny"] == Decimal("280")


def test_tradingview_closed_position_rows_include_same_day_realized_pnl():
    cur = FakeCursor(
        [
            [
                {
                    "account": "tonghuashun",
                    "type": "A股",
                    "symbol": "601138",
                    "name": "工业富联",
                    "currency": "CNY",
                    "market_value": Decimal("0"),
                    "market_value_native": Decimal("0"),
                    "market_value_cny": Decimal("0"),
                    "cost": Decimal("31215.60"),
                    "cost_cny": Decimal("31215.60"),
                    "holding_pnl": Decimal("1000.15"),
                    "holding_pnl_native": Decimal("1000.15"),
                    "holding_pnl_cny": Decimal("1000.15"),
                    "daily_pnl": Decimal("211.75"),
                    "daily_pnl_cny": Decimal("211.75"),
                    "major_category": "A股",
                    "minor_category": "A股",
                    "as_of": date(2026, 6, 3),
                    "snapshot_as_of": date(2026, 6, 3),
                    "is_closed": True,
                }
            ]
        ]
    )

    rows = _tradingview_closed_position_rows(cur, daily_pnl_as_of=date(2026, 6, 3))

    assert rows[0]["symbol"] == "601138"
    assert rows[0]["holding_pnl_cny"] == Decimal("1000.15")
    assert rows[0]["daily_pnl_cny"] == Decimal("211.75")
