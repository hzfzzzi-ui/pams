"""Regression tests for Batch A: cross-endpoint valuation, debt, risk, benchmark, and THS rollforward parity."""
from datetime import date
from decimal import Decimal
from unittest import TestCase
from unittest.mock import patch

from domain.accounts.views.debt_and_attribution import (
    _esop_loan_from_cards,
    _ibkr_negative_cash_from_cards,
    build_networth_and_debts_payload,
)
from domain.investments.holdings.holdings_account_cards import merge_account_center_balances
from domain.investments.holdings.ths_trade_rollforward import (
    _cn_a_symbol_variants,
    ths_post_snapshot_delta_and_count,
)
from domain.investments.returns.benchmark_summary import summarize_open_holdings_leaderboard
from domain.risk.portfolio_config import build_portfolio_config_payload_from_inputs
from domain.risk.portfolio_risk_disclosure import _build_risk_diagnosis


SAMPLE_ACCOUNT_CARDS = {
    "ibkr": {
        "account_code": "ibkr",
        "net_value": {"value": "189858.99", "as_of": "2026-11-25"},
        "cash": {"value": "-186126.30", "as_of": "2026-11-25"},
        "leverage": {"value": "1.98"},
        "cash_by_currency": [
            {"currency": "MXN", "native_amount": "90000.00", "amount_base": "30621.15", "as_of": "2026-11-25"},
            {"currency": "KRW", "native_amount": "-35250119.46", "amount_base": "-174827.37", "as_of": "2026-11-25"},
            {"currency": "USD", "native_amount": "-6240.15", "amount_base": "-41601.18", "as_of": "2026-11-25"},
            {"currency": "SEK", "native_amount": "-489.53", "amount_base": "-318.91", "as_of": "2026-11-25"},
        ],
    },
    "investment_overview": {
        "net_value": {"value": "837050.86"},
        "cumulative_pnl": {"value": "-196759.20"},
        "cash": {"value": "32307.43"},
        "financing": {"value": "526747.46"},
        "financing_items": [
            {"account_code": "huawei_stock", "account_name": "华为 ESOP", "kind": "loan", "name": "华为 ESOP 公司内部借款", "amount_cny": "310000.00"},
            {"account_code": "ibkr", "account_name": "IBKR", "kind": "negative_cash", "name": "IBKR · KRW 负现金", "amount_cny": "174827.37", "currency": "KRW"},
            {"account_code": "ibkr", "account_name": "IBKR", "kind": "negative_cash", "name": "IBKR · USD 负现金", "amount_cny": "41601.18", "currency": "USD"},
            {"account_code": "ibkr", "account_name": "IBKR", "kind": "negative_cash", "name": "IBKR · SEK 负现金", "amount_cny": "318.91", "currency": "SEK"},
        ],
    },
}


class DebtAndRiskParityTest(TestCase):
    def test_ibkr_negative_cash_and_esop_loan_extraction_from_cards(self):
        self.assertAlmostEqual(_ibkr_negative_cash_from_cards(SAMPLE_ACCOUNT_CARDS), 216747.46, places=2)
        self.assertAlmostEqual(_esop_loan_from_cards(SAMPLE_ACCOUNT_CARDS), 310000.00, places=2)

    def test_risk_diagnosis_includes_esop_debt_and_ibkr_negative_currencies(self):
        lookthrough = {
            "total_market_value_cny": 1331465.12,
            "normalized_holdings": [
                {"symbol": "HUAWEI_ESOP", "name": "ESOP", "assetType": "非上市股权", "currency": "CNY", "marketValue": 706500.0},
                {"symbol": "NVDA", "name": "NVIDIA", "assetType": "美股个股", "currency": "USD", "marketValue": 624965.12},
            ],
        }
        diag = _build_risk_diagnosis({}, lookthrough, account_cards=SAMPLE_ACCOUNT_CARDS)
        self.assertAlmostEqual(diag["all"]["ibkr_debt_cny"], 216747.46, places=2)
        self.assertAlmostEqual(diag["all"]["esop_debt_cny"], 310000.00, places=2)
        self.assertAlmostEqual(diag["all"]["total_debt_cny"], 526747.46, places=2)
        self.assertAlmostEqual(diag["ex_esop"]["total_debt_cny"], 216747.46, places=2)
        self.assertGreater(diag["all"]["estimated_annual_interest_cny"], diag["ex_esop"]["estimated_annual_interest_cny"])

    def test_portfolio_config_financing_uses_account_cards_when_risk_neg_cash_missing(self):
        risk = {
            "as_of": "2026-11-26",
            "summary": {"total_net_worth_cny": 1331465.12},
            "cash_balances": [],
        }
        lookthrough = {
            "as_of": "2026-11-26",
            "total_market_value_cny": 1331465.12,
            "normalized_holdings": [],
            "dimensions": {},
        }
        cfg = build_portfolio_config_payload_from_inputs(
            risk, lookthrough, base_currency="CNY", account_cards=SAMPLE_ACCOUNT_CARDS
        )
        debt_rule = next(r for r in cfg["rules"] if r["id"] == "debt")
        self.assertAlmostEqual(abs(debt_rule["current_amount_cny"]), 526747.46, places=2)
        self.assertGreater(debt_rule["current"], 0.35)
        self.assertAlmostEqual(cfg["incremental_rebalance"]["ibkr_debt_cny"], 216747.46, places=2)

    def test_networth_and_debts_refund_reduces_expense_and_reads_investment_overview(self):
        fake_cash_rows = [
            {"account_code": "ccb_7332", "source_evidence_status": "verified", "cashflow_type": "operating_income", "currency": "CNY", "amount_cny": Decimal("1000.00")},
            {"account_code": "ccb_7332", "source_evidence_status": "verified", "cashflow_type": "operating_expense", "currency": "CNY", "amount_cny": Decimal("-300.00")},
            # Refund on operating_expense must REDUCE monthly expense, not add to it.
            {"account_code": "ccb_7332", "source_evidence_status": "verified", "cashflow_type": "operating_expense", "currency": "CNY", "amount_cny": Decimal("50.00")},
            # Unverified row must be skipped.
            {"account_code": "ccb_7332", "source_evidence_status": "unknown", "cashflow_type": "operating_expense", "currency": "CNY", "amount_cny": Decimal("-999.00")},
        ]

        class _FakeCur:
            def execute(self, *_args, **_kwargs):
                pass
            def fetchall(self):
                return []

        with patch("domain.accounts.views.debt_and_attribution.read_page", return_value={"data": SAMPLE_ACCOUNT_CARDS}), \
             patch("domain.accounts.views.debt_and_attribution._load_accounts_config", return_value={}), \
             patch("domain.ledger.cashflow.cashflow_read_v2.load_cash_rows", return_value=fake_cash_rows):
            payload = build_networth_and_debts_payload(_FakeCur(), today=date(2026, 11, 27))

        self.assertAlmostEqual(payload["attribution"]["monthly_income_cny"], 1000.0, places=2)
        self.assertAlmostEqual(payload["attribution"]["monthly_expense_cny"], 250.0, places=2)
        self.assertAlmostEqual(payload["attribution"]["monthly_savings_cny"], 750.0, places=2)
        self.assertAlmostEqual(payload["attribution"]["investment_net_value_cny"], 837050.86, places=2)
        self.assertAlmostEqual(payload["attribution"]["investment_pnl_cny"], -196759.20, places=2)
        self.assertAlmostEqual(payload["debts"]["total_debt_cny"], 526747.46, places=2)


class AccountCenterMergeAndThsEdgeTest(TestCase):
    def test_merge_account_center_balances_upgrades_stale_ths_and_preserves_ibkr_usd(self):
        book = {
            "tonghuashun": {"balance_as_of": "2026-09-18", "balance_amount": "305860.23", "balance_currency": "CNY", "personal_contribution": None},
            "ibkr": {"balance_as_of": "2026-11-25", "balance_amount": "28478.71", "balance_currency": "USD", "personal_contribution": "189785.88"},
        }
        cards = {
            "tonghuashun": {"balance_as_of": "2026-11-26", "balance_amount": "154543.47", "balance_currency": "CNY", "balance_label": "组合估值"},
            "ibkr": {"balance_as_of": "2026-11-25", "balance_amount": "189858.99", "balance_currency": "CNY", "balance_label": "组合价值"},
        }
        merged = merge_account_center_balances(book, cards)
        self.assertEqual(merged["tonghuashun"]["balance_amount"], "154543.47")
        self.assertEqual(merged["tonghuashun"]["balance_as_of"], "2026-11-26")
        self.assertEqual(merged["tonghuashun"]["personal_contribution"], "154543.47")
        # IBKR retains native USD display while aligning personal_contribution to official close CNY.
        self.assertEqual(merged["ibkr"]["balance_currency"], "USD")
        self.assertEqual(merged["ibkr"]["balance_amount"], "28478.71")
        self.assertEqual(merged["ibkr"]["personal_contribution"], "189858.99")

    def test_cn_a_symbol_variants_handle_shanghai_etfs_and_prefixes(self):
        self.assertEqual(_cn_a_symbol_variants("510300"), ("510300", "sh510300"))
        self.assertEqual(_cn_a_symbol_variants("601899.SH"), ("601899", "sh601899"))
        self.assertEqual(_cn_a_symbol_variants("sh518880"), ("518880", "sh518880"))
        self.assertEqual(_cn_a_symbol_variants("002281"), ("002281", "sz002281"))

    def test_ths_position_delta_fails_closed_on_new_symbol_and_tracks_full_close(self):
        new_symbol_trade = [{
            "trade_id": 1, "trade_date": date(2026, 11, 20), "trade_type": "buy",
            "symbol": "510300", "currency": "CNY", "quantity": Decimal("1000"),
            "source_evidence_status": "verified",
        }]
        with patch("domain.investments.holdings.ths_trade_rollforward._table_exists", return_value=True), \
             patch("domain.investments.holdings.ths_trade_rollforward.load_trade_rows", return_value=new_symbol_trade):
            res = ths_post_snapshot_delta_and_count(
                None, snap_day=date(2026, 11, 17), as_of=date(2026, 11, 26),
                snapshot_quantities={"601899": Decimal("3000")},
            )
        self.assertIsNone(res)

        full_close_trade = [{
            "trade_id": 2, "trade_date": date(2026, 11, 20), "trade_type": "sell",
            "symbol": "000735", "currency": "CNY", "quantity": Decimal("400"),
            "source_evidence_status": "verified",
        }]
        with patch("domain.investments.holdings.ths_trade_rollforward._table_exists", return_value=True), \
             patch("domain.investments.holdings.ths_trade_rollforward.load_trade_rows", return_value=full_close_trade), \
             patch("domain.investments.holdings.ths_trade_rollforward._latest_cn_a_price", return_value=Decimal("5.00")):
            res = ths_post_snapshot_delta_and_count(
                None, snap_day=date(2026, 11, 17), as_of=date(2026, 11, 26),
                snapshot_quantities={"601899": Decimal("3000"), "000735": Decimal("400")},
            )
        self.assertEqual(res, (Decimal("-2000.00"), 1))


class BenchmarkSummaryParityTest(TestCase):
    def test_uses_floating_pnl_populates_account_and_cleans_duplicate_symbol(self):
        rows = [
            {
                "symbol": "601899", "name": "紫金矿业", "account": "tonghuashun",
                "currency": "CNY", "major_category": "A股",
                "market_value_cny": 62965.12, "cost_cny": 72992.22,
                "floating_pnl_cny": -10027.10, "holding_pnl_cny": -35868.96,
            },
            {
                "symbol": "005930.KS", "name": "005930.KS - SAMSUNG ELECTRONICS CO LTD", "account": "ibkr",
                "currency": "KRW", "major_category": "韩股",
                "market_value_cny": 127361.89, "cost_cny": 145000.00,
                "floating_pnl_cny": -17638.11, "holding_pnl_cny": -51639.75,
            },
            # Missing CNY conversion for non-CNY row must be skipped rather than mixing raw USD as CNY.
            {
                "symbol": "NVDA", "name": "NVIDIA", "account": "ibkr",
                "currency": "USD", "major_category": "美股",
                "market_value": 10000.0, "market_value_cny": None, "cost": 8000.0, "cost_cny": None,
            },
        ]
        closed_items = [
            # Fully closed winner must appear in winners leaderboard
            {
                "account_code": "tencent_licaitong", "symbol": "011613", "name": "华夏科创50ETF联接C",
                "cycles": [{"cost": "38000", "trade_pnl": "12641.96", "distributions": "0"}],
            },
            # Closed cycle for a currently open symbol (601899) must NOT be double-counted
            {
                "account_code": "tonghuashun", "symbol": "601899", "name": "紫金矿业",
                "cycles": [{"cost": "50000", "trade_pnl": "-20000", "distributions": "0"}],
            },
        ]
        res = summarize_open_holdings_leaderboard(rows, closed_items=closed_items)
        # Total PnL = -35868.96 (601899) + -51639.75 (005930.KS) + 12641.96 (closed 011613)
        self.assertAlmostEqual(res["portfolio"]["ex_esop_pnl_cny"], -74866.75, places=2)
        self.assertAlmostEqual(res["portfolio"]["ex_esop_market_value_cny"], 190327.01, places=2)
        self.assertEqual([w["symbol"] for w in res["leaderboard"]["winners"]], ["011613"])
        self.assertTrue(res["leaderboard"]["winners"][0]["is_closed"])
        by_sym = {x["symbol"]: x for x in res["leaderboard"]["losers"]}
        self.assertEqual(by_sym["601899"]["account"], "同花顺")
        self.assertAlmostEqual(by_sym["601899"]["pnl_cny"], -35868.96, places=2)
        self.assertEqual(by_sym["005930.KS"]["name"], "SAMSUNG ELECTRONICS CO LTD")
        self.assertEqual(by_sym["005930.KS"]["account"], "盈透证券")
