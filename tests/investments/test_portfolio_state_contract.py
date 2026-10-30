import sys
from datetime import date
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from domain.investments.portfolio_state import (  # noqa: E402
    ACCOUNT_BALANCE_FIELDS,
    DATA_QUALITY_FIELDS,
    DATA_QUALITY_PAYLOAD_KEYS,
    INVESTMENT_POSITION_FIELDS,
    INVESTMENT_TRANSACTION_FIELDS,
    PORTFOLIO_STATE_KEYS,
    RISK_SNAPSHOT_FIELDS,
    VALUATION_SERIES_FIELDS,
    build_data_quality_payload_from_parts,
    build_portfolio_state_from_parts,
    load_account_balances,
    load_investment_transactions,
    load_investment_positions,
    load_valuation_series,
)


def test_portfolio_state_has_contract_top_level_keys():
    state = build_portfolio_state_from_parts(as_of=date(2026, 5, 31), base_currency="CNY")

    assert list(state) == PORTFOLIO_STATE_KEYS
    assert state["as_of"] == "2026-05-31"
    assert state["base_currency"] == "CNY"


def test_portfolio_state_component_fields_are_present_and_json_ready():
    account = {
        "account_code": "cash",
        "parent_account_code": None,
        "display_name": "Cash",
        "account_type": "funding",
        "base_currency": "CNY",
        "amount": Decimal("100.25"),
        "currency": "CNY",
        "amount_cny": Decimal("100.25"),
        "as_of": date(2026, 5, 31),
        "truth_source": "manual",
        "review_status": "ready_for_review",
        "include_in_nav": True,
    }
    position = {
        "account_code": "broker",
        "asset_id": "QQQ",
        "symbol": "QQQ",
        "asset_name": "QQQ ETF",
        "asset_type": "us_stock",
        "quantity": Decimal("1"),
        "market_value": Decimal("450"),
        "currency": "USD",
        "market_value_cny": Decimal("3240"),
        "cost_cny": Decimal("3000"),
        "holding_pnl_cny": Decimal("240"),
        "as_of": date(2026, 5, 31),
        "source_type": "holdings_snapshot",
    }
    transaction = {
        "account_code": "broker",
        "trade_date": date(2026, 1, 1),
        "symbol": "QQQ",
        "name": "QQQ ETF",
        "side": "buy",
        "quantity": Decimal("1"),
        "price": Decimal("450"),
        "gross_amount": Decimal("450"),
        "fees": Decimal("0"),
        "currency": "USD",
        "source": "manual",
        "source_key": "demo",
    }
    valuation = {
        "date": date(2026, 5, 31),
        "assets_cny": Decimal("3340.25"),
        "liabilities_cny": Decimal("0"),
        "net_worth_cny": Decimal("3340.25"),
        "investment_value_cny": Decimal("3240"),
        "investment_cost_cny": Decimal("3000"),
        "investment_pnl_cny": Decimal("240"),
        "investment_return_rate": Decimal("0.08"),
    }
    state = build_portfolio_state_from_parts(
        as_of=date(2026, 5, 31),
        base_currency="CNY",
        account_balances=[account],
        investment_positions=[position],
        investment_transactions=[transaction],
        valuation_series=[valuation],
    )

    assert set(ACCOUNT_BALANCE_FIELDS).issubset(state["account_balances"][0])
    assert set(INVESTMENT_POSITION_FIELDS).issubset(state["investment_positions"][0])
    assert set(INVESTMENT_TRANSACTION_FIELDS).issubset(state["investment_transactions"][0])
    assert set(VALUATION_SERIES_FIELDS).issubset(state["valuation_series"][0])
    assert set(RISK_SNAPSHOT_FIELDS).issubset(state["risk_snapshot"])
    assert set(DATA_QUALITY_FIELDS).issubset(state["data_quality"])
    assert state["account_balances"][0]["amount_cny"] == "100.25"
    assert state["investment_positions"][0]["market_value_cny"] == "3240"


def test_portfolio_state_data_quality_reports_coverage_freshness_and_missing_items():
    state = build_portfolio_state_from_parts(
        as_of=date(2026, 5, 31),
        base_currency="CNY",
        account_balances=[
            {
                "account_code": "cash",
                "amount_cny": Decimal("0"),
                "as_of": None,
                "review_status": "missing_data",
                "include_in_nav": True,
            }
        ],
        investment_positions=[
            {
                "account_code": "broker",
                "asset_id": "QQQ",
                "market_value_cny": Decimal("100"),
                "as_of": date(2026, 5, 30),
            }
        ],
        investment_transactions=[
            {
                "account_code": "broker",
                "trade_date": date(2026, 1, 1),
                "gross_amount": Decimal("100"),
            }
        ],
    )

    quality = state["data_quality"]
    assert quality["coverage"]["account_balances"] == {"rows": 1, "has_data": True}
    assert quality["coverage"]["cash_transactions"] == {"rows": 0, "has_data": False}
    assert quality["freshness"]["account_balances"]["missing_as_of_count"] == 1
    assert quality["freshness"]["investment_positions"]["latest_as_of"] == "2026-05-30"
    assert quality["freshness"]["investment_transactions"]["latest_as_of"] == "2026-01-01"
    assert quality["missing"]["account_balances_missing_data"] == ["cash"]
    assert "cash_transactions" in quality["missing"]["components_without_rows"]


def test_portfolio_state_data_quality_flags_stale_valuation_and_missing_fx():
    state = build_portfolio_state_from_parts(
        as_of=date(2026, 6, 24),
        base_currency="CNY",
        account_balances=[
            {
                "account_code": "ibkr",
                "amount_cny": Decimal("700"),
                "currency": "USD",
                "as_of": date(2026, 6, 24),
                "review_status": "reviewed",
            }
        ],
        investment_positions=[
            {
                "account_code": "ibkr",
                "asset_id": "GOOG",
                "currency": "USD",
                "market_value_cny": Decimal("700"),
                "as_of": date(2026, 6, 24),
            }
        ],
        valuation_series=[
            {
                "date": date(2026, 6, 18),
                "net_worth_cny": Decimal("1000"),
            }
        ],
        fx_rates=[],
    )

    checks = {check["id"]: check for check in state["data_quality"]["checks"]}

    assert checks["valuation_series_freshness"]["status"] == "attention"
    assert checks["fx_rate_coverage"]["severity"] == "error"
    assert checks["fx_rate_coverage"]["details"]["missing_currencies"] == ["USD"]
    assert state["data_quality"]["summary"]["status"] == "error"


def test_data_quality_payload_is_lightweight_and_contract_stable():
    payload = build_data_quality_payload_from_parts(
        as_of=date(2026, 5, 31),
        base_currency="CNY",
        account_balances=[
            {
                "account_code": "cash",
                "amount_cny": Decimal("100"),
                "as_of": date(2026, 5, 31),
                "review_status": "reviewed",
            }
        ],
        fx_rates=[{"quote_currency": "USD", "rate": Decimal("7.2"), "as_of": date(2026, 5, 31)}],
    )

    assert list(payload) == DATA_QUALITY_PAYLOAD_KEYS
    assert payload["as_of"] == "2026-05-31"
    assert payload["base_currency"] == "CNY"
    assert set(DATA_QUALITY_FIELDS).issubset(payload["data_quality"])
    assert "account_balances" not in payload
    assert payload["data_quality"]["coverage"]["account_balances"] == {"rows": 1, "has_data": True}


def test_load_investment_transactions_reads_trade_fact_table_first():
    cur = FakeCursor(
        [
            {"table_name": "pams_investment_trade_fact"},
            {"table_name": "pams_fx_rates_daily"},
            [{"quote_currency": "USD", "rate": Decimal("7.2")}],
            [
                {
                    "account_code": "ibkr",
                    "trade_date": date(2026, 6, 24),
                    "symbol": "GOOG",
                    "name": "ALPHABET INC",
                    "side": "buy",
                    "quantity": Decimal("1"),
                    "price": Decimal("100"),
                    "gross_amount": Decimal("100"),
                    "fees": Decimal("1.5"),
                    "currency": "USD",
                    "source": "ibkr_flex",
                    "source_key": "ibkr-1",
                    "trade_type": "trade",
                    "net_cash": Decimal("-101.5"),
                    "net_cash_cny": Decimal("-730.8"),
                    "realized_pnl_cny": Decimal("0"),
                }
            ],
        ]
    )

    rows = load_investment_transactions(cur, base_currency="CNY")

    assert rows[0]["symbol"] == "GOOG"
    assert rows[0]["fees"] == Decimal("1.5")
    assert rows[0]["gross_amount_cny"] == Decimal("720.0")
    assert "pams_investment_trade_fact" in cur.executed[-1][0]


def test_load_valuation_series_reads_portfolio_valuation_state():
    cur = FakeCursor(
        [
            {"table_name": "pams_portfolio_valuation_state"},
            [
                {
                    "valuation_date": date(2026, 6, 24),
                    "assets_cny": Decimal("1200"),
                    "liabilities_cny": Decimal("200"),
                    "net_liquidation_value": Decimal("1000"),
                    "investment_value_cny": Decimal("800"),
                    "total_pnl_cny": Decimal("100"),
                }
            ],
        ]
    )

    rows = load_valuation_series(cur, base_currency="CNY")

    assert rows == [
        {
            "date": date(2026, 6, 24),
            "assets_cny": Decimal("1200"),
            "liabilities_cny": Decimal("200"),
            "net_worth_cny": Decimal("1000"),
            "investment_value_cny": Decimal("800"),
            "investment_cost_cny": Decimal("700"),
            "investment_pnl_cny": Decimal("100"),
            "investment_return_rate": Decimal("0.1428571428571428571428571429"),
        }
    ]


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


def test_load_account_balances_prefers_newer_platform_snapshot_for_platform_accounts():
    cur = FakeCursor(
        [
            {"table_name": "pams_fx_rates_daily"},
            [],
            {"table_name": None},
            {"table_name": "pams_holdings_snapshot"},
            [
                {
                    "account_code": "tonghuashun",
                    "currency": "CNY",
                    "as_of": date(2026, 6, 16),
                    "market_value": Decimal("387782.00"),
                }
            ],
        ]
    )
    account_cfg = {
        "accounts": {
            "tonghuashun": {
                "display_name": "Tonghuashun",
                "account_type": "brokerage",
                "base_currency": "CNY",
                "truth_source": "platform_snapshot",
                "owner_ratio": "0.625",
                "manual_balance": {
                    "amount": "367653.64",
                    "currency": "CNY",
                    "as_of": date(2026, 6, 9),
                },
            }
        }
    }

    rows = load_account_balances(cur, "CNY", account_cfg)

    assert rows[0]["amount_cny"] == Decimal("242363.750000")
    assert rows[0]["as_of"] == date(2026, 6, 16)
    assert any("complete_account_snapshot" in sql for sql, _ in cur.executed)


def test_load_investment_positions_adds_synthetic_broker_cash_from_net_liquidation():
    cur = FakeCursor(
        [
            {"table_name": "pams_holdings_snapshot"},
            {"table_name": None},
            [
                {
                    "account_code": "tonghuashun",
                    "asset_liability": "asset",
                    "major_category": "A股",
                    "minor_category": "A股",
                    "symbol": "601899",
                    "asset_id": "manual-security-id",
                    "name": "Zijin",
                    "currency": "CNY",
                    "market_value": Decimal("331903"),
                    "cost": Decimal("359215.255"),
                    "holding_pnl": Decimal("-27312.255"),
                    "quantity": Decimal("1"),
                    "as_of": date(2026, 6, 4),
                }
            ],
            [],
        ]
    )
    account_cfg = {
        "accounts": {
            "tonghuashun": {
                "display_name": "Tonghuashun",
                "account_type": "brokerage",
                "base_currency": "CNY",
                "owner_ratio": "0.625",
                "manual_balance": {
                    "amount": "385539.56",
                    "currency": "CNY",
                    "as_of": date(2026, 6, 4),
                },
            }
        }
    }

    rows = load_investment_positions(cur, "CNY", account_cfg)

    security = next(row for row in rows if row["symbol"] == "601899")
    assert security["asset_id"] == "601899"
    assert security["holding_snapshot_asset_id"] == "manual-security-id"
    assert security["holding_snapshot_currency"] == "CNY"
    cash = next(row for row in rows if row["source_type"] == "synthetic_broker_cash")
    assert cash["account_code"] == "tonghuashun"
    assert cash["asset_type"] == "cash"
    assert cash["symbol"] == "CASH_CNY"
    assert cash["market_value_cny"] == Decimal("33522.85000")
    assert any("complete_account_snapshot" in sql for sql, _ in cur.executed)


def test_non_investment_account_does_not_enter_holding_projection():
    cur = FakeCursor([
        {"table_name": "pams_holdings_snapshot"}, {"table_name": None},
        [{"account_code": "cash_account", "asset_liability": "资产", "major_category": "美股",
          "minor_category": "美股基金", "asset_id": "MANUAL_TEST", "symbol": "TEST",
          "name": "Example", "currency": "CNY", "market_value": Decimal("70"),
          "cost": Decimal("60"), "holding_pnl": Decimal("10"), "as_of": date(2026, 6, 4)}], []])
    config = {"accounts": {"cash_account": {"account_type": "funding", "include_in_investments": False}}}
    assert load_investment_positions(cur, "CNY", config) == []


def test_load_investment_positions_adds_synthetic_broker_cash_from_account_balance():
    cur = FakeCursor(
        [
            {"table_name": "pams_holdings_snapshot"},
            {"table_name": None},
            [
                {
                    "account_code": "tonghuashun",
                    "asset_liability": "资产",
                    "major_category": "A股",
                    "minor_category": "A股",
                    "symbol": "601899",
                    "name": "Zijin",
                    "currency": "CNY",
                    "market_value": Decimal("100"),
                    "cost": Decimal("90"),
                    "holding_pnl": Decimal("10"),
                    "quantity": Decimal("1"),
                    "as_of": date(2026, 6, 17),
                }
            ],
            [],
        ]
    )
    account_cfg = {
        "accounts": {
            "tonghuashun": {
                "display_name": "Tonghuashun",
                "account_type": "brokerage",
                "base_currency": "CNY",
                "owner_ratio": "0.625",
            }
        }
    }

    rows = load_investment_positions(
        cur,
        "CNY",
        account_cfg,
        account_balances=[
            {
                "account_code": "tonghuashun",
                "amount_cny": Decimal("70"),
                "currency": "CNY",
                "as_of": date(2026, 6, 17),
                "include_in_nav": True,
            }
        ],
    )

    cash = next(row for row in rows if row["source_type"] == "synthetic_broker_cash")
    assert cash["market_value_cny"] == Decimal("7.500")


def test_load_investment_positions_skips_stale_synthetic_broker_cash():
    cur = FakeCursor(
        [
            {"table_name": "pams_holdings_snapshot"},
            {"table_name": None},
            [
                {
                    "account_code": "tonghuashun",
                    "asset_liability": "资产",
                    "major_category": "A股",
                    "minor_category": "A股",
                    "symbol": "601899",
                    "name": "Zijin",
                    "currency": "CNY",
                    "market_value": Decimal("387782"),
                    "cost": Decimal("350000"),
                    "holding_pnl": Decimal("37782"),
                    "quantity": Decimal("1"),
                    "as_of": date(2026, 6, 16),
                }
            ],
            [],
        ]
    )
    account_cfg = {
        "accounts": {
            "tonghuashun": {
                "display_name": "Tonghuashun",
                "account_type": "brokerage",
                "base_currency": "CNY",
                "owner_ratio": "0.625",
                "manual_balance": {
                    "amount": "367653.64",
                    "currency": "CNY",
                    "as_of": date(2026, 6, 9),
                },
            }
        }
    }

    rows = load_investment_positions(cur, "CNY", account_cfg)

    assert all(row["source_type"] != "synthetic_broker_cash" for row in rows)


def test_load_investment_positions_carries_forward_missing_broker_cash_rows():
    cur = FakeCursor(
        [
            {"table_name": "pams_holdings_snapshot"},
            {"table_name": "pams_fx_rates_daily"},
            [
                {"quote_currency": "USD", "rate": Decimal("7")},
            ],
            [
                {
                    "account_code": "ibkr",
                    "asset_liability": "资产",
                    "major_category": "美股/海外",
                    "minor_category": "美股个股",
                    "symbol": "MU",
                    "name": "Micron Technology",
                    "currency": "USD",
                    "market_value": Decimal("1000"),
                    "cost": Decimal("800"),
                    "holding_pnl": Decimal("200"),
                    "quantity": Decimal("10"),
                    "as_of": date(2026, 6, 24),
                }
            ],
            [
                {
                    "id": 1,
                    "account_code": "ibkr",
                    "asset_liability": "负债",
                    "major_category": "借款",
                    "minor_category": "融资负债",
                    "symbol": "CASH_USD",
                    "name": "Cash USD",
                    "currency": "USD",
                    "market_value": Decimal("-200"),
                    "cost": Decimal("-200"),
                    "holding_pnl": Decimal("0"),
                    "quantity": Decimal("-200"),
                    "as_of": date(2026, 6, 23),
                }
            ],
        ]
    )
    account_cfg = {
        "accounts": {
            "ibkr": {
                "display_name": "IBKR",
                "account_type": "brokerage",
                "base_currency": "USD",
            }
        }
    }

    rows = load_investment_positions(
        cur,
        "CNY",
        account_cfg,
        account_balances=[
            {
                "account_code": "ibkr",
                "amount_cny": Decimal("7000"),
                "currency": "CNY",
                "as_of": date(2026, 6, 24),
                "include_in_nav": True,
            }
        ],
    )

    cash = next(row for row in rows if row["symbol"] == "CASH_USD")
    assert cash["source_type"] == "holdings_snapshot_cash_carry_forward"
    assert cash["market_value_cny"] == Decimal("-1400")
    assert all(row["source_type"] != "synthetic_broker_cash" for row in rows)
