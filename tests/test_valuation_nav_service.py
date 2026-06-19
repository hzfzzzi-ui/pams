from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from services.valuation_nav import (  # noqa: E402
    _bank_backcast_series,
    _investment_snapshot_series,
    _statement_rows_by_account,
    _tonghuashun_series,
    build_nav_drawdown_payload,
    build_nav_series_payload,
    build_nav_weekly_payload,
    current_nav_bucket_from_parts,
)


class FakeCursor:
    def __init__(self, results):
        self.results = list(results)
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        result = self.results.pop(0)
        return result

    def fetchall(self):
        result = self.results.pop(0)
        return result


def test_build_nav_series_payload_preserves_legacy_shape_and_latest_append():
    cur = FakeCursor(
        [
            {"exists": True},
            {"as_of": date(2026, 5, 31)},
            [
                {"as_of": date(2026, 5, 20), "total_market_value": Decimal("100.50")},
                {"as_of": date(2026, 5, 10), "total_market_value": Decimal("90.25")},
            ],
            {"total_mv": Decimal("123.45")},
        ]
    )

    payload = build_nav_series_payload(cur, limit=2, base_currency="CNY")

    assert set(payload) == {"rows"}
    assert payload["rows"] == [
        {"asOfDate": "2026-05-10", "netWorth": 90.25},
        {"asOfDate": "2026-05-20", "netWorth": 100.5},
        {"asOfDate": "2026-05-31", "netWorth": 123.45},
    ]
    assert len(cur.executed) == 4


def test_build_nav_series_payload_falls_back_to_holdings_when_nav_table_missing():
    cur = FakeCursor(
        [
            {"exists": False},
            [
                {"as_of": date(2026, 5, 20), "total_market_value": Decimal("100.50")},
                {"as_of": date(2026, 5, 31), "total_market_value": Decimal("123.45")},
            ],
        ]
    )

    payload = build_nav_series_payload(cur, limit=2, base_currency="CNY")

    assert payload["rows"] == [
        {"asOfDate": "2026-05-20", "netWorth": 100.5},
        {"asOfDate": "2026-05-31", "netWorth": 123.45},
    ]


def test_build_nav_drawdown_payload_preserves_legacy_shape():
    cur = FakeCursor(
        [
            {"exists": True},
            [
                {
                    "as_of": date(2026, 5, 10),
                    "total_market_value": Decimal("100"),
                    "completeness": Decimal("1"),
                    "peak": Decimal("100"),
                    "drawdown": Decimal("0"),
                },
                {
                    "as_of": date(2026, 5, 20),
                    "total_market_value": Decimal("90"),
                    "completeness": Decimal("1"),
                    "peak": Decimal("100"),
                    "drawdown": Decimal("-0.1"),
                },
            ]
        ]
    )

    payload = build_nav_drawdown_payload(cur, limit=2)

    assert payload["max_drawdown"] == "-0.1"
    assert payload["rows"][0]["as_of"] == "2026-05-10"
    assert payload["rows"][1]["drawdown"] == "-0.1"


def test_build_nav_drawdown_payload_falls_back_to_holdings_when_nav_table_missing():
    cur = FakeCursor(
        [
            {"exists": False},
            [
                {"as_of": date(2026, 5, 10), "total_market_value": Decimal("100")},
                {"as_of": date(2026, 5, 20), "total_market_value": Decimal("90")},
            ],
        ]
    )

    payload = build_nav_drawdown_payload(cur, limit=2)

    assert payload["max_drawdown"] == "-0.1"
    assert payload["rows"][0]["peak"] == "100"
    assert payload["rows"][1]["drawdown"] == "-0.1"


def _owner_ratio_case_sql(alias: str = "h"):
    return "1", []


def _account_owner_ratio(account_code: str):
    return Decimal("1")


def test_investment_snapshot_series_uses_complete_account_snapshots_for_latest_date() -> None:
    dates = [date(2026, 6, 18), date(2026, 6, 19)]
    cur = FakeCursor([[]])

    values = _investment_snapshot_series(
        cur,
        account_codes=["ibkr"],
        dates=dates,
        base_currency="CNY",
        owner_ratio_case_sql=_owner_ratio_case_sql,
    )

    assert values == {dates[0]: Decimal("0"), dates[1]: Decimal("0")}
    sql, params = cur.executed[0]
    assert "complete_account_snapshot" in sql
    assert "JOIN complete_account_snapshot cas" in sql
    assert params[0] == ["2026-06-18", "2026-06-19"]
    assert params[1] == date(2026, 6, 19)
    assert params[2] == ["ibkr"]


def test_tonghuashun_series_uses_trade_fact_before_legacy_tradingview() -> None:
    dates = [date(2026, 1, 1), date(2026, 1, 2)]
    cur = FakeCursor(
        [
            {"fact_exists": True, "px_exists": True},
            [
                {"date": dates[0], "gross_value_cny": Decimal("100")},
                {"date": dates[1], "gross_value_cny": Decimal("110")},
            ],
        ]
    )

    values = _tonghuashun_series(
        cur,
        dates=dates,
        base_currency="CNY",
        account_owner_ratio=lambda account_code: Decimal("0.625"),
    )

    assert values == {
        dates[0]: Decimal("62.500"),
        dates[1]: Decimal("68.750"),
    }
    assert any("pams_investment_trade_fact" in sql for sql, _ in cur.executed)
    assert not any("pams_tradingview_transactions" in sql for sql, _ in cur.executed)


def test_build_nav_weekly_payload_preserves_legacy_shape_and_row_fields():
    cur = FakeCursor(
        [
            [],
            {"bank_rows_exists": True, "cashflow_fact_exists": True},
            [],
            {"fact_exists": False, "px_exists": False},
            {"tv_exists": False, "px_exists": False},
            {"exists": False},
        ]
    )
    overview_payload = {
        "accounts": [
            {
                "account_code": "ccb",
                "account_type": "funding",
                "include_in_nav": True,
                "primary_balance": {"amount": "100", "currency": "CNY"},
            }
        ]
    }

    payload = build_nav_weekly_payload(
        cur,
        start_date=date(2026, 5, 24),
        base_currency="CNY",
        overview_payload=overview_payload,
        owner_ratio_case_sql=_owner_ratio_case_sql,
        account_owner_ratio=_account_owner_ratio,
    )

    assert set(payload) == {"start", "end", "base_currency", "interval", "method", "anchor_date", "rows"}
    assert payload["start"] == "2026-05-24"
    assert payload["end"] == "2026-05-31"
    assert payload["method"] == "account_anchor_backcast"
    assert {"date", "asOfDate", "assets", "liabilities", "netWorth"} <= set(payload["rows"][0])
    assert payload["rows"][-1]["netWorth"] == "100"


def test_build_nav_weekly_payload_accepts_current_terminal_date():
    cur = FakeCursor(
        [
            [],
            {"bank_rows_exists": True, "cashflow_fact_exists": True},
            [],
            {"fact_exists": False, "px_exists": False},
            {"tv_exists": False, "px_exists": False},
            {"exists": False},
        ]
    )
    overview_payload = {
        "accounts": [
            {
                "account_code": "ccb",
                "account_type": "funding",
                "include_in_nav": True,
                "primary_balance": {"amount": "100", "currency": "CNY"},
            }
        ]
    }

    payload = build_nav_weekly_payload(
        cur,
        start_date=date(2026, 6, 10),
        base_currency="CNY",
        overview_payload=overview_payload,
        owner_ratio_case_sql=_owner_ratio_case_sql,
        account_owner_ratio=_account_owner_ratio,
        terminal_date=date(2026, 6, 16),
    )

    assert payload["start"] == "2026-06-10"
    assert payload["end"] == "2026-06-16"
    assert payload["anchor_date"] == "2026-06-16"
    assert payload["rows"][-1]["asOfDate"] == "2026-06-16"


def test_build_nav_weekly_payload_can_sample_daily_dates():
    cur = FakeCursor(
        [
            [],
            {"bank_rows_exists": True, "cashflow_fact_exists": True},
            [],
            {"fact_exists": False, "px_exists": False},
            {"tv_exists": False, "px_exists": False},
            {"exists": False},
        ]
    )

    payload = build_nav_weekly_payload(
        cur,
        start_date=date(2026, 6, 14),
        base_currency="CNY",
        overview_payload={
            "accounts": [
                {
                    "account_code": "ccb",
                    "account_type": "funding",
                    "include_in_nav": True,
                    "primary_balance": {"amount": "100", "currency": "CNY"},
                }
            ]
        },
        owner_ratio_case_sql=_owner_ratio_case_sql,
        account_owner_ratio=_account_owner_ratio,
        terminal_date=date(2026, 6, 16),
        interval="daily",
    )

    assert payload["interval"] == "daily"
    assert [row["asOfDate"] for row in payload["rows"]] == [
        "2026-06-14",
        "2026-06-15",
        "2026-06-16",
    ]


def test_build_nav_weekly_payload_can_override_terminal_nav_from_portfolio_state():
    cur = FakeCursor(
        [
            [],
            {"bank_rows_exists": True, "cashflow_fact_exists": True},
            [],
            {"fact_exists": False, "px_exists": False},
            {"tv_exists": False, "px_exists": False},
            {"exists": False},
        ]
    )

    payload = build_nav_weekly_payload(
        cur,
        start_date=date(2026, 6, 10),
        base_currency="CNY",
        overview_payload={
            "accounts": [
                {
                    "account_code": "ccb",
                    "account_type": "funding",
                    "include_in_nav": True,
                    "primary_balance": {"amount": "100", "currency": "CNY"},
                }
            ]
        },
        owner_ratio_case_sql=_owner_ratio_case_sql,
        account_owner_ratio=_account_owner_ratio,
        terminal_date=date(2026, 6, 16),
        terminal_nav_bucket={"assets": Decimal("250"), "liabilities": Decimal("40"), "net_worth": Decimal("210")},
    )

    assert payload["rows"][-1]["assets"] == "250"
    assert payload["rows"][-1]["liabilities"] == "40"
    assert payload["rows"][-1]["netWorth"] == "210"


def test_current_nav_bucket_from_parts_uses_positions_and_unrepresented_accounts():
    bucket = current_nav_bucket_from_parts(
        investment_positions=[
            {"account_code": "ibkr", "market_value_cny": Decimal("300")},
            {"account_code": "ibkr", "market_value_cny": Decimal("-120")},
            {"account_code": "tonghuashun", "market_value_cny": Decimal("240")},
            {"account_code": "tonghuashun", "market_value_cny": Decimal("6")},
        ],
        account_balances=[
            {"account_code": "ibkr", "amount_cny": Decimal("180"), "include_in_nav": True},
            {"account_code": "tonghuashun", "amount_cny": Decimal("246"), "include_in_nav": True},
            {"account_code": "ccb", "amount_cny": Decimal("80"), "include_in_nav": True},
        ],
    )

    assert bucket == {"assets": Decimal("626"), "liabilities": Decimal("120"), "net_worth": Decimal("506")}


def test_statement_rows_by_account_falls_back_to_cashflow_fact() -> None:
    cur = FakeCursor(
        [
            {"bank_rows_exists": False, "cashflow_fact_exists": True},
            [
                {
                    "id": 11,
                    "account_code": "ccb",
                    "transaction_date": date(2026, 5, 25),
                    "amount": Decimal("-10"),
                    "currency": "CNY",
                    "category": "daily",
                    "subcategory": None,
                    "summary": "测试",
                    "description": None,
                    "counterparty": None,
                }
            ],
        ]
    )

    rows = _statement_rows_by_account(
        cur,
        ["ccb"],
        date(2026, 5, 24),
        date(2026, 5, 31),
    )

    assert rows["ccb"][0]["amount"] == Decimal("-10")
    assert "pams_cashflow_fact" in cur.executed[1][0]
    assert "affects_nav IS TRUE" in cur.executed[1][0]


def test_statement_rows_by_account_tolerates_missing_legacy_and_fact_tables() -> None:
    cur = FakeCursor([{"bank_rows_exists": False, "cashflow_fact_exists": False}])

    rows = _statement_rows_by_account(
        cur,
        ["ccb"],
        date(2026, 5, 24),
        date(2026, 5, 31),
    )

    assert rows == {}
    assert len(cur.executed) == 1


def test_bank_backcast_prefers_statement_running_balance_for_opening_nav() -> None:
    rows = {
        "ccb_7332": [
            {
                "id": 1,
                "account_code": "ccb_7332",
                "transaction_date": date(2026, 1, 4),
                "amount": Decimal("9325.16"),
                "currency": "CNY",
                "statement_balance": "113356.98",
            },
            {
                "id": 2,
                "account_code": "ccb_7332",
                "transaction_date": date(2026, 5, 18),
                "amount": Decimal("126658.49"),
                "currency": "CNY",
                "statement_balance": "150405.51",
            },
        ]
    }

    values = _bank_backcast_series(
        accounts=[
            {
                "account_code": "ccb_7332",
                "account_type": "funding",
                "include_in_nav": True,
                "base_currency": "CNY",
                "primary_balance": {"amount": "84756.92", "currency": "CNY", "as_of": "2026-06-01"},
            }
        ],
        rows_by_account=rows,
        dates=[date(2026, 1, 1), date(2026, 1, 8), date(2026, 6, 16)],
        terminal_date=date(2026, 6, 16),
        fx_timelines={"CNY": [(date(2026, 1, 1), Decimal("1"))]},
        base_currency="CNY",
    )

    assert values[date(2026, 1, 1)]["net_worth"] == Decimal("104031.82")
    assert values[date(2026, 1, 8)]["net_worth"] == Decimal("113356.98")
    assert values[date(2026, 6, 16)]["net_worth"] == Decimal("84756.92")


def test_build_nav_weekly_payload_clamps_start_after_anchor():
    cur = FakeCursor(
        [
            [],
            {"fact_exists": False, "px_exists": False},
            {"tv_exists": False, "px_exists": False},
            {"exists": False},
        ]
    )

    payload = build_nav_weekly_payload(
        cur,
        start_date=date(2026, 6, 10),
        base_currency="CNY",
        overview_payload={"accounts": []},
        owner_ratio_case_sql=_owner_ratio_case_sql,
        account_owner_ratio=_account_owner_ratio,
    )

    assert payload["start"] == "2026-05-31"
    assert len(payload["rows"]) == 1
