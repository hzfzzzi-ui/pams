from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from endpoints import ledger  # noqa: E402


def test_ledger_manual_update_type_defaults_are_explicit() -> None:
    assert ledger.LEDGER_MANUAL_CASHFLOW_TYPES == {
        "operating_income",
        "operating_expense",
        "investment_funding",
        "investment_withdrawal",
        "internal_transfer",
        "debt_repayment",
    }

    assert ledger._manual_category("operating_income", "", "") == ("其他收入", "手动确认")
    assert ledger._manual_category("operating_expense", "", "") == ("其他支出", "手动确认")
    assert ledger._manual_category("investment_funding", None, None) == ("内部划转", "投资入金")
    assert ledger._manual_category("debt_repayment", "内部划转", "") == ("内部划转", "信用卡还款")


def test_ledger_manual_update_uses_fact_flags() -> None:
    funding_flags = ledger.cashflow_flags_for_type("investment_funding")
    expense_flags = ledger.cashflow_flags_for_type("operating_expense")
    repayment_flags = ledger.cashflow_flags_for_type("debt_repayment")

    assert funding_flags["affects_spending"] is False
    assert funding_flags["affects_nav"] is False
    assert funding_flags["affects_investment_capital"] is True

    assert expense_flags["affects_spending"] is True
    assert expense_flags["affects_nav"] is True

    assert repayment_flags["affects_spending"] is False
    assert repayment_flags["affects_nav"] is False
