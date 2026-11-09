from datetime import date
from pathlib import Path
import sys

import pytest
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
    assert all(value is None for value in result["confirmed_subtotal"].values())


def test_pending_classification_preserves_confirmed_period_and_trend_subtotals():
    day = date(2026, 1, 1)
    rows = [
        {"transaction_date_obj": day, "amount_cny": 120, "flow_type": "income"},
        {"transaction_date_obj": day, "amount_cny": -35, "flow_type": "expense"},
        {"transaction_date_obj": day, "amount_cny": -500, "flow_type": "unclassified", "needs_review": True},
        {"transaction_date_obj": day, "amount_cny": None, "flow_type": "expense"},
    ]
    transfers = [{"match_date_obj": day, "amount_cny": 20, "match_type": "investment_funding"}]
    summary = summarize_period(rows, transfers, day, day)
    assert summary["income"] is None and summary["net"] is None
    assert summary["unclassified_count"] == 1 and summary["pending_count"] == 2
    assert summary["confirmed_subtotal"] == {"income": 120, "expense": 35, "net": 85, "cash_net": 65}
    for grain in ("monthly", "weekly"):
        point = _trend(rows, day, day, grain)[0]
        assert point["net"] is None and point["unclassified_count"] == 1
        assert point["confirmed_subtotal"] == {"income": 120, "expense": 35, "net": 85}
    transfers[0]["amount_cny"] = None
    assert summarize_period(rows, transfers, day, day)["confirmed_subtotal"]["cash_net"] is None


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


@pytest.mark.parametrize('account_code,currency', [('ccb_7332', 'CNY'), ('gf_9711', 'USD'), ('santander', 'MXN')])
def test_keyword_hint_uses_same_rule_for_every_subaccount(account_code, currency):
    row = {'account_code': account_code, 'currency': currency, 'amount': -120,
           'source_evidence_status': 'verified', 'classification_status': 'pending',
           'description': 'CREDIT CARD REPAYMENT'}
    result = classify_ledger_row(row)
    assert result['flow_type'] == 'unclassified'
    assert result['classification_hint'] == {
        'cashflow_type': 'debt_repayment', 'category': '内部划转', 'subcategory': '信用卡还款',
        'matched_keyword': 'credit card repayment',
    }


@pytest.mark.parametrize('overrides', [
    {'summary': 'POS 财付通', 'amount': -520},
    {'summary': '美团外卖 滴滴出行'},
    {'summary': '美团外卖撤销'},
    {'source_evidence_status': 'unknown'},
    {'amount': 20},
])
def test_weak_conflicting_or_unverified_evidence_has_no_hint(overrides):
    row = {'summary': '美团外卖', 'amount': -20, 'source_evidence_status': 'verified',
           'classification_status': 'pending', **overrides}
    assert classify_ledger_row(row)['classification_hint'] is None


@pytest.mark.parametrize('summary,subcategory', [
    ('消费 DLO*Uber Rides CDMX', '打车'),
    ('消费 UBER   *TRIP', '打车'),
    ('消费 DLO UBERRIDES CA2', '打车'),
    ('消费 UBER* EATS', '外卖'),
    ('消费 DLO UBEREATS CA2', '外卖'),
    ('消费 OXXOCITY WALK', '超市便利'),
    ('消费 7-ELEVEN', '超市便利'),
    ('消费 REST PICKUP COFFEE', '咖啡饮品'),
    ('消费 STARBUCKS CITY WALK', '咖啡饮品'),
    ('OXXO PAY', None),
    ('OXXOGAS TAMAULIPAS', None),
    ('UBER *ONE MEMBERSHIP', None),
    ('消费 财付通-理财通-腾安基金', None),
    ('财付通 微信转账', None),
    ('PAGO TRANSFERENCIA SPEI', None),
])
def test_merchant_aliases_require_an_identifiable_purchase(summary, subcategory):
    row = {'summary': summary, 'amount': -20, 'source_evidence_status': 'verified',
           'classification_status': 'pending'}
    view = classify_ledger_row(row)
    assert view['flow_type'] == 'unclassified'
    assert (view['classification_hint'] or {}).get('subcategory') == subcategory


@pytest.mark.parametrize('summary,amount,expected', [
    ('消费退货 原交易退款', 100, ('operating_income', '消费退款')),
    ('消费退货 原交易退款', -100, None),
    ('信用卡卡号还款', -100, ('debt_repayment', '信用卡还款')),
    ('信用卡卡号还款', 100, None),
    ('存入 约定还款', 100, ('debt_repayment', '信用卡还款')),
    ('存入 约定还款', -100, None),
    ('利息 利息', -100, ('operating_expense', '利息支出')),
    ('利息 利息', 100, None),
    ('利息存入', 100, ('operating_income', '利息')),
    ('モバイルSUICA', -100, ('operating_expense', '交通卡')),
    ('PASMO', -100, ('operating_expense', '交通卡')),
    ('PASMO', 100, None),
    ('普通汇兑 银行渠道', 100, ('fx_conversion', '换汇转账')),
    ('汇兑 银行渠道', -100, ('fx_conversion', '换汇转账')),
    ('基金赎回 资金到账', 100, ('investment_withdrawal', '基金赎回')),
    ('基金赎回 资金到账', -100, None),
    ('理财产品赎回 资金到账', 100, ('investment_withdrawal', '理财赎回')),
    ('理财产品赎回 资金到账', -100, None),
    ('积存金买入 银行渠道', -100, ('investment_funding', '积存金买入')),
    ('积存金买入 银行渠道', 100, None),
    ('费用 银行手续费', -100, ('fee', '银行费用')),
    ('费用 银行手续费', 100, None),
    ('消费 服务费用', -100, None),
    ('退货退税 账单退款', 100, ('operating_income', '退货退税')),
    ('退货退税 账单退款', -100, None),
    ('汇兑 冲正', 100, None),
    ('基金上账 银行渠道', 100, None),
    ('基金下账 银行渠道', -100, None),
    ('财付通-理财通', -100, None),
    ('跨行转出', -100, None),
])
def test_statement_aliases_respect_cashflow_direction(summary, amount, expected):
    view = classify_ledger_row({'summary': summary, 'amount': amount,
                                'source_evidence_status': 'verified', 'classification_status': 'pending'})
    hint = view['classification_hint']
    assert view['flow_type'] == 'unclassified'
    assert (None if hint is None else (hint['cashflow_type'], hint['subcategory'])) == expected


@pytest.mark.parametrize('account_type,amount,expected', [
    ('liability', -100, 'debt_drawdown'),
    ('liability', 100, None),
    ('cash', -100, None),
    (None, -100, None),
])
def test_cash_advance_requires_liability_account_and_debit(account_type, amount, expected):
    view = classify_ledger_row({'summary': '取现 ATM', 'amount': amount, 'account_type': account_type,
                                'source_evidence_status': 'verified', 'classification_status': 'pending'})
    assert (view['classification_hint'] or {}).get('cashflow_type') == expected


def test_confirmed_classification_is_not_overridden_by_text():
    row = {'id': 7, 'amount': -20, 'summary': '证券 美团外卖',
           'classification_status': 'confirmed', 'cashflow_type': 'operating_expense', 'category': '购物'}
    result = classify_ledger_row(row)
    assert result['flow_type'] == 'expense' and result['coarse_category'] == '购物'
    assert 'classification_hint' not in result
    assert classify_ledger_row(row, {7})['flow_type'] == 'transfer'
