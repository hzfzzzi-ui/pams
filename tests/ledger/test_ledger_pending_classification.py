from datetime import date
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend/api"))
from domain.ledger.ledger_view import classify_ledger_row, summarize_period, _trend, _account_summary


def test_unconfirmed_classification_does_not_create_income_or_expense():
    day = date(2026, 1, 1)
    row = {"amount": -10, "amount_cny": -10, "cashflow_type": "operating_expense",
           "classification_status": "suggested", "transaction_date_obj": day}
    row.update(classify_ledger_row(row))
    assert row["flow_type"] == "unclassified" and row["needs_review"]
    result = summarize_period([row], [], day, day)
    assert result["income"] is None and result["expense"] is None and result["net"] is None
    assert result["classification_status"] == "incomplete" and result["unclassified_count"] == 1


def test_pending_classification_makes_trend_and_account_totals_incomplete():
    day = date(2026, 1, 1)
    row = {"account_code":"test", "amount":-10, "amount_cny":-10, "transaction_date_obj":day,
           "flow_type":"unclassified", "needs_review":True}
    trend = _trend([row],day,day,"monthly")
    assert trend[0]["income"] is None and trend[0]["expense"] is None and trend[0]["net"] is None
    account = _account_summary([row])[0]
    assert account["income"] is None and account["expense"] is None and account["classification_status"] == "incomplete"


def test_only_confirmed_transfer_overrides_pending_classification():
    row = {'id': 7, 'amount': -10, 'amount_cny': -10, 'classification_status': 'pending'}
    assert classify_ledger_row(row, set())['flow_type'] == 'unclassified'
    assert classify_ledger_row(row, {7})['flow_type'] == 'transfer'
