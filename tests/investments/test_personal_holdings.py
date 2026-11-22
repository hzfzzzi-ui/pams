import json
from unittest import TestCase

from domain.investments.personal_holdings import personal_holdings


class PersonalHoldingsTest(TestCase):
    def test_dedicated_loan_changes_net_value_only_once(self):
        configs = {"equity": {"latest_position_evidence": "recorded_equity_and_debt"}}
        rows = [
            dict(account="equity", major_category="ESOP", market_value_cny=120, cost_cny=100,
                 snapshot_scope_member="recorded", holding_pnl_cny=20),
            dict(account="equity", major_category="负债", market_value_cny=40, snapshot_scope_member="recorded"),
            dict(account="broker", major_category="美股", market_value_cny=80),
            dict(account="broker", major_category="负债", market_value_cny=10),
        ]
        equity, _, stock, _ = personal_holdings({"rows": rows}, {}, accounts=configs)["rows"]
        self.assertEqual((equity["position_net_value_cny"], equity["portfolio_weight"], equity["cumulative_pnl_ratio"]),
                         (80, 0.6, 0.2))
        self.assertEqual(stock["position_net_value_cny"], 80)
        self.assertIsNone(stock["cumulative_pnl_ratio"])
        rows[1]["snapshot_scope_member"] = "unverified"
        self.assertIsNone(personal_holdings({"rows": rows}, {}, accounts=configs)["rows"][0]["position_net_value_cny"])

    def test_employee_equity_shows_confirmed_annual_income_over_personal_cost(self):
        equity = dict(account="equity", major_category="员工持股", symbol="E", currency="CNY",
                      market_value_cny=100, cost_cny=100, annual_dividend_income_cny=5,
                      realized_income_status="confirmed", holding_pnl_cny=12)
        second = dict(equity, account="joint", owner_ratio="0.5", market_value_cny=50, cost_cny=50,
                      annual_dividend_income_cny=4)
        rows = personal_holdings({"rows": [equity, second, dict(equity, major_category="股票")]}, {})["rows"]
        self.assertEqual(rows[0]["floating_pnl_cny"], 0)
        self.assertEqual(rows[0]["position_dividend_return"], {"amount_cny": "5", "yield_on_cost": "0.05"})
        self.assertEqual(rows[0]["combined_position"]["position_dividend_return"], {"amount_cny": "9", "yield_on_cost": "0.06"})
        self.assertIsNone(rows[2]["position_dividend_return"])
        second.update(realized_income_status="partial")
        equity.update(cost_cny=0)
        rows = personal_holdings({"rows": [equity, second]}, {})["rows"]
        self.assertEqual(rows[0]["position_dividend_return"], {"amount_cny": "5", "yield_on_cost": None})
        self.assertEqual(rows[0]["combined_position"]["position_dividend_return"], {"amount_cny": None, "yield_on_cost": None})

    def test_floating_return_uses_remaining_cost_and_personal_value(self):
        rows = [dict(account="pooled", symbol="SEC", major_category="A股", market_value_cny=80,
                     cost_cny=100, holding_pnl_cny=-60, owner_ratio="0.5"),
                dict(account="second", symbol="SEC", major_category="A股", market_value_cny=50, cost_cny=0)]
        first, zero = personal_holdings({"rows": rows}, {})["rows"]
        self.assertEqual((first["floating_pnl_cny"], first["floating_pnl_ratio"]), (-20, -0.2))
        self.assertIsNone(zero["floating_pnl_ratio"])
        self.assertEqual(first["combined_position"]["floating_pnl_ratio"], 0.3)

    def test_owner_value_is_already_allocated_and_cash_debt_closed_are_excluded(self):
        rows = [
            dict(major_category="A股", market_value_cny="30", owner_ratio="0.3"),
            dict(major_category="美股", symbol="ETF", market_value_cny="70"),
            dict(major_category="负债", market_value_cny="40"),
            dict(major_category="现金", market_value_cny="10"),
            dict(major_category="港股", is_closed=True, market_value_cny="1"),
            dict(major_category="贵金属", market_value_cny=None),
        ]
        result = personal_holdings({"rows": rows}, {"ETF": "etf_us"})
        self.assertEqual(result["personal_summary"], {"market_value_cny": 100, "missing_values": 1})
        self.assertEqual([r["portfolio_weight"] for r in result["rows"]],
                         [0.3, 0.7, None, None, None, None])
        self.assertEqual(result["rows"][1]["position_segment"], "场内")

    def test_one_instrument_in_two_accounts_keeps_both_details(self):
        rows = [dict(holding_id=i, account=str(i), symbol="TEST", currency="CNY",
                     major_category="A股", minor_category="A股基金", market_value_cny=str(i))
                for i in (1, 2)]
        result = personal_holdings({"rows": rows}, {})
        json.dumps(result, allow_nan=False)
        first, second = result["rows"]
        self.assertEqual(first["combined_key"], second["combined_key"])
        self.assertEqual(first["combined_position"]["market_value_cny"], 3)
        self.assertAlmostEqual(first["combined_position"]["portfolio_weight"], 1)
        self.assertIsNone(first["combined_position"]["holding_pnl_cny"])
