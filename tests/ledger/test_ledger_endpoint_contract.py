from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock


ROOT = Path(__file__).resolve().parents[2]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

import pytest
from fastapi import HTTPException

from endpoints import ledger  # noqa: E402


def test_confirm_source_record_transfer_uses_runtime_scope(monkeypatch):
    from domain.imports import isolation
    from domain.ledger import confirmed_transfers_v2
    from endpoints import bank_reconcile

    conn = MagicMock()
    monkeypatch.setenv("PAMS_IMPORT_RUNTIME_ENABLED", "true")
    monkeypatch.setattr(bank_reconcile, "get_conn", lambda: conn)
    monkeypatch.setattr(bank_reconcile, "_table_exists", lambda cur, table: table == "source_records")
    monkeypatch.setattr(isolation, "source_enabled", lambda cur: isolation.runtime_import_active())
    confirm = MagicMock(return_value={"transfer_id": "fixture"})
    monkeypatch.setattr(confirmed_transfers_v2, "confirm", confirm)
    request = bank_reconcile.ConfirmTransferRequest(
        from_kind="cashflow", from_id=1, to_kind="cashflow", to_id=2, note="人工核对", confirmed=True,
    )
    assert bank_reconcile.confirm_transfer(request) == {"transfer_id": "fixture"}
    assert confirm.call_args.kwargs == request.model_dump()
    assert not isolation.runtime_import_active()

@pytest.mark.parametrize("month", ["2099-01", "2026-13"])
def test_ledger_workbench_rejects_invalid_history_month_before_connecting(monkeypatch, month) -> None:
    monkeypatch.setattr(ledger, "get_conn", lambda: pytest.fail("invalid month reached the database"))
    with pytest.raises(HTTPException) as error:
        ledger.ledger_workbench(month=month)
    assert error.value.status_code == 422


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
    assert ledger._manual_category("fee", "", "") == ("金融费用", "银行费用")
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


@pytest.mark.parametrize(("kind", "amount", "stored_type", "subcategory"), [
    ("personal_loan_drawdown", 100, "debt_drawdown", "个人借入本金"),
    ("personal_loan_repayment", -100, "debt_repayment", "归还借款本金"),
])
def test_personal_loan_classification_requires_matching_unpaired_direction(
    monkeypatch, kind, amount, stored_type, subcategory,
):
    from domain.ledger import cashflow_classification, confirmed_transfers_v2

    conn = MagicMock()
    cur = conn.__enter__.return_value.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = {"account_code": "bank"}
    row = {"amount": amount}
    monkeypatch.setattr(ledger, "get_conn", lambda: conn)
    monkeypatch.setattr(ledger, "_table_exists", lambda cur, table: True)
    monkeypatch.setattr(ledger, "_verified_new_cashflow", lambda cur, row_id: row)
    occupied = set()
    monkeypatch.setattr(confirmed_transfers_v2, "occupied_cashflow_ids", lambda cur: occupied)
    confirm = MagicMock()
    monkeypatch.setattr(cashflow_classification, "confirm", confirm)
    payload = ledger.LedgerRowUpdate(cashflow_type=kind)

    ledger.update_ledger_row(1, payload)
    confirm.assert_called_once_with(cur, cashflow_id=1, cashflow_type=stored_type,
                                    category="个人借款", subcategory=subcategory)
    confirm.reset_mock()
    row["amount"] = -amount
    with pytest.raises(HTTPException, match="方向"):
        ledger.update_ledger_row(1, payload)
    row["amount"] = amount
    occupied.add(1)
    with pytest.raises(HTTPException, match="撤销配对"):
        ledger.update_ledger_row(1, payload)
    confirm.assert_not_called()
