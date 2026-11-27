"""Regression tests for THS post-snapshot rollforward, IBKR multi-currency cash, and cash_management rows."""
from datetime import date
from decimal import Decimal
from unittest import TestCase

from domain.investments.holdings.ths_trade_rollforward import (
    merge_cash_management_holding_rows,
    project_ths_post_snapshot_rows,
)
from domain.investments.valuation.account_portfolio_value import extract_ibkr_cash_by_currency
from domain.investments.valuation.investment_overview import holdings_overview_card


class ThsTradeRollforwardTest(TestCase):
    def test_post_snapshot_buys_and_dividend_preserve_net_worth_identity(self):
        rows = [
            {
                "account": "tonghuashun",
                "symbol": "601899",
                "name": "紫金矿业",
                "asset_liability": "资产",
                "major_category": "A股",
                "minor_category": "A股",
                "currency": "CNY",
                "as_of": date(2026, 11, 17),
                "snapshot_as_of": date(2026, 11, 26),
                "quantity": Decimal("3000"),
                "cost": Decimal("85431.9851"),
                "cost_cny": Decimal("85431.9851"),
                "buy_price": Decimal("28.4773"),
                "current_price": Decimal("23.75"),
                "previous_price": Decimal("24.00"),
                "market_value": Decimal("71250.00"),
                "market_value_native": Decimal("71250.00"),
                "market_value_cny": Decimal("71250.00"),
                "holding_pnl": Decimal("-14181.9851"),
                "holding_pnl_native": Decimal("-14181.9851"),
                "holding_pnl_cny": Decimal("-14181.9851"),
                "daily_pnl": Decimal("-750.00"),
                "daily_pnl_native": Decimal("-750.00"),
                "daily_pnl_cny": Decimal("-750.00"),
                "daily_total_pnl_cny": Decimal("-750.00"),
            },
            {
                "account": "tonghuashun",
                "symbol": "002281",
                "name": "光迅科技",
                "asset_liability": "资产",
                "major_category": "A股",
                "minor_category": "A股",
                "currency": "CNY",
                "as_of": date(2026, 11, 17),
                "snapshot_as_of": date(2026, 11, 26),
                "quantity": Decimal("200"),
                "cost": Decimal("46558.63"),
                "cost_cny": Decimal("46558.63"),
                "current_price": Decimal("79.14"),
                "previous_price": Decimal("80.00"),
                "market_value": Decimal("15828.00"),
                "market_value_native": Decimal("15828.00"),
                "market_value_cny": Decimal("15828.00"),
                "diluted_cost_source": "verified_trade_snapshot",
                "realized_trade_pnl": Decimal("0"),
                "net_distributions": Decimal("100"),
                "net_distributions_cny": Decimal("100"),
            },
        ]
        trades = [
            {
                "trade_id": 698,
                "trade_date": date(2026, 11, 18),
                "trade_type": "dividend",
                "symbol": "002281",
                "currency": "CNY",
                "net_cash": Decimal("74.00"),
                "source_evidence_status": "verified",
            },
            {
                "trade_id": 697,
                "trade_date": date(2026, 11, 19),
                "trade_type": "buy",
                "symbol": "601899",
                "currency": "CNY",
                "quantity": Decimal("500"),
                "gross_amount": Decimal("11860.00"),
                "net_cash": Decimal("-11865.12"),
                "source_evidence_status": "verified",
            },
            {
                "trade_id": 699,
                "trade_date": date(2026, 11, 24),
                "trade_type": "buy",
                "symbol": "601899",
                "currency": "CNY",
                "quantity": Decimal("300"),
                "gross_amount": Decimal("7320.00"),
                "net_cash": Decimal("-7325.07"),
                "source_evidence_status": "verified",
            },
        ]
        projected = project_ths_post_snapshot_rows(rows, trades, view_as_of=date(2026, 11, 26))
        zj, gx = projected[0], projected[1]
        self.assertEqual(zj["quantity"], Decimal("3800"))
        self.assertEqual(zj["cost"], Decimal("104622.1751"))
        self.assertEqual(zj["market_value"], Decimal("90250.00"))
        self.assertEqual(zj["daily_pnl"], Decimal("-950.00"))
        self.assertTrue(zj["projected_from_trades"])
        self.assertEqual(zj["projected_trade_as_of"], "2026-11-24")
        self.assertEqual(gx["net_distributions"], Decimal("174.00"))

        # Net-worth conservation: (mv_after + cash_after) - (mv_before + cash_before)
        # == dividend + trade_pnl_since_execution - fees
        cash_before = Decimal("20118.49")
        cash_after = cash_before + Decimal("74.00") - Decimal("11865.12") - Decimal("7325.07")
        self.assertEqual(cash_after, Decimal("1002.30"))
        delta_net = (zj["market_value"] + cash_after) - (Decimal("71250.00") + cash_before)
        expected_delta = (
            Decimal("74.00")
            + Decimal("500") * (Decimal("23.75") - Decimal("23.72")) - Decimal("5.12")
            + Decimal("300") * (Decimal("23.75") - Decimal("24.40")) - Decimal("5.07")
        )
        self.assertEqual(delta_net, expected_delta)

    def test_unverified_post_snapshot_trade_fails_closed(self):
        rows = [{
            "account": "tonghuashun", "symbol": "601899", "asset_liability": "资产",
            "major_category": "A股", "currency": "CNY", "as_of": date(2026, 11, 17),
            "quantity": Decimal("3000"), "cost": Decimal("85000"), "current_price": Decimal("24"),
            "market_value": Decimal("72000"),
        }]
        trades = [{
            "trade_id": 700, "trade_date": date(2026, 11, 24), "trade_type": "buy",
            "symbol": "601899", "currency": "CNY", "quantity": Decimal("100"),
            "net_cash": Decimal("-2400"), "source_evidence_status": "unverified",
        }]
        out = project_ths_post_snapshot_rows(rows, trades, view_as_of=date(2026, 11, 26))
        self.assertEqual(out[0]["quantity"], Decimal("3000"))
        self.assertNotIn("projected_from_trades", out[0])


class IbkrMultiCurrencyCashTest(TestCase):
    SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
    <FlexQueryResponse>
      <FlexStatements>
        <FlexStatement accountId="U999" fromDate="20261125" toDate="20261125">
          <AccountInformation accountId="U999" currency="USD" />
          <CashReport>
            <CashReportCurrency accountId="U999" currency="BASE_SUMMARY" levelOfDetail="BaseCurrency" fromDate="20261125" toDate="20261125" endingCash="-27918.80" />
            <CashReportCurrency accountId="U999" currency="MXN" levelOfDetail="Currency" fromDate="20261125" toDate="20261125" endingCash="90000.00" />
            <CashReportCurrency accountId="U999" currency="USD" levelOfDetail="Currency" fromDate="20261125" toDate="20261125" endingCash="-6240.15" />
            <CashReportCurrency accountId="U999" currency="KRW" levelOfDetail="Currency" fromDate="20261125" toDate="20261125" endingCash="-35250119.46" />
            <CashReportCurrency accountId="U999" currency="HKD" levelOfDetail="Currency" fromDate="20261125" toDate="20261125" endingCash="0.000009" />
          </CashReport>
          <ConversionRates>
            <ConversionRate reportDate="20261125" fromCurrency="MXN" toCurrency="USD" rate="0.051018" />
            <ConversionRate reportDate="20261125" fromCurrency="KRW" toCurrency="USD" rate="0.00074376" />
            <ConversionRate reportDate="20261125" fromCurrency="HKD" toCurrency="USD" rate="0.12757" />
          </ConversionRates>
        </FlexStatement>
      </FlexStatements>
    </FlexQueryResponse>"""

    def test_extracts_non_zero_currencies_and_splits_positive_negative_in_overview(self):
        items = extract_ibkr_cash_by_currency(
            self.SAMPLE_XML,
            account_id="U999",
            as_of=date(2026, 11, 25),
            base_currency="USD",
            fx_rate=Decimal("6.6667"),
            owner_ratio=Decimal("1"),
        )
        self.assertIsNotNone(items)
        # HKD dust (< 0.01 CNY) is omitted; MXN positive comes first, then KRW and USD negatives.
        self.assertEqual([i["currency"] for i in items], ["MXN", "KRW", "USD"])
        self.assertEqual(items[0]["native_amount"], "90000.00")
        self.assertTrue(Decimal(items[0]["amount_base"]) > 0)
        self.assertTrue(Decimal(items[1]["amount_base"]) < 0)
        self.assertTrue(Decimal(items[2]["amount_base"]) < 0)

        page = {
            "holdings": {
                "rows": [
                    {"account": "ibkr", "position_group": "股票", "market_value_cny": "375959.53", "holding_pnl_cny": "0"},
                    {"account": "wechat_lqt", "position_group": "现金/活期", "market_value_cny": "986.80", "balance_as_of": "2026-11-23"},
                    {"account": "tencent_licaitong_current_plus", "position_group": "现金/活期", "market_value_cny": "0.20", "balance_as_of": "2026-11-14"},
                ]
            },
            "account_cards": {
                "ibkr": {
                    "account_code": "ibkr",
                    "net_value": {"value": "189858.99", "basis": "official_close", "as_of": "2026-11-25"},
                    "market_value": {"value": "375959.53", "as_of": "2026-11-25"},
                    "cash": {"value": "-186126.30", "as_of": "2026-11-25"},
                    "cash_by_currency": items,
                    "leverage": {"value": "1.98"},
                }
            },
            "accounts": {
                "overview": {
                    "accounts": [
                        {"account_code": "ibkr", "display_name": "IBKR", "account_type": "brokerage"},
                        {"account_code": "wechat_lqt", "display_name": "微信零钱通", "account_type": "cash_management"},
                        {"account_code": "tencent_licaitong_current_plus", "display_name": "理财通活期+", "account_type": "cash_management"},
                    ]
                }
            },
        }
        overview = holdings_overview_card(page, {})
        expected_pos = Decimal(items[0]["amount_base"]) + Decimal("986.80") + Decimal("0.20")
        expected_neg = abs(Decimal(items[1]["amount_base"])) + abs(Decimal(items[2]["amount_base"]))
        self.assertEqual(Decimal(overview["cash"]["value"]), expected_pos)
        self.assertEqual(Decimal(overview["financing"]["value"]), expected_neg)
        # Official NAV of IBKR + cash management accounts stays exact.
        self.assertEqual(Decimal(overview["net_value"]["value"]), Decimal("189858.99") + Decimal("986.80") + Decimal("0.20"))
        self.assertEqual(
            {(f["currency"], f["native_amount"]) for f in overview["financing_items"]},
            {("KRW", "-35250119.46"), ("USD", "-6240.15")},
        )


class CashManagementMergeTest(TestCase):
    def test_merges_wechat_lqt_and_current_plus_with_latest_balances(self):
        holdings = {
            "rows": [
                {
                    "account": "tencent_licaitong_current_plus",
                    "name": "Cash CNY",
                    "symbol": "CASH_CNY",
                    "major_category": "现金",
                    "minor_category": "现金/活期",
                    "currency": "CNY",
                    "market_value": "0.20",
                    "market_value_cny": "0.20",
                    "as_of": "2026-11-14",
                }
            ]
        }
        cfg = {
            "accounts": {
                "tencent_licaitong_current_plus": {"display_name": "理财通活期+", "account_type": "cash_management", "base_currency": "CNY"},
                "wechat_lqt": {"display_name": "微信零钱通", "account_type": "cash_management", "base_currency": "CNY"},
                "ccb_7332": {"display_name": "建行7332", "account_type": "funding", "base_currency": "CNY"},
            }
        }
        balances = {
            "tencent_licaitong_current_plus": {"balance_amount": "0.2000", "balance_as_of": "2026-11-14", "balance_currency": "CNY", "personal_contribution": "0.20000"},
            "wechat_lqt": {"balance_amount": "986.8000", "balance_as_of": "2026-11-23", "balance_currency": "CNY", "personal_contribution": "986.80000"},
            "ccb_7332": {"balance_amount": "18971.5800", "balance_as_of": "2026-11-01", "balance_currency": "CNY"},
        }
        merged = merge_cash_management_holding_rows(
            holdings, account_config=cfg, account_balances=balances,
            excluded_accounts=set(), owner_ratio=lambda _: Decimal("1"),
        )
        by_acct = {r["account"]: r for r in merged["rows"]}
        self.assertEqual(set(by_acct), {"tencent_licaitong_current_plus", "wechat_lqt"})
        self.assertEqual(by_acct["wechat_lqt"]["name"], "微信零钱通")
        self.assertEqual(by_acct["wechat_lqt"]["market_value_cny"], "986.80000")
        self.assertEqual(by_acct["wechat_lqt"]["balance_as_of"], "2026-11-23")
        self.assertTrue(by_acct["wechat_lqt"]["is_cash_management"])
        self.assertEqual(by_acct["tencent_licaitong_current_plus"]["name"], "理财通活期+")
        self.assertTrue(by_acct["tencent_licaitong_current_plus"]["is_cash_management"])
