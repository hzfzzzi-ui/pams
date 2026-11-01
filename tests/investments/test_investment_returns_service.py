from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

import domain.investments.investment_returns as investment_returns  # noqa: E402
import domain.investments.investment_return_ibkr as investment_return_ibkr  # noqa: E402
import domain.investments.investment_return_data as investment_return_data  # noqa: E402
from domain.investments.investment_return_math import ibkr_return_effective_date  # noqa: E402
from domain.investments.investment_returns import (  # noqa: E402
    annualized_rate,
    apply_reported_display_anchor,
    build_account_performance_payload,
    build_investment_return_series_payload,
    build_section_return_series_payload,
    derived_cumulative_anchor,
    row_index_on_or_after,
    section_return_summary,
)


@pytest.fixture(autouse=True)
def proven_account_scope(monkeypatch):
    from domain.accounts import account_balance
    monkeypatch.setattr(account_balance, 'load_account_snapshot_coverage',
        lambda cur, pairs: {pair: {'status':'complete'} for pair in pairs})
    monkeypatch.setattr(account_balance, '_cash_source_schema_available', lambda cur: True)


class FakeCursor:
    def __init__(self, results):
        self.results = list(results)
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self.results.pop(0)

    def fetchall(self):
        return self.results.pop(0)


def test_reported_anchor_helpers_are_stable():
    assert annualized_rate(Decimal("0.10"), date(2026, 1, 1), date(2026, 1, 31)) > Decimal("2")
    assert derived_cumulative_anchor(Decimal("0.20"), Decimal("0.10")).quantize(Decimal("0.0001")) == Decimal("0.0909")
    rows = [{"date": date(2026, 1, 1)}, {"date": date(2026, 2, 1)}]
    assert row_index_on_or_after(rows, date(2026, 1, 15)) == 1
    assert row_index_on_or_after(rows, date(2026, 3, 1)) == 1


def test_apply_reported_display_anchor_sets_terminal_official_rate():
    rows = [
        {"date": date(2026, 1, 1), "display_return_rate": Decimal("0")},
        {"date": date(2026, 2, 1), "display_return_rate": Decimal("0.02")},
    ]

    apply_reported_display_anchor(rows, {"total_return_rate": Decimal("0.10")})

    assert rows[0]["display_return_rate_raw"] == Decimal("0")
    assert rows[-1]["display_return_rate"] == Decimal("0.10")


def test_section_return_summary_uses_reported_owner_ratio():
    rows = [
        {
            "date": date(2026, 1, 1),
            "market_value": Decimal("100"),
            "cumulative_capital": Decimal("100"),
            "display_return_rate": Decimal("0"),
        },
        {
            "date": date(2026, 5, 31),
            "market_value": Decimal("130"),
            "cumulative_capital": Decimal("100"),
            "display_return_rate": Decimal("0.30"),
        },
    ]

    summary = section_return_summary(
        rows=rows,
        reported={"account_code": "joint", "total_return_gross": Decimal("80"), "total_return_rate": Decimal("0.20")},
        owner_ratio_lookup=lambda account_code: Decimal("0.625"),
    )

    assert summary["total_return"] == Decimal("50.000")
    assert summary["total_return_rate"] == Decimal("0.20")
    assert summary["annualized_return_rate"] > Decimal("0")


def test_account_net_liquidation_anchor_uses_rolled_account_performance(monkeypatch: pytest.MonkeyPatch):
    observed = {}

    def fake_account_performance(cur, **kwargs):
        observed.update(kwargs)
        return {"net_liquidation_cny": "247497.05625", "as_of": "2026-06-18"}

    monkeypatch.setattr(investment_returns, "build_account_performance_payload", fake_account_performance)

    anchor = investment_returns._account_net_liquidation_anchor_cny(
        FakeCursor([]),
        account_code="tonghuashun",
        overview_payload={
            "accounts": [
                {
                    "account_code": "tonghuashun",
                    "include_in_nav": True,
                    "primary_balance": {
                        "amount": "229783.525",
                        "gross_amount": "367653.64",
                        "currency": "CNY",
                        "as_of": "2026-06-09",
                    },
                }
            ]
        },
        account_owner_ratio=lambda account_code: Decimal("0.625"),
        fx_timelines={"CNY": [(date(2026, 6, 18), Decimal("1"))]},
        as_of=date(2026, 6, 18),
        base_currency="CNY",
    )

    assert anchor == Decimal("247497.05625")
    assert observed["account_code"] == "tonghuashun"
    assert observed["net_liquidation_gross"] == Decimal("367653.64")
    assert observed["net_liquidation_as_of"] == "2026-06-09"


def test_foreign_account_anchor_rejects_missing_or_future_fx_and_stale_balance():
    day = date(2026, 6, 18)
    account = {"account_code": "tonghuashun", "primary_balance": {
        "amount": "100", "currency": "USD", "as_of": day.isoformat()}}
    kw = dict(account_code="tonghuashun", overview_payload={"accounts": [account]},
        account_owner_ratio=lambda _: Decimal(1), as_of=day, base_currency="CNY")
    assert investment_returns._account_net_liquidation_anchor_cny(
        None, fx_timelines={"USD": [(date(2026, 6, 19), Decimal(8))]}, **kw) is None
    assert investment_returns._account_net_liquidation_anchor_cny(
        None, fx_timelines={"USD": [(day, Decimal(7))]}, **kw) == Decimal(700)
    account["primary_balance"]["as_of"] = "2026-06-17"
    assert investment_returns._account_net_liquidation_anchor_cny(
        None, fx_timelines={"USD": [(day, Decimal(7))]}, **kw) is None


def test_build_account_performance_payload_preserves_legacy_shape():
    cur = FakeCursor(
        [
            {"as_of": date(2026, 5, 31)},
            [
                {
                    "account_code": "tonghuashun",
                    "as_of": date(2026, 5, 31),
                    "market_value_cny": Decimal("380000"),
                    "holding_pnl_cny": Decimal("-18000"),
                }
            ],
        ]
    )

    payload = build_account_performance_payload(
        cur,
        account_code="tonghuashun",
        owner_ratio=Decimal("0.625"),
        initial_capital=Decimal("400000"),
    )

    assert payload is not None
    assert payload["status"] == "ok"
    assert payload["account_code"] == "tonghuashun"
    assert payload["as_of"] == "2026-05-31"
    assert payload["net_liquidation_cny"] == "237500.000"
    assert payload["initial_capital_cny"] == "250000.000"
    assert payload["total_pnl_cny"] is None
    assert payload["external_flow_coverage"] == "unknown"
    assert payload["rows_count"] == 1


def test_build_account_performance_payload_uses_account_net_liquidation_when_available():
    cur = FakeCursor(
        [
            {"as_of": date(2026, 6, 4)},
            [
                {
                    "account_code": "tonghuashun",
                    "as_of": date(2026, 6, 4),
                    "market_value_cny": Decimal("331903"),
                    "holding_pnl_cny": Decimal("-27312.255"),
                }
            ],
        ]
    )

    payload = build_account_performance_payload(
        cur,
        account_code="tonghuashun",
        owner_ratio=Decimal("0.625"),
        initial_capital=Decimal("400000"),
        net_liquidation_gross=Decimal("385539.56"),
        net_liquidation=Decimal("240962.225"),
    )

    assert payload is not None
    assert payload["net_liquidation_gross_cny"] == "385539.56"
    assert payload["net_liquidation_cny"] == "240962.225"
    assert payload["position_market_value_cny"] is None
    assert payload["cash_or_uninvested_cny"] is None
    assert payload["unexplained_anchor_difference_cny"] == "33522.850"
    assert payload["total_pnl_cny"] is None


def test_reported_account_performance_overlay_adds_cash_and_updates_return_ratio(monkeypatch: pytest.MonkeyPatch):
    import main as api_main

    monkeypatch.setattr(
        api_main,
        "_reported_returns_cfg",
        lambda: {
            "account_performance": {
                "tonghuashun": {
                    "use_reported_anchor": True,
                    "as_of": "2026-06-19",
                    "total_return_gross": "-3143.33",
                    "cash_balance_gross": "8471.36",
                    "basis": "user confirmed",
                }
            }
        },
    )
    monkeypatch.setattr(api_main, "_account_owner_ratio", lambda account_code: Decimal("0.625"))

    payload = {
        "account_code": "tonghuashun",
        "initial_capital_cny": "250000.000",
        "net_liquidation_gross_cny": "387560.0000",
        "net_liquidation_cny": "242225.0000000",
        "net_liquidation_source": "current_snapshot",
        "position_market_value_gross_cny": "387560.0000",
        "position_market_value_cny": "242225.0000000",
        "cash_or_uninvested_cny": "0",
        "holding_pnl_cny": "-2362.6830625",
        "total_pnl_cny": "-7775.0000000",
        "return_ratio": "-0.0311",
    }

    out = api_main._apply_reported_account_performance(payload, "tonghuashun")

    for key, value in payload.items():
        assert out[key] == value
    assert out["reported_performance"]["status"] == "unverified_configuration"
    missing = {**payload, "net_liquidation_gross_cny": None, "net_liquidation_cny": None,
               "total_pnl_cny": None, "return_ratio": None}
    out_missing = api_main._apply_reported_account_performance(missing, "tonghuashun")
    assert all(out_missing[key] is None for key in ("net_liquidation_gross_cny", "net_liquidation_cny",
                                                     "total_pnl_cny", "return_ratio"))


def test_holdings_category_today_window_uses_holdings_daily_total(monkeypatch: pytest.MonkeyPatch):
    cur = FakeCursor(
        [
            [{"account_code": "tencent_licaitong"}],
            {"baseline_date": date(2026, 6, 15)},
        ]
    )

    def fake_holdings_payload(*args, **kwargs):
        assert kwargs["category"] == "A股"
        assert kwargs["minor_category"] == "A股基金"
        assert kwargs["owner_ratios"] == {"tencent_licaitong": Decimal("1")}
        return {
            "rows": [
                {
                    "as_of": date(2026, 6, 16),
                    "market_value_cny": Decimal("1000"),
                    "daily_total_pnl_cny": Decimal("12.34"),
                    "is_closed": False,
                },
                {
                    "as_of": date(2026, 6, 16),
                    "market_value_cny": Decimal("2000"),
                    "daily_pnl_cny": Decimal("-2.00"),
                    "is_closed": False,
                },
            ]
        }

    monkeypatch.setattr(investment_returns, "build_holdings_by_category_payload", fake_holdings_payload)

    payload = investment_returns._holdings_category_today_window_payload(
        cur,
        category="A股",
        minor_category="A股基金",
        view_as_of=date(2026, 6, 16),
        excluded_holding_accounts=set(),
        account_owner_ratio=lambda account: Decimal("1"),
        label_date=date(2026, 6, 16),
    )

    assert payload["baseline_date"] == "2026-06-15"
    assert payload["latest_date"] == "2026-06-16"
    assert payload["total_return"] == "10.34"


def test_a_stock_today_window_prefers_account_net_liquidation_delta(monkeypatch: pytest.MonkeyPatch):
    def fake_account_performance(cur, **kwargs):
        assert kwargs["account_code"] == "tonghuashun"
        assert kwargs["daily_pnl_label_date"] == date(2026, 6, 18)
        return {
            "as_of": "2026-06-18",
            "net_liquidation_cny": "247497.05625",
            "daily_pnl_cny": "-134.9",
        }

    monkeypatch.setattr(investment_returns, "build_account_performance_payload", fake_account_performance)

    payload = investment_returns._a_stock_holdings_today_window_payload(
        FakeCursor([{"baseline_date": date(2026, 6, 17)}]),
        view_as_of=date(2026, 6, 18),
        excluded_holding_accounts=set(),
        account_owner_ratio=lambda account_code: Decimal("0.625"),
        label_date=date(2026, 6, 18),
        overview_payload={
            "accounts": [
                {
                    "account_code": "tonghuashun",
                    "primary_balance": {
                        "amount": "229783.525",
                        "gross_amount": "367653.64",
                        "currency": "CNY",
                        "as_of": "2026-06-09",
                    },
                }
            ]
        },
    )

    assert payload is not None
    assert payload["label_date"] == "2026-06-18"
    assert payload["baseline_date"] == "2026-06-17"
    assert payload["total_return"] == "-134.9"
    assert Decimal(payload["return_rate"]).quantize(Decimal("0.000001")) == Decimal("-0.000545")


def test_build_account_performance_rolls_forward_stale_manual_net_liquidation():
    cur = FakeCursor(
        [
            {"as_of": date(2026, 6, 15)},
            [
                {
                    "account_code": "tonghuashun",
                    "as_of": date(2026, 6, 15),
                    "market_value_cny": Decimal("361549"),
                    "holding_pnl_cny": Decimal("-16693.0525"),
                }
            ],
            {"market_value": Decimal("366290"), "snapshot_as_of": date(2026, 6, 9)},
            {"cash_delta": Decimal("30749.22")},
            {"previous_as_of": date(2026, 6, 14)},
            {"market_value": Decimal("369308")},
            {"cash_delta": Decimal("0"), "fact_ids": []},
        ]
    )

    payload = build_account_performance_payload(
        cur,
        account_code="tonghuashun",
        owner_ratio=Decimal("0.625"),
        initial_capital=Decimal("400000"),
        net_liquidation_gross=Decimal("367653.64"),
        net_liquidation=Decimal("229783.525"),
        net_liquidation_as_of=date(2026, 6, 9),
    )

    assert payload is not None
    assert payload["net_liquidation_source"] == "rolled_forward_manual_balance"
    assert payload["net_liquidation_gross_cny"] == "362912.64"
    assert payload["net_liquidation_cny"] == "226820.40000"
    assert payload["cash_or_uninvested_cny"] is None
    assert payload["unexplained_anchor_difference_cny"] == "852.27500"
    assert payload["total_pnl_cny"] is None
    assert payload["daily_pnl_cny"] is None
    assert payload["daily_pnl_source"] is None
    assert payload["external_flow_coverage"] == "unknown"
    assert payload["net_liquidation_rollforward"]["cash_delta_gross_cny"] == "30749.22"


def test_build_account_performance_masks_stale_daily_pnl_window():
    cur = FakeCursor(
        [
            {"as_of": date(2026, 6, 15)},
            [
                {
                    "account_code": "tonghuashun",
                    "as_of": date(2026, 6, 15),
                    "market_value_cny": Decimal("361549"),
                    "holding_pnl_cny": Decimal("-16693.0525"),
                }
            ],
            {"market_value": Decimal("366290"), "snapshot_as_of": date(2026, 6, 9)},
            {"cash_delta": Decimal("30749.22")},
            {"previous_as_of": date(2026, 6, 14)},
            {"market_value": Decimal("369308")},
            {"cash_delta": Decimal("0"), "fact_ids": []},
        ]
    )

    payload = build_account_performance_payload(
        cur,
        account_code="tonghuashun",
        owner_ratio=Decimal("0.625"),
        initial_capital=Decimal("400000"),
        net_liquidation_gross=Decimal("367653.64"),
        net_liquidation=Decimal("229783.525"),
        net_liquidation_as_of=date(2026, 6, 9),
        daily_pnl_label_date=date(2026, 6, 16),
    )

    assert payload is not None
    assert payload["daily_pnl_label_date"] == "2026-06-16"
    assert payload["daily_pnl_effective"] is False
    assert payload["daily_pnl_cny"] is None
    assert payload["raw_daily_pnl_cny"] is None


def _owner_ratio_case_sql(alias: str = "h"):
    return "1", []


def _account_owner_ratio(account_code: str):
    return Decimal("0.5") if account_code == "tonghuashun" else Decimal("1")


def test_ibkr_return_effective_date_aligns_usd_to_beijing_bucket():
    assert ibkr_return_effective_date(date(2026, 6, 17), "USD") == date(2026, 6, 18)
    assert ibkr_return_effective_date(date(2026, 6, 17), "HKD") == date(2026, 6, 17)
    assert ibkr_return_effective_date(date(2026, 6, 17), "KRW") == date(2026, 6, 17)


def test_ibkr_trade_flow_events_uses_effective_date_for_usd_trades():
    cur = FakeCursor(
        [
            [
                {
                    "trade_date": date(2026, 6, 17),
                    "quantity": Decimal("1"),
                    "gross_amount": Decimal("100"),
                    "commission": Decimal("0"),
                    "net_cash": Decimal("-100"),
                    "currency": "USD",
                }
            ]
        ]
    )

    events = investment_return_data._ibkr_trade_flow_events(
        cur,
        start_date=date(2026, 6, 18),
        end_date=date(2026, 6, 18),
        base_currency="CNY",
        fx_timelines={"USD": [(date(2026, 6, 18), Decimal("7"))]},
    )

    markets, start_param, end_param = cur.executed[0][1]
    assert start_param == date(2026, 6, 17)
    assert end_param == date(2026, 6, 18)
    assert "\u97e9\u80a1" in markets
    assert "\u6b27\u80a1" in markets
    assert events == [(date(2026, 6, 18), Decimal("700"))]


def test_ibkr_trade_flow_events_keeps_non_usd_trades_on_trade_date():
    cur = FakeCursor(
        [
            [
                {
                    "trade_date": date(2026, 6, 17),
                    "quantity": Decimal("1"),
                    "gross_amount": Decimal("100"),
                    "commission": Decimal("0"),
                    "net_cash": Decimal("-100"),
                    "currency": "HKD",
                }
            ]
        ]
    )

    events = investment_return_data._ibkr_trade_flow_events(
        cur,
        start_date=date(2026, 6, 17),
        end_date=date(2026, 6, 17),
        base_currency="CNY",
        fx_timelines={"HKD": [(date(2026, 6, 17), Decimal("0.9"))]},
    )

    assert events == [(date(2026, 6, 17), Decimal("90.0"))]


def test_ibkr_trade_flow_events_includes_korea_and_europe_markets():
    cur = FakeCursor(
        [
            [
                {
                    "trade_date": date(2026, 5, 11),
                    "quantity": Decimal("10"),
                    "gross_amount": Decimal("18760000"),
                    "commission": Decimal("11256"),
                    "net_cash": Decimal("-18771256"),
                    "currency": "KRW",
                },
                {
                    "trade_date": date(2026, 6, 18),
                    "quantity": Decimal("1"),
                    "gross_amount": Decimal("1000"),
                    "commission": Decimal("2"),
                    "net_cash": Decimal("-1002"),
                    "currency": "SEK",
                },
            ]
        ]
    )

    events = investment_return_data._ibkr_trade_flow_events(
        cur,
        start_date=date(2026, 5, 11),
        end_date=date(2026, 6, 18),
        base_currency="CNY",
        fx_timelines={
            "KRW": [(date(2026, 5, 11), Decimal("0.00447"))],
            "SEK": [(date(2026, 6, 18), Decimal("0.74"))],
        },
    )

    markets = cur.executed[0][1][0]
    assert "\u97e9\u80a1" in markets
    assert "\u6b27\u80a1" in markets
    assert events == [
        (date(2026, 5, 11), Decimal("83907.51432")),
        (date(2026, 6, 18), Decimal("741.48")),
    ]


def test_snapshot_category_value_series_uses_complete_account_snapshots():
    dates = [date(2026, 6, 18), date(2026, 6, 19)]
    cur = FakeCursor([[]])

    values = investment_return_data._snapshot_category_value_series(
        cur,
        dates=dates,
        base_currency="CNY",
        major_category="美股",
        owner_ratio_case_sql=_owner_ratio_case_sql,
        excluded_holding_accounts=set(),
        account_codes=["ibkr"],
    )

    assert values == {dates[0]: Decimal("0"), dates[1]: Decimal("0")}
    sql, params = cur.executed[0]
    assert "complete_account_snapshot" in sql
    assert "JOIN complete_holding_snapshot hs ON hs.as_of <= d.date" in sql
    assert params[0] == ["2026-06-18", "2026-06-19"]
    assert params[1] == date(2026, 6, 19)


def test_legacy_ibkr_estimate_exposes_source_date_and_missing_values():
    payload = {"summary": {"total_return": "10"}, "today_window": {"amount": "10"}, "rows": [
        {"date": "2026-06-02", "market_value": "700", "return_rate": "0.1"},
        {"date": "2026-06-03", "market_value": "0", "return_rate": "0"}]}
    investment_returns._apply_ibkr_history_coverage(payload, [
        "ibkr_history_coverage_unverified", "ibkr_history_origins:2026-06-02=2026-06-01",
        "ibkr_history_missing:2026-06-03"])
    assert payload["status"] == "partial"
    assert payload["rows"][0]["market_value"] == "700"
    assert payload["rows"][0]["ibkr_history_evidence"]["source_as_of"] == "2026-06-01"
    assert payload["rows"][0]["ibkr_history_evidence"]["status"] == "estimated"
    assert payload["rows"][1]["market_value"] is None
    assert all(row["return_rate"] is None for row in payload["rows"])
    assert payload["summary"]["total_return"] is None and payload["today_window"] is None


def test_ibkr_return_without_history_is_unavailable_without_assuming_zero():
    day = date(2026, 6, 2)
    cur = FakeCursor([[]])
    values, costs, warnings = investment_return_ibkr._ibkr_full_trade_return_series(
        cur, dates=[day], base_currency="CNY", fx_timelines={})
    assert values == {} and costs == {}
    assert warnings == ["ibkr_position_history_unavailable"]
    assert len(cur.executed) == 1


def test_ibkr_return_ignores_obsolete_position_history():
    cur = FakeCursor([[]])
    values, costs, warnings = investment_return_ibkr._ibkr_full_trade_return_series(
        cur, dates=[date(2026, 6, 2)], base_currency="CNY", fx_timelines={})
    assert values == {} and costs == {} and warnings == ["ibkr_position_history_unavailable"]
    assert all('pams_ibkr_position_history' not in sql for sql, _ in cur.executed)


def test_ibkr_full_trade_return_series_delays_usd_trade_to_beijing_bucket(monkeypatch: pytest.MonkeyPatch):
    dates = [date(2026, 6, 17), date(2026, 6, 18)]
    cur = FakeCursor(
        [
            [{"symbol": "QQQ", "currency": "USD"}],
            [
                {
                    "trade_id": 1, "source_table": "ibkr_flex_trade",
                    "trade_date": date(2026, 6, 17),
                    "symbol": "QQQ",
                    "currency": "USD",
                    "quantity": Decimal("1"),
                    "gross_amount": Decimal("100"),
                    "commission": Decimal("0"),
                    "net_cash": Decimal("-100"),
                }
            ],
        ]
    )

    from domain.brokers.ibkr import ibkr_fact_source_verification
    monkeypatch.setattr(ibkr_fact_source_verification, 'verify_ibkr_fact', lambda *a, **k: {'status':'consistent','unknown_fields':[]})
    monkeypatch.setattr(
        investment_return_ibkr,
        "_fetch_yfinance_closes",
        lambda *args, **kwargs: (
            {"QQQ": {dates[0]: Decimal("110"), dates[1]: Decimal("120")}},
            [],
        ),
    )

    values, costs, warnings = investment_return_ibkr._ibkr_full_trade_return_series(
        cur,
        dates=dates,
        base_currency="CNY",
        fx_timelines={"USD": [(dates[1], Decimal("7"))]},
    )

    assert values[dates[0]] == Decimal("0")
    assert costs[dates[0]] == Decimal("0")
    assert values[dates[1]] == Decimal("840")
    assert costs[dates[1]] == Decimal("700")
    assert "ibkr_history_coverage_unverified" in warnings
    assert "ibkr_history_missing:2026-06-17" in warnings


def test_ibkr_unknown_trade_tax_marks_reconstructed_value_unavailable(monkeypatch: pytest.MonkeyPatch):
    day = date(2026, 6, 18)
    cur = FakeCursor([[{"symbol": "QQQ", "currency": "USD"}], [{"trade_id":1,"source_table":"ibkr_flex_trade",
        "trade_date": date(2026, 6, 17), "symbol": "QQQ", "currency": "USD",
        "quantity": Decimal("1"), "gross_amount": Decimal("100"), "commission": Decimal("0"),
        "net_cash": Decimal("-100"), "tax_known": False}]])
    from domain.brokers.ibkr import ibkr_fact_source_verification
    monkeypatch.setattr(ibkr_fact_source_verification, 'verify_ibkr_fact', lambda *a, **k: {'status':'consistent','unknown_fields':[]})
    monkeypatch.setattr(investment_return_ibkr, "_fetch_yfinance_closes",
        lambda *args, **kwargs: ({"QQQ": {day: Decimal("120")}}, []))
    _, _, warnings = investment_return_ibkr._ibkr_full_trade_return_series(
        cur, dates=[day], base_currency="CNY", fx_timelines={"USD": [(day, Decimal("7"))]})
    assert "ibkr_history_missing:2026-06-18" in warnings


def test_ibkr_full_trade_return_series_includes_korea_market(monkeypatch: pytest.MonkeyPatch):
    dates = [date(2026, 5, 10), date(2026, 5, 11)]
    cur = FakeCursor(
        [
            [{"symbol": "000660.KS", "currency": "KRW"}],
            [
                {
                    "trade_id": 1, "source_table": "ibkr_flex_trade",
                    "trade_date": date(2026, 5, 11),
                    "symbol": "000660.KS",
                    "currency": "KRW",
                    "quantity": Decimal("10"),
                    "gross_amount": Decimal("18760000"),
                    "commission": Decimal("11256"),
                    "net_cash": Decimal("-18771256"),
                }
            ],
        ]
    )

    from domain.brokers.ibkr import ibkr_fact_source_verification
    monkeypatch.setattr(ibkr_fact_source_verification, 'verify_ibkr_fact', lambda *a, **k: {'status':'consistent','unknown_fields':[]})
    monkeypatch.setattr(
        investment_return_ibkr,
        "_fetch_yfinance_closes",
        lambda *args, **kwargs: (
            {"000660.KS": {dates[1]: Decimal("200000")}},
            [],
        ),
    )

    values, costs, warnings = investment_return_ibkr._ibkr_full_trade_return_series(
        cur,
        dates=dates,
        base_currency="CNY",
        fx_timelines={"KRW": [(dates[1], Decimal("0.00447"))]},
    )

    symbol_query_markets = cur.executed[0][1][0]
    trade_query_markets = cur.executed[1][1][2]
    assert "\u97e9\u80a1" in symbol_query_markets
    assert "\u97e9\u80a1" in trade_query_markets
    assert values[dates[0]] == Decimal("0")
    assert values[dates[1]] == Decimal("8940.00000")
    assert costs[dates[1]] == Decimal("83907.51432")
    assert "ibkr_history_coverage_unverified" in warnings
    assert "ibkr_history_missing:2026-05-10" in warnings


def test_overlay_exact_snapshot_values_replaces_only_confirmed_dates(monkeypatch: pytest.MonkeyPatch):
    dates = [date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3)]

    values = investment_returns._overlay_exact_snapshot_values(
        FakeCursor([[{"as_of": dates[1], "value_cny": Decimal("125"), "row_count": 2}]]),
        {dates[0]: Decimal("100"), dates[1]: Decimal("110"), dates[2]: Decimal("120")},
        dates=dates,
        base_currency="CNY",
        owner_ratio_case_sql=_owner_ratio_case_sql,
        account_codes=["tonghuashun"],
    )

    assert values == {dates[0]: Decimal("100"), dates[1]: Decimal("125"), dates[2]: Decimal("120")}


def test_gold_proxy_adjustment_preserves_non_gold_precious_residual():
    sample_date = date(2026, 5, 27)
    cur = FakeCursor(
        [
            [
                {
                    "date": sample_date,
                    "snapshot_value_cny": Decimal("9852.10"),
                    "proxy_value_cny": Decimal("9738.51"),
                    "priced_count": 1,
                }
            ]
        ]
    )

    values = investment_return_data._apply_gold_proxy_to_precious_values(
        cur,
        {sample_date: Decimal("9902.10")},
        dates=[sample_date],
        base_currency="CNY",
        owner_ratio_case_sql=_owner_ratio_case_sql,
        excluded_holding_accounts=set(),
    )

    assert values[sample_date] == Decimal("9788.51")
    assert any("Au99.99" in str(item) for item in cur.executed[0][1])


def test_investment_transaction_flow_events_require_snapshot_coverage():
    cur = FakeCursor([[]])

    events = investment_return_data._investment_transaction_flow_events(
        cur,
        start_date=date(2026, 3, 1),
        end_date=date(2026, 3, 31),
        base_currency="CNY",
        fx_timelines={"CNY": [(date(2026, 3, 1), Decimal("1"))]},
        account_owner_ratio=lambda account_code: Decimal("1"),
        major_category="贵金属",
    )

    sql, params = cur.executed[0]
    assert events == []
    assert "pams_holdings_snapshot hs" in sql
    assert "INTERVAL '10 days'" in sql
    assert "hs.major_category = %s" in sql
    assert params[-2:] == ["资产", "贵金属"]


def test_overlay_terminal_exact_section_value_only_replaces_latest(monkeypatch: pytest.MonkeyPatch):
    dates = [date(2026, 6, 16), date(2026, 6, 17)]
    observed = {}

    def fake_exact_snapshot_value(cur, **kwargs):
        observed.update(kwargs)
        return Decimal("150")

    monkeypatch.setattr(investment_returns, "_exact_snapshot_category_value", fake_exact_snapshot_value)

    values = investment_returns._overlay_terminal_exact_section_value(
        FakeCursor([]),
        {dates[0]: Decimal("100"), dates[1]: Decimal("110")},
        dates=dates,
        section="a_stock",
        base_currency="CNY",
        owner_ratio_case_sql=_owner_ratio_case_sql,
        excluded_holding_accounts=set(),
        account_codes=["tonghuashun"],
    )

    assert values[dates[0]] == Decimal("100")
    assert values[dates[1]] == Decimal("150")
    assert observed["as_of"] == dates[-1]
    assert observed["account_codes"] == ["tonghuashun"]


def test_build_section_return_series_payload_ignores_a_stock_reported_anchor_by_default(monkeypatch: pytest.MonkeyPatch):
    dates = [date(2026, 1, 1), date(2026, 5, 31)]
    monkeypatch.setattr(investment_returns, "_section_first_activity_date", lambda cur, section: dates[0])
    monkeypatch.setattr(investment_returns, "series_dates", lambda start_date, end_date, interval: dates)
    monkeypatch.setattr(investment_returns, "load_fx_timelines", lambda cur, base_currency: {"CNY": [(dates[-1], Decimal("1"))]})
    monkeypatch.setattr(
        investment_returns,
        "tonghuashun_series",
        lambda cur, dates, base_currency, account_owner_ratio: {dates[0]: Decimal("100"), dates[1]: Decimal("110")},
    )
    monkeypatch.setattr(
        investment_returns,
        "_tonghuashun_external_flow_events",
        lambda cur, **kwargs: [(dates[0], Decimal("100"))],
    )

    def fail_sparse_snapshot_overlay(*args, **kwargs):
        raise AssertionError("a_stock return series must not use sparse Tonghuashun snapshots")

    monkeypatch.setattr(investment_returns, "_overlay_exact_snapshot_values", fail_sparse_snapshot_overlay)
    monkeypatch.setattr(investment_returns, "_overlay_terminal_exact_section_value", fail_sparse_snapshot_overlay)

    payload = build_section_return_series_payload(
        FakeCursor([]),
        section="a_stock",
        start_date=date(2026, 1, 1),
        interval="daily",
        base_currency="CNY",
        view_as_of=dates[-1],
        reported_returns_cfg={
            "as_of": "2026-05-31",
            "account_performance": {
                "tonghuashun": {
                    "account_code": "tonghuashun",
                    "total_return_gross": "20",
                    "total_return_rate": "0.20",
                    "annualized_return_rate": "0.30",
                    "basis": "reported anchor",
                }
            },
        },
        overview_payload={
            "accounts": [
                {
                    "account_code": "tonghuashun",
                    "include_in_nav": True,
                    "primary_balance": {"amount": "110", "currency": "CNY"},
                }
            ]
        },
        owner_ratio_case_sql=_owner_ratio_case_sql,
        account_owner_ratio=_account_owner_ratio,
        excluded_holding_accounts=set(),
    )

    assert set(payload) == {"section", "label", "start", "end", "base_currency", "interval", "method", "coverage_notes", "summary", "rows"}
    assert payload["section"] == "a_stock"
    assert Decimal(payload["summary"]["total_return"]) == Decimal("10")
    assert payload["summary"]["total_return_rate"] == "0.1"
    assert payload["rows"][-1]["display_return_rate"] == "0.1"
    assert payload["rows"][-1]["display_return_rate_raw"] is None


def test_build_section_return_series_payload_clamps_start_after_terminal(monkeypatch: pytest.MonkeyPatch):
    terminal = date(2026, 5, 31)
    monkeypatch.setattr(investment_returns, "_section_first_activity_date", lambda cur, section: None)
    monkeypatch.setattr(investment_returns, "load_fx_timelines", lambda cur, base_currency: {"CNY": [(terminal, Decimal("1"))]})
    monkeypatch.setattr(
        investment_returns,
        "_snapshot_category_value_series",
        lambda cur, **kwargs: {terminal: Decimal("100")},
    )
    monkeypatch.setattr(investment_returns, "_investment_transaction_flow_events", lambda cur, **kwargs: [])

    payload = build_section_return_series_payload(
        FakeCursor([]),
        section="a_fund",
        start_date=date(2026, 6, 10),
        interval="daily",
        base_currency="CNY",
        view_as_of=terminal,
        reported_returns_cfg={},
        overview_payload={"accounts": []},
        owner_ratio_case_sql=_owner_ratio_case_sql,
        account_owner_ratio=_account_owner_ratio,
        excluded_holding_accounts=set(),
    )

    assert payload["start"] == "2026-05-31"
    assert payload["end"] == "2026-05-31"
    assert len(payload["rows"]) == 1


def test_build_section_return_series_payload_compounds_stale_reported_total(monkeypatch: pytest.MonkeyPatch):
    dates = [date(2026, 6, 2), date(2026, 6, 3)]
    monkeypatch.setattr(investment_returns, "_section_first_activity_date", lambda cur, section: dates[0])
    monkeypatch.setattr(investment_returns, "series_dates", lambda start_date, end_date, interval: dates)
    monkeypatch.setattr(investment_returns, "load_fx_timelines", lambda cur, base_currency: {"CNY": [(dates[-1], Decimal("1"))]})
    monkeypatch.setattr(
        investment_returns,
        "_snapshot_category_value_series",
        lambda cur, **kwargs: {dates[0]: Decimal("100"), dates[1]: Decimal("110")},
    )
    monkeypatch.setattr(investment_returns, "_investment_transaction_flow_events", lambda cur, **kwargs: [])

    payload = build_section_return_series_payload(
        FakeCursor([]),
        section="a_fund",
        start_date=dates[0],
        interval="daily",
        base_currency="CNY",
        view_as_of=dates[-1],
        reported_returns_cfg={
            "as_of": "2026-06-02",
            "sections": {"a_share_fund": {"total_ytd_pnl": "25"}},
        },
        overview_payload={"accounts": []},
        owner_ratio_case_sql=_owner_ratio_case_sql,
        account_owner_ratio=_account_owner_ratio,
        excluded_holding_accounts=set(),
    )

    assert Decimal(payload["summary"]["total_return"]) == Decimal("35")
    assert Decimal(payload["summary"]["total_return_rate"]).quantize(Decimal("0.0001")) == Decimal("0.4667")


def test_build_section_return_series_payload_injects_us_stock_warning(monkeypatch: pytest.MonkeyPatch):
    sample_date = date(2026, 1, 1)
    monkeypatch.setattr(investment_returns, "_section_first_activity_date", lambda cur, section: sample_date)
    monkeypatch.setattr(investment_returns, "series_dates", lambda start_date, end_date, interval: [sample_date])
    monkeypatch.setattr(investment_returns, "load_fx_timelines", lambda cur, base_currency: {"USD": [(sample_date, Decimal("7"))]})
    monkeypatch.setattr(
        investment_returns,
        "_ibkr_full_trade_return_series",
        lambda cur, **kwargs: ({sample_date: Decimal("100")}, {sample_date: Decimal("100")}, ["missing Yahoo close: TEST"]),
    )
    monkeypatch.setattr(investment_returns, "_ibkr_trade_flow_events", lambda cur, **kwargs: [(sample_date, Decimal("100"))])
    monkeypatch.setattr(
        investment_returns,
        "_overlay_terminal_exact_section_value",
        lambda cur, values, **kwargs: values,
    )

    payload = build_section_return_series_payload(
        FakeCursor([]),
        section="us_stock",
        start_date=sample_date,
        interval="daily",
        base_currency="CNY",
        view_as_of=sample_date,
        reported_returns_cfg={},
        overview_payload={"accounts": []},
        owner_ratio_case_sql=_owner_ratio_case_sql,
        account_owner_ratio=_account_owner_ratio,
        excluded_holding_accounts=set(),
    )

    assert payload["section"] == "us_stock"
    assert "missing Yahoo close: TEST" in payload["coverage_notes"]


def test_us_stock_return_missing_history_has_no_zero_curve(monkeypatch: pytest.MonkeyPatch):
    day = date(2026, 1, 1)
    monkeypatch.setattr(investment_returns, "_section_first_activity_date", lambda cur, section: day)
    monkeypatch.setattr(investment_returns, "load_fx_timelines", lambda cur, base_currency: {})
    payload = build_section_return_series_payload(
        FakeCursor([[]]), section="us_stock", start_date=day,
        interval="daily", base_currency="CNY", view_as_of=day, reported_returns_cfg={},
        overview_payload={"accounts": []}, owner_ratio_case_sql=_owner_ratio_case_sql,
        account_owner_ratio=_account_owner_ratio, excluded_holding_accounts=set(), today_label_date=day)
    assert payload["status"] == "unavailable"
    assert payload["reason"] == "ibkr_position_history_unavailable"
    assert payload["rows"][0]["market_value"] is None
    assert payload["rows"][0]["time_weighted_return_rate"] is None
    assert payload["summary"]["total_return"] is None and payload["today_window"] is None


def test_ibkr_today_window_uses_snapshot_price_against_latest_confirmed_close():
    cur = FakeCursor(
        [
            {
                "latest_date": date(2026, 6, 15),
                "baseline_date": date(2026, 6, 14),
                "daily_price_pnl_cny": Decimal("0"),
                "daily_fx_pnl_cny": Decimal("0"),
                "baseline_value_cny": Decimal("100"),
            }
        ]
    )

    payload = investment_returns._ibkr_holdings_today_window_payload(
        cur,
        base_currency="CNY",
        label_date=date(2026, 6, 15),
        view_as_of=date(2026, 6, 15),
    )

    query = cur.executed[0][0]
    assert payload is not None
    assert "h.current_price AS current_price" in query
    assert "h.current_price IS DISTINCT FROM curr_overseas_px.close" in query
    assert "THEN curr_overseas_px.close" in query
    assert "THEN curr_overseas_px.trade_date" in query
    assert query.index(") prev_snap ON TRUE") < query.index(") prev_basis ON TRUE")


def test_ibkr_today_window_clears_after_daily_pnl_rollover_when_data_is_stale():
    cur = FakeCursor(
        [
            {
                "latest_date": date(2026, 6, 15),
                "baseline_date": date(2026, 6, 14),
                "daily_price_pnl_cny": Decimal("123"),
                "daily_fx_pnl_cny": Decimal("4"),
                "baseline_value_cny": Decimal("1000"),
            }
        ]
    )

    payload = investment_returns._ibkr_holdings_today_window_payload(
        cur,
        base_currency="CNY",
        label_date=date(2026, 6, 16),
        view_as_of=date(2026, 6, 16),
    )

    assert payload == {
        "label_date": "2026-06-16",
        "baseline_date": "2026-06-15",
        "latest_date": "2026-06-15",
        "total_return": None,
        "return_rate": None,
        "status": "unavailable",
        "reason": "stale_snapshot",
    }


def _base_total_return_rows():
    return [
        {
            "date": date(2026, 1, 1),
            "market_value_cny": Decimal("100"),
            "cost_cny": Decimal("100"),
            "position_pnl_cny": Decimal("0"),
            "realized_income_cny": Decimal("0"),
            "total_pnl_cny": Decimal("0"),
            "return_rate": Decimal("0"),
        },
        {
            "date": date(2026, 1, 2),
            "market_value_cny": Decimal("110"),
            "cost_cny": Decimal("100"),
            "position_pnl_cny": Decimal("10"),
            "realized_income_cny": Decimal("0"),
            "total_pnl_cny": Decimal("10"),
            "return_rate": Decimal("0.10"),
        },
    ]


def _patch_total_return_extras(
    monkeypatch: pytest.MonkeyPatch,
    *,
    warning: str | None = None,
    adjustment: Decimal = Decimal("0"),
    section_amounts: dict[date, Decimal] | None = None,
):
    monkeypatch.setattr(investment_returns, "load_fx_timelines", lambda cur, base_currency: {"CNY": [(date(2026, 1, 2), Decimal("1"))]})
    monkeypatch.setattr(investment_returns, "_overlay_exact_snapshot_values", lambda cur, values, **kwargs: values)
    monkeypatch.setattr(investment_returns, "_overlay_terminal_exact_section_value", lambda cur, values, **kwargs: values)
    monkeypatch.setattr(
        investment_returns,
        "tonghuashun_series",
        lambda cur, dates, base_currency, account_owner_ratio: {d: Decimal("0") for d in dates},
    )
    monkeypatch.setattr(
        investment_returns,
        "_tonghuashun_cost_series",
        lambda cur, **kwargs: {d: Decimal("0") for d in kwargs["dates"]},
    )
    monkeypatch.setattr(
        investment_returns,
        "_ibkr_full_trade_return_series",
        lambda cur, dates, **kwargs: (
            {d: Decimal("0") for d in dates},
            {d: Decimal("0") for d in dates},
            [warning] if warning else [],
        ),
    )
    monkeypatch.setattr(
        investment_returns,
        "_reported_account_return_adjustments",
        lambda cur, dates, base_currency, **kwargs: {d: (adjustment if d == dates[-1] else Decimal("0")) for d in dates},
    )
    monkeypatch.setattr(
        investment_returns,
        "_total_section_return_amounts",
        lambda cur, **kwargs: section_amounts,
    )


def test_build_investment_return_series_payload_preserves_legacy_shape(monkeypatch: pytest.MonkeyPatch):
    _patch_total_return_extras(monkeypatch)
    payload = build_investment_return_series_payload(
        FakeCursor([_base_total_return_rows()]),
        start_date=date(2026, 1, 1),
        interval="daily",
        base_currency="CNY",
        view_as_of=date(2026, 1, 2),
        overview_payload={"accounts": []},
        reported_returns_cfg={},
        owner_ratio_case_sql=_owner_ratio_case_sql,
        account_owner_ratio=_account_owner_ratio,
        account_initial_capital_cny=lambda account_code: None,
    )

    assert set(payload) == {"start", "end", "base_currency", "interval", "method", "coverage_notes", "rows"}
    assert payload["start"] == "2026-01-01"
    assert payload["end"] == "2026-01-02"
    assert payload["method"] == "coverage_adjusted_daily_return"
    assert {"date", "market_value", "cost", "position_pnl", "total_pnl", "return_rate"} <= set(payload["rows"][-1])


def test_total_return_with_unknown_ibkr_history_is_not_partial_zero(monkeypatch: pytest.MonkeyPatch):
    _patch_total_return_extras(monkeypatch, warning="ibkr_position_history_unavailable")
    payload = build_investment_return_series_payload(
        FakeCursor([_base_total_return_rows()]), start_date=date(2026, 1, 1), interval="daily",
        base_currency="CNY", view_as_of=date(2026, 1, 2), overview_payload={"accounts": []},
        reported_returns_cfg={}, owner_ratio_case_sql=_owner_ratio_case_sql,
        account_owner_ratio=_account_owner_ratio, account_initial_capital_cny=lambda account: None,
        today_label_date=date(2026, 1, 2))
    assert payload["status"] == "unavailable" and payload["today_window"] is None
    assert all(row["market_value"] is None and row["total_pnl"] is None
               and row["time_weighted_return_rate"] is None for row in payload["rows"])


def test_build_investment_return_series_payload_keeps_a_stock_net_liquidation(monkeypatch: pytest.MonkeyPatch):
    dates = [date(2026, 1, 1), date(2026, 1, 2)]
    rows = [
        {
            "date": sample_date,
            "market_value_cny": Decimal("0"),
            "cost_cny": Decimal("0"),
            "position_pnl_cny": Decimal("0"),
            "realized_income_cny": Decimal("0"),
            "total_pnl_cny": Decimal("0"),
            "return_rate": Decimal("0"),
        }
        for sample_date in dates
    ]

    monkeypatch.setattr(investment_returns, "load_fx_timelines", lambda cur, base_currency: {"CNY": [(dates[-1], Decimal("1"))]})
    monkeypatch.setattr(investment_returns, "_overlay_exact_snapshot_values", lambda cur, values, **kwargs: values)

    def fail_a_stock_terminal_overlay(cur, values, **kwargs):
        if kwargs.get("section") == "a_stock":
            raise AssertionError("A-share account returns must use net liquidation, not holding-only terminal value")
        return values

    monkeypatch.setattr(investment_returns, "_overlay_terminal_exact_section_value", fail_a_stock_terminal_overlay)
    monkeypatch.setattr(
        investment_returns,
        "tonghuashun_series",
        lambda cur, dates, base_currency, account_owner_ratio: {dates[0]: Decimal("100"), dates[1]: Decimal("110")},
    )
    monkeypatch.setattr(
        investment_returns,
        "_tonghuashun_cost_series",
        lambda cur, **kwargs: {dates[0]: Decimal("100"), dates[1]: Decimal("100")},
    )
    monkeypatch.setattr(
        investment_returns,
        "_ibkr_full_trade_return_series",
        lambda cur, dates, **kwargs: ({d: Decimal("0") for d in dates}, {d: Decimal("0") for d in dates}, []),
    )
    monkeypatch.setattr(
        investment_returns,
        "_reported_account_return_adjustments",
        lambda cur, dates, base_currency, **kwargs: {d: Decimal("0") for d in dates},
    )
    monkeypatch.setattr(investment_returns, "_total_section_return_amounts", lambda cur, **kwargs: None)

    payload = build_investment_return_series_payload(
        FakeCursor([rows]),
        start_date=dates[0],
        interval="daily",
        base_currency="CNY",
        view_as_of=dates[-1],
        overview_payload={
            "accounts": [
                {
                    "account_code": "tonghuashun",
                    "include_in_nav": True,
                    "primary_balance": {"amount": "110", "currency": "CNY"},
                }
            ]
        },
        reported_returns_cfg={},
        owner_ratio_case_sql=_owner_ratio_case_sql,
        account_owner_ratio=_account_owner_ratio,
        account_initial_capital_cny=lambda account_code: None,
    )

    latest = payload["rows"][-1]
    assert latest["market_value"] == "110"
    assert latest["position_pnl"] == "10"
    assert latest["total_pnl"] == "10"
    assert latest["return_rate"] == "0.1"


def test_build_investment_return_series_payload_clamps_start_after_view_date(monkeypatch: pytest.MonkeyPatch):
    _patch_total_return_extras(monkeypatch)
    payload = build_investment_return_series_payload(
        FakeCursor([[_base_total_return_rows()[-1]]]),
        start_date=date(2026, 6, 10),
        interval="daily",
        base_currency="CNY",
        view_as_of=date(2026, 5, 31),
        overview_payload={"accounts": []},
        reported_returns_cfg={},
        owner_ratio_case_sql=_owner_ratio_case_sql,
        account_owner_ratio=_account_owner_ratio,
        account_initial_capital_cny=lambda account_code: None,
    )

    assert payload["start"] == "2026-05-31"


def test_build_investment_return_series_payload_applies_reported_adjustment(monkeypatch: pytest.MonkeyPatch):
    _patch_total_return_extras(monkeypatch, adjustment=Decimal("5"))
    payload = build_investment_return_series_payload(
        FakeCursor([_base_total_return_rows()]),
        start_date=date(2026, 1, 1),
        interval="daily",
        base_currency="CNY",
        view_as_of=date(2026, 1, 2),
        overview_payload={"accounts": []},
        reported_returns_cfg={},
        owner_ratio_case_sql=_owner_ratio_case_sql,
        account_owner_ratio=_account_owner_ratio,
        account_initial_capital_cny=lambda account_code: None,
    )

    assert payload["rows"][-1]["reported_return_adjustment"] == "5"
    assert payload["rows"][-1]["total_pnl"] == "15"
    assert payload["rows"][-1]["return_rate"] == "0.15"


def test_build_investment_return_series_payload_uses_section_return_amounts(monkeypatch: pytest.MonkeyPatch):
    rows = _base_total_return_rows()
    _patch_total_return_extras(
        monkeypatch,
        section_amounts={rows[0]["date"]: Decimal("0"), rows[1]["date"]: Decimal("25")},
    )

    payload = build_investment_return_series_payload(
        FakeCursor([rows]),
        start_date=date(2026, 1, 1),
        interval="daily",
        base_currency="CNY",
        view_as_of=date(2026, 1, 2),
        overview_payload={"accounts": []},
        reported_returns_cfg={},
        owner_ratio_case_sql=_owner_ratio_case_sql,
        account_owner_ratio=_account_owner_ratio,
        account_initial_capital_cny=lambda account_code: None,
    )

    assert payload["rows"][-1]["position_pnl"] == "25"
    assert payload["rows"][-1]["reported_return_adjustment"] == "15"
    assert payload["rows"][-1]["total_pnl"] == "25"
    assert payload["rows"][-1]["return_rate"] == "0.25"


def test_build_investment_return_series_payload_injects_ibkr_warning(monkeypatch: pytest.MonkeyPatch):
    _patch_total_return_extras(monkeypatch, warning="missing Yahoo close: TEST")
    payload = build_investment_return_series_payload(
        FakeCursor([_base_total_return_rows()]),
        start_date=date(2026, 1, 1),
        interval="daily",
        base_currency="CNY",
        view_as_of=date(2026, 1, 2),
        overview_payload={"accounts": []},
        reported_returns_cfg={},
        owner_ratio_case_sql=_owner_ratio_case_sql,
        account_owner_ratio=_account_owner_ratio,
        account_initial_capital_cny=lambda account_code: None,
    )

    assert any(item.get("reason") == "missing Yahoo close: TEST" for item in payload["coverage_notes"])


def test_esop_missing_same_day_fx_does_not_become_zero(monkeypatch):
    _patch_total_return_extras(monkeypatch)
    args=dict(start_date=date(2026,1,1),interval='daily',base_currency='CNY',view_as_of=date(2026,1,2),
              overview_payload={'accounts':[{'account_code':'huawei_stock','include_in_nav':True,
                  'primary_balance':{'amount':'100','currency':'USD'}}]},reported_returns_cfg={},
              owner_ratio_case_sql=_owner_ratio_case_sql,account_owner_ratio=_account_owner_ratio,
              account_initial_capital_cny=lambda account:None)
    payload=build_investment_return_series_payload(FakeCursor([_base_total_return_rows()]),**args)
    assert payload['status']=='unavailable' and payload['reason']=='fx_rate_unknown'
    assert all(row['total_pnl'] is None for row in payload['rows'])
    monkeypatch.setattr(investment_returns,'load_fx_timelines',lambda cur,base_currency:{'USD':[(date(2026,1,2),Decimal('7'))]})
    payload=build_investment_return_series_payload(FakeCursor([_base_total_return_rows()]),**args)
    assert payload.get('status')!='unavailable'


def test_missing_ibkr_official_sources_allow_verified_generic_coverage(monkeypatch):
    from domain.accounts import official_flow_coverage, flow_coverage
    from domain.investments import investment_returns as service
    from datetime import timedelta

    end=date(2026,6,18); start=end-timedelta(days=1)
    class Conn:
        autocommit=False
    class Cursor:
        connection=Conn()
        def execute(self,query,params=None):
            self.query=query
        def fetchone(self):
            if 'MAX(as_of)' in self.query:
                return {'previous_day':start}
            return {'isolation':'repeatable read'}
    monkeypatch.setattr(official_flow_coverage,'_source',lambda *_:None)
    monkeypatch.setattr(official_flow_coverage,'_source_present',lambda *_:False)
    monkeypatch.setattr(official_flow_coverage,'_view_date',lambda:end)
    monkeypatch.setattr(flow_coverage,'has_unvalued_non_cash_transfer',lambda *_:False)
    monkeypatch.setattr(service,'_cash_delta_cny',lambda *a,**kw:None)
    monkeypatch.setattr(service,'load_account_balance_basis',lambda *a,**kw:{
        'as_of':end,'rows':[],'position_value_gross':Decimal(110),'holding_pnl_gross':None,
        'net_gross':Decimal(110),'net_personal':Decimal(110),'net_liquidation_source':'current_snapshot',
        'net_liquidation_rollforward':None,'previous_net_gross':Decimal(100),'previous_as_of':start,
        'cash_value_gross':Decimal(10),'securities_value_gross':Decimal(100),'other_liabilities_gross':Decimal(0)})
    proof={'start':start,'end':end,'opening_gross_cny':Decimal(100),'closing_gross_cny':Decimal(110),
           'external_flow_gross_cny':Decimal(0),'basis':'reviewed_source_reports'}
    monkeypatch.setattr(flow_coverage,'latest_verified_period',lambda *a,**kw:proof)
    monkeypatch.setattr(flow_coverage,'verified_period',lambda *a,**kw:proof)
    result=service.build_account_performance_payload(Cursor(),account_code='ibkr',owner_ratio=Decimal(1),
        initial_capital=Decimal(100),initial_capital_as_of=start)
    assert Decimal(result['daily_pnl_cny'])==Decimal(10)
    assert Decimal(result['total_pnl_cny'])==Decimal(10)
    assert result['daily_pnl_source']=='verified_reviewed_source_period'
