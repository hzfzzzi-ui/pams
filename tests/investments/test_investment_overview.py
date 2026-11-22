from decimal import Decimal
from unittest import TestCase

from domain.investments.investment_overview import holdings_overview_card


def holding(account, group, value, daily=None, day="2026-11-19", **extra):
    return dict(account=account, position_group=group, market_value_cny=value, holding_pnl_cny="0",
                daily_total_pnl_cny=daily, daily_return_status="confirmed",
                daily_return_status_as_of=day, **extra)


class InvestmentOverviewTest(TestCase):
    def card(self, rows, cards=None):
        kinds = {"joint": "brokerage", "fund": "fund_platform", "cash": "cash_management",
                 "equity": "brokerage", "bank": "funding"}
        accounts = [{"account_code": code, "account_type": kind} for code, kind in kinds.items()]
        page = {"holdings": {"rows": rows}, "account_cards": cards or {},
                "accounts": {"overview": {"accounts": accounts}}}
        return holdings_overview_card(page, {})

    def test_personal_scope_official_close_and_no_double_counting(self):
        cards = {"joint": {"account_code": "joint", "net_value": {"value": "75", "basis": "official_close"},
                            "market_value": {"value": "100"}, "cash": {"value": "-26"},
                            "leverage": {"value": "1.4"}},
                 "fund": {"account_code": "fund", "cash": {"value": "2"}},
                 "section": {"cash": {"value": "2"}}}
        rows = [holding("joint", "股票", 98), holding("equity", "员工持股", 50),
                holding("equity", "负债", 10), holding("fund", "基金", 13),
                holding("cash", "现金/活期", 2), holding("bank", "现金/活期", 123),
                holding("fund", "基金", 100, is_closed=True),
                holding("fund", "基金", 100, reference_as_of="2025-12-31")]
        result = self.card(rows, cards)
        expected = {"net_value": 130, "market_value": 163, "cash": 2, "financing": 36,
                    "leverage": Decimal(168) / 130}
        for key, value in expected.items():
            self.assertEqual(Decimal(result[key]["value"]), value, key)

        self.assertEqual(Decimal(result["account_card_net_shares"]["joint"]["value"]), Decimal(75) / 130)
        self.assertEqual(Decimal(result["group_net_shares"]["员工持股"]["value"]), Decimal(40) / 130)
        by_code = {a["account_code"]: a for a in result["account_returns"]}
        self.assertEqual(by_code["joint"]["net_value"]["value"], "75")
        self.assertEqual(by_code["joint"]["cash"]["value"], "-26")
        self.assertTrue(by_code["cash"]["cash_only"])
        items = result["financing_items"]
        self.assertEqual({(i["account_code"], i["kind"], Decimal(i["amount_cny"])) for i in items},
                         {("equity", "loan", Decimal(10)), ("joint", "negative_cash", Decimal(26))})
        self.assertEqual(sum(Decimal(i["share"]) for i in items), 1)

    def test_fund_card_uses_included_net_once_and_rejects_a_stale_display(self):
        fund = holding("fund", "基金", 40)
        cash_code = "tencent_licaitong_current_plus"
        card = {"net_value": {"value": "42"}}
        page = {"holdings": {"rows": [fund, holding(cash_code, "现金/活期", 2), holding("broker", "股票", 58)]},
                "funds": {"rows": [fund, dict(fund, account="old", is_closed=True)]},
                "account_cards": {"fund_overview": card}, "accounts": {"overview": {"accounts": [
                    {"account_code": code, "account_type": kind} for code, kind in
                    [("fund", "fund_platform"), (cash_code, "cash_management"), ("broker", "brokerage")]
                ]}}}
        for amount, expected in [("42", "0.42"), ("43", None), (None, None)]:
            card["net_value"]["value"] = amount
            self.assertEqual(holdings_overview_card(page, {})["account_card_net_shares"]["fund_overview"]["value"],
                             expected)

    def test_known_loan_survives_unknown_cash_without_guessing_a_share(self):
        rows = [holding("joint", "负债", -12, name="分期本金")]
        result = self.card(rows, {"joint": {"account_code": "joint"}})
        self.assertEqual((result["financing"]["value"], result["financing"]["status"]), ("12", "partial"))
        self.assertEqual([(i["amount_cny"], i["share"]) for i in result["financing_items"]],
                         [("12", None), (None, None)])
        self.assertIsNone(result["net_value"]["value"])
        self.assertIsNone(result["account_card_net_shares"]["joint"]["value"])

    def test_same_day_partial_and_no_account_plus_rows_double_count(self):
        rows = [holding("joint", "股票", 20, -3), holding("fund", "基金", 50, 1),
                holding("fund", "基金", 40, 7, "2026-11-18"), holding("equity", "员工持股", 30)]
        cards = {"joint": {"account_code": "joint", "cash": {"value": "0"},
                           "recent_daily_pnl": {"value": "-3", "as_of": "2026-11-19", "basis": "holding_daily"}},
                 "equity": {"account_code": "equity", "cash": {"value": "0"},
                            "recent_daily_pnl": {"value": "2", "as_of": "2026-11-19", "basis": "account_daily"}}}
        result = self.card(rows, cards)["recent_daily_pnl"]
        self.assertEqual((result["as_of"], result["value"], result["status"]), ("2026-11-19", "0", "partial"))

    def test_unknown_cash_does_not_hide_daily_return_or_create_zero(self):
        result = self.card([holding("joint", "股票", 60, 1)], {"joint": {"account_code": "joint"}})
        for key in ("net_value", "cash", "financing", "leverage"):
            self.assertIsNone(result[key]["value"])
        self.assertEqual(result["market_value"]["value"], "60")
        self.assertEqual(result["recent_daily_pnl"]["value"], "1")

    def test_recent_confirmed_zero_survives_stale_current_and_other_missing_accounts(self):
        row = holding("fund", "基金", 12, 100, daily_pnl_stale=True)
        row["recent_confirmed_return"] = {"status": "confirmed", "as_of": "2026-11-18",
                                          "fields": {"daily_total_pnl_cny": "0"}}
        result = self.card([row, holding("joint", "股票", None)], {"joint": {"account_code": "joint"}})
        self.assertEqual(result["recent_daily_pnl"]["value"], "0")
        self.assertEqual(result["recent_daily_pnl"]["as_of"], "2026-11-18")
        self.assertEqual(result["recent_daily_pnl"]["status"], "partial")
        self.assertEqual(result["net_value"]["value"], "12")
        self.assertEqual(result["net_value"]["status"], "partial")
        self.assertIsNone(result["leverage"]["value"])
