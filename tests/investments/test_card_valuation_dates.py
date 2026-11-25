"""Quoted personal holdings and separately reconciled cash on different dates."""
from unittest import TestCase

from domain.investments.holdings.holdings_account_cards import account_center_balances, build_holdings_account_cards


class ValuationDatesTest(TestCase):
    def build(self, cash, market, holdings_count=1):
        positions = {"by_account": [{"account_code": "tonghuashun", "holdings_count": holdings_count, "portfolio_value": {
            "status": "cash_carried_forward", "as_of": "2026-03-02", "amount_base": "12",
            "cash_base": cash, "cash_as_of": "2026-02-26",
        }}]}
        stock = {"as_of": "2026-03-03", "rows": [{"account": "tonghuashun", "currency": "CNY",
                  "market_value": "20", "owner_ratio": "0.5", "market_value_cny": market}]}
        cards = build_holdings_account_cards(positions=positions, overview={}, stock_performance={}, stock=stock,
                                            funds={}, overseas={}, closed_returns={})
        return cards["tonghuashun"]

    def test_new_quote_keeps_cash_and_personal_scope(self):
        for cash, net in [("3", "13"), ("0", "10")]:
            card = self.build(cash, "10")
            self.assertEqual((card["net_value"]["value"], card["net_value"]["as_of"]), (net, "2026-03-03"))
            self.assertEqual(card["cash"]["as_of"], "2026-02-26")
            self.assertEqual(card["net_value"]["status"], "estimated")

    def test_incomplete_evidence_preserves_dated_value(self):
        for cash, market in [(None, "10"), ("3", None)]:
            card = self.build(cash, market)
            self.assertEqual(card["net_value"]["as_of"], "2026-03-02")
            self.assertEqual(card["net_value"]["value"], "12")

    def test_partial_position_list_cannot_replace_account_net(self):
        card = self.build("3", "10", holdings_count=2)
        self.assertEqual(card["net_value"]["as_of"], "2026-03-02")

    def test_fund_balance_is_scoped_to_current_rows(self):
        def holding(code, amount, **extra):
            return dict(account_code=code, market_value_cny=amount, snapshot_as_of="2026-03-03", **extra)
        rows = [holding("a", "4"), holding("a", "6"), holding("b", "20"),
                holding("a", "100", is_closed=True), holding("a", "100", reference_as_of="2025-12-31"),
                holding("c", None)]
        balances = account_center_balances({}, {"rows": rows}, {})
        self.assertEqual({k: v["balance_amount"] for k, v in balances.items()}, {"a": "10", "b": "20"})
