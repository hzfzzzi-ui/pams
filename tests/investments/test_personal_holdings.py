import json
from unittest import TestCase

from domain.investments.personal_holdings import personal_holdings


class PersonalHoldingsTest(TestCase):
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
