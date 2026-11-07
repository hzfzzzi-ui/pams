from datetime import date
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend/api"))
from domain.ledger.ledger_view import classify_ledger_row, summarize_period, _trend, _account_summary
from domain.ledger.cashflow_classification import bank_classification_hint


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


def test_summary_exposes_confirmed_subtotals_without_counting_pending_rows():
    day = date(2026, 1, 1)
    pending = {'flow_type': 'unclassified', 'amount_cny': -999, 'transaction_date_obj': day, 'needs_review': True}
    expense = {'id': 8, 'amount': -20, 'amount_cny': -20, 'transaction_date_obj': day,
               'classification_status': 'confirmed', 'cashflow_type': 'operating_expense',
               'category': '其他支出', 'summary': '转账'}
    expense.update(classify_ledger_row(expense))
    assert expense['flow_type'] == 'expense'
    result = summarize_period([pending, expense], [], day, day)
    assert result['income'] is result['expense'] is result['net'] is None
    assert result['confirmed_operating'] == {'income': None, 'expense': 20, 'net': -20}


def test_bank_hint_requires_verified_source_and_explicit_operation():
    row = {'account_code': 'ccb_7332', 'source_evidence_status': 'verified',
           'classification_status': 'pending', 'amount': -50}
    assert bank_classification_hint({**row, 'summary': '信用卡约定还款 尾号0453'})['cashflow_type'] == 'debt_repayment'
    assert bank_classification_hint({**row, 'summary': '基金下账 基金定投申购,110020'})['cashflow_type'] == 'investment_funding'
    assert bank_classification_hint({**row, 'summary': '消费 微信支付 理财通'}) is None
    assert bank_classification_hint({**row, 'summary': '信用卡约定还款 尾号0453', 'source_evidence_status': 'unknown'}) is None
    assert bank_classification_hint({**row, 'summary': '薪金报酬 代发', 'amount': 50})['cashflow_type'] == 'operating_income'
    card = {**row, 'account_code': 'ccb_credit_0453'}
    assert bank_classification_hint({**card, 'summary': '消费 Restaurant'})['cashflow_type'] == 'operating_expense'
    assert bank_classification_hint({**card, 'summary': '消费 Transit Card Charge'}) is None
