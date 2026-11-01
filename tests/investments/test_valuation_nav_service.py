from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from domain.investments.valuation_nav import (  # noqa: E402
    _bank_backcast_series,
    _investment_snapshot_series,
    _statement_rows_by_account,
    _tonghuashun_series,
    build_nav_drawdown_payload,
    build_nav_series_payload,
    build_nav_weekly_payload,
    current_nav_account_values_from_parts,
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


def test_bank_nav_requires_fx_on_the_valuation_day():
    from domain.investments.valuation_nav import build_weekly_nav_rows, fx_rate_at
    day = date(2026, 6, 4)
    accounts = [{"account_code": "bank_usd", "account_type": "funding", "base_currency": "USD",
                 "primary_balance": {"amount": "100", "currency": "USD", "as_of": day}}]
    rates = {"USD": [(date(2026, 6, 3), Decimal("7")), (date(2026, 6, 5), Decimal("8"))]}
    assert fx_rate_at(rates, "USD", day, "CNY") is None
    args = dict(accounts=accounts, rows_by_account={}, dates=[day], terminal_date=day, base_currency="CNY")
    bank = _bank_backcast_series(fx_timelines=rates, **args)
    values = build_weekly_nav_rows(dates=[day], bank_series=bank, snapshot_series={}, a_share_series={}, ibkr_values={})
    assert values[0]["net_worth"] is None and values[0]["unavailable_accounts"] == ["bank_usd"]
    bank = _bank_backcast_series(fx_timelines={"USD": [(day, Decimal("7"))]}, **args)
    values = build_weekly_nav_rows(dates=[day], bank_series=bank, snapshot_series={}, a_share_series={}, ibkr_values={})
    assert values[0]["net_worth"] == Decimal("700")


def test_nav_series_and_drawdown_propagate_missing_history():
    rows = [
        {"asOfDate": "2026-05-10", "netWorth": "100"},
        {"asOfDate": "2026-05-11", "netWorth": None, "unavailable_accounts": ["ibkr"]},
        {"asOfDate": "2026-05-12", "netWorth": "90"},
    ]
    series = build_nav_series_payload(rows)
    assert series["rows"][0]["netWorth"] == 100
    assert series["rows"][1]["netWorth"] is None
    drawdown = build_nav_drawdown_payload(rows)
    assert drawdown["max_drawdown"] is None
    assert drawdown["rows"][0]["drawdown"] == "0"
    assert drawdown["rows"][1]["drawdown"] is None
    assert drawdown["rows"][2]["drawdown"] is None


def test_nav_drawdown_preserves_proven_zero_and_known_history():
    rows = [{"asOfDate": "2026-05-10", "netWorth": "100"},
            {"asOfDate": "2026-05-11", "netWorth": "90"}]
    payload = build_nav_drawdown_payload(rows)
    assert payload["max_drawdown"] == "-0.1"
    assert payload["rows"][1]["drawdown"] == "-0.1"
    zero = [{"asOfDate": "2026-05-10", "netWorth": "0"}]
    assert build_nav_series_payload(zero)["rows"][0]["netWorth"] == 0
    assert build_nav_drawdown_payload(zero)["rows"][0]["drawdown"] == "0"


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
            {"item_exists": True, "batch_exists": True, "cashflow_fact_exists": True},
            [],
            {"fact_exists": False, "px_exists": False},
            {"exists": True},
            [],
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
        include_ibkr=False,
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
            {"item_exists": True, "batch_exists": True, "cashflow_fact_exists": True},
            [],
            {"fact_exists": False, "px_exists": False},
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
            {"item_exists": True, "batch_exists": True, "cashflow_fact_exists": True},
            [],
            {"fact_exists": False, "px_exists": False},
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
            {"item_exists": True, "batch_exists": True, "cashflow_fact_exists": True},
            [],
            {"fact_exists": False, "px_exists": False},
            {"exists": True},
            [],
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
        include_ibkr=False,
        terminal_date=date(2026, 6, 16),
        terminal_nav_bucket={"assets": Decimal("250"), "liabilities": Decimal("40"), "net_worth": Decimal("210")},
    )

    assert payload["rows"][-1]["assets"] == "250"
    assert payload["rows"][-1]["liabilities"] == "40"
    assert payload["rows"][-1]["netWorth"] == "210"


def test_build_nav_weekly_payload_does_not_scale_unproven_account_history():
    cur = FakeCursor(
        [
            [],
            {"fact_exists": True, "px_exists": True, "item_exists": True, "batch_exists": True},
            [],
            [
                {"date": date(2026, 6, 10), "gross_value_cny": Decimal("100")},
                {"date": date(2026, 6, 17), "gross_value_cny": Decimal("110")},
            ],
            [],
            {"exists": True},
            [],
        ]
    )

    payload = build_nav_weekly_payload(
        cur,
        start_date=date(2026, 6, 10),
        base_currency="CNY",
        overview_payload={
            "accounts": [
                {
                    "account_code": "tonghuashun",
                    "account_type": "brokerage",
                    "include_in_nav": True,
                    "primary_balance": {"amount": "120", "currency": "CNY"},
                }
            ]
        },
        owner_ratio_case_sql=_owner_ratio_case_sql,
        account_owner_ratio=_account_owner_ratio,
        include_ibkr=False,
        terminal_date=date(2026, 6, 17),
        terminal_account_values_cny={"tonghuashun": Decimal("130")},
    )

    assert payload["rows"][-1]["netWorth"] is None
    assert payload["rows"][-1]["unavailable_accounts"] == ["tonghuashun"]


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


def test_current_nav_account_values_from_parts_groups_positions_and_unrepresented_accounts():
    values = current_nav_account_values_from_parts(
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
            {"account_code": "ignored", "amount_cny": Decimal("99"), "include_in_nav": False},
        ],
    )

    assert values == {
        "ibkr": Decimal("180"),
        "tonghuashun": Decimal("246"),
        "ccb": Decimal("80"),
    }


def test_statement_rows_by_account_requires_source_proof(monkeypatch) -> None:
    from domain.imports import source_fact_evidence

    class Verifier:
        def __init__(self, cur):
            pass

        def verify(self, *, table, fact_id):
            return {"status": "verified" if fact_id == 11 else "inconsistent"}

    monkeypatch.setattr(source_fact_evidence, "NativeFactVerifier", Verifier)
    cur = FakeCursor([
        {"cashflow_fact_exists": True, "item_exists": True, "batch_exists": True},
        [{"id": 11, "account_code": "ccb", "amount": Decimal("-10"), "statement_balance": "999"},
         {"id": 12, "account_code": "ccb", "amount": Decimal("-20")}],
    ])
    rows = _statement_rows_by_account(cur, ["ccb"], date(2026, 5, 24), date(2026, 5, 31))
    assert rows["ccb"][0]["amount"] == Decimal("-10")
    assert rows["ccb"][0]["statement_balance"] is None
    assert rows.evidence_problems["ccb"]


def test_statement_rows_by_account_missing_schema_is_unknown() -> None:
    cur = FakeCursor([{"cashflow_fact_exists": False}])
    rows = _statement_rows_by_account(cur, ["ccb"], date(2026, 5, 24), date(2026, 5, 31))
    assert rows == {}
    assert rows.evidence_problems["ccb"] == {"cash_source_schema_unavailable"}


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


def test_bank_backcast_treats_liability_statement_balance_as_negative_nav() -> None:
    rows = {
        "ccb_credit_0453": [
            {
                "id": 1,
                "account_code": "ccb_credit_0453",
                "transaction_date": date(2026, 1, 4),
                "amount": Decimal("-100"),
                "currency": "CNY",
                "statement_balance": "1000",
            },
            {
                "id": 2,
                "account_code": "ccb_credit_0453",
                "transaction_date": date(2026, 1, 5),
                "amount": Decimal("-100"),
                "currency": "CNY",
                "statement_balance": "1100",
            },
        ]
    }

    values = _bank_backcast_series(
        accounts=[
            {
                "account_code": "ccb_credit_0453",
                "account_type": "liability",
                "include_in_nav": True,
                "base_currency": "CNY",
                "primary_balance": {"amount": "-1100", "currency": "CNY", "as_of": "2026-01-05"},
            }
        ],
        rows_by_account=rows,
        dates=[date(2026, 1, 1), date(2026, 1, 4), date(2026, 1, 5)],
        terminal_date=date(2026, 1, 5),
        fx_timelines={"CNY": [(date(2026, 1, 1), Decimal("1"))]},
        base_currency="CNY",
    )

    assert values[date(2026, 1, 1)]["net_worth"] == Decimal("-900")
    assert values[date(2026, 1, 4)]["net_worth"] == Decimal("-1000")
    assert values[date(2026, 1, 5)]["liabilities"] == Decimal("1100")
    assert values[date(2026, 1, 5)]["net_worth"] == Decimal("-1100")


def test_bank_backcast_uses_non_nav_rows_for_account_balance_changes() -> None:
    rows = {
        "icbc": [
            {
                "id": 1,
                "account_code": "icbc",
                "transaction_date": date(2026, 1, 6),
                "amount": Decimal("-1000"),
                "currency": "CNY",
                "statement_balance": "0.63",
            }
        ]
    }

    values = _bank_backcast_series(
        accounts=[
            {
                "account_code": "icbc",
                "account_type": "funding",
                "include_in_nav": True,
                "base_currency": "CNY",
            }
        ],
        rows_by_account=rows,
        dates=[date(2026, 1, 1), date(2026, 1, 6)],
        terminal_date=date(2026, 1, 31),
        fx_timelines={"CNY": [(date(2026, 1, 1), Decimal("1"))]},
        base_currency="CNY",
    )

    assert values[date(2026, 1, 1)]["net_worth"] == Decimal("1000.63")
    assert values[date(2026, 1, 6)]["net_worth"] == Decimal("0.63")


def test_bank_backcast_reconstructs_zero_anchor_account_from_cashflow_rows() -> None:
    rows = {
        "alipay": [
            {
                "id": 1,
                "account_code": "alipay",
                "transaction_date": date(2026, 1, 18),
                "amount": Decimal("-19"),
                "currency": "CNY",
                "statement_balance": None,
            },
            {
                "id": 2,
                "account_code": "alipay",
                "transaction_date": date(2026, 2, 1),
                "amount": Decimal("-81"),
                "currency": "CNY",
                "statement_balance": None,
            },
        ]
    }

    values = _bank_backcast_series(
        accounts=[
            {
                "account_code": "alipay",
                "account_type": "funding",
                "include_in_nav": True,
                "base_currency": "CNY",
            }
        ],
        rows_by_account=rows,
        dates=[date(2026, 1, 1), date(2026, 1, 31), date(2026, 2, 28)],
        terminal_date=date(2026, 2, 28),
        fx_timelines={"CNY": [(date(2026, 1, 1), Decimal("1"))]},
        base_currency="CNY",
    )

    assert all("alipay" in value["unavailable_accounts"] for value in values.values())


def test_bank_backcast_does_not_infer_unanchored_liability_without_statement_balance() -> None:
    rows = {
        "gf_9711": [
            {
                "id": 1,
                "account_code": "gf_9711",
                "transaction_date": date(2026, 1, 3),
                "amount": Decimal("-100"),
                "currency": "USD",
                "statement_balance": None,
            }
        ]
    }

    values = _bank_backcast_series(
        accounts=[
            {
                "account_code": "gf_9711",
                "account_type": "liability",
                "include_in_nav": True,
                "base_currency": "USD",
            }
        ],
        rows_by_account=rows,
        dates=[date(2026, 1, 1), date(2026, 1, 31)],
        terminal_date=date(2026, 1, 31),
        fx_timelines={"CNY": [(date(2026, 1, 1), Decimal("1"))], "USD": [(date(2026, 1, 1), Decimal("7"))]},
        base_currency="CNY",
    )

    assert values[date(2026, 1, 1)]["net_worth"] == Decimal("0")
    assert values[date(2026, 1, 31)]["net_worth"] == Decimal("0")


def test_build_nav_weekly_payload_clamps_start_after_anchor():
    cur = FakeCursor(
        [
            [],
            {"fact_exists": False, "px_exists": False},
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


def test_current_nav_ignores_positions_of_excluded_account():
    from domain.investments.valuation_nav import current_nav_bucket_from_parts, current_nav_account_values_from_parts
    accounts=[{'account_code':'kept','include_in_nav':True,'amount_cny':Decimal('1050')},
              {'account_code':'ibkr','include_in_nav':False,'amount_cny':None,'snapshot_scope_status':'unknown'}]
    positions=[{'account_code':'kept','market_value_cny':Decimal('1050'),'snapshot_scope_status':'complete'},
               {'account_code':'ibkr','market_value_cny':Decimal('999'),'snapshot_scope_status':'unknown'}]
    bucket=current_nav_bucket_from_parts(account_balances=accounts,investment_positions=positions)
    assert bucket['net_worth']==Decimal('1050')
    assert current_nav_account_values_from_parts(account_balances=accounts,investment_positions=positions)=={'kept':Decimal('1050')}
    accounts[1]['include_in_nav']=True
    assert current_nav_bucket_from_parts(account_balances=accounts,investment_positions=positions)['net_worth'] is None


def test_weekly_nav_does_not_include_disabled_tonghuashun(monkeypatch):
    from domain.investments import valuation_nav as nav
    day = date(2026, 10, 30)
    def excluded(*args, **kwargs):
        raise AssertionError('disabled account was calculated')
    monkeypatch.setattr(nav, '_tonghuashun_series', excluded)
    monkeypatch.setattr(nav, 'load_fx_timelines', lambda *args, **kwargs: {})
    monkeypatch.setattr(nav, '_statement_rows_by_account', lambda *args, **kwargs: {})
    monkeypatch.setattr(nav, '_bank_backcast_series', lambda **kwargs: {})
    monkeypatch.setattr(nav, '_investment_snapshot_series', lambda *args, **kwargs: {})
    payload = nav.build_nav_weekly_payload(object(), start_date=day, terminal_date=day,
        base_currency='CNY', overview_payload={'accounts':[{'account_code':'tonghuashun','include_in_nav':False}]},
        owner_ratio_case_sql=lambda alias:('1',[]),account_owner_ratio=lambda _:Decimal(1),
        include_ibkr=False,interval='daily')
    assert payload['rows'][0]['netWorth']=='0'
