from datetime import date
from decimal import Decimal

from domain.investments.subscription_settlement import _claim


def test_unfilled_subscription_principal_returns_to_same_payer():
    order = dict(account_code='fund', currency='CNY', trade_type='fund_subscription',
                 source_evidence_status='verified', fund_execution_confirmed=True, net_cash=Decimal('-40'))
    debit = dict(account_code='cash', currency='CNY', source_evidence_status='verified',
                 posting_date=None, transaction_date=date(2025, 3, 31), amount=Decimal('-100'))
    refund = dict(debit, posting_date=date(2025, 4, 2), amount=Decimal('60'))
    assert _claim(order, debit, refund) == (date(2025, 3, 31), date(2025, 4, 2), Decimal('60'))
    assert _claim(order, debit, dict(refund, amount=Decimal('100'))) is None
    assert _claim(order, debit, dict(refund, account_code='other')) is None
    assert _claim(order, debit, dict(refund, source_evidence_status='unknown')) is None


def test_monthly_cash_and_nav_use_the_same_confirmed_subscription_legs(monkeypatch):
    from unittest.mock import Mock
    from domain.investments import subscription_settlement as settlement

    day = date(2030, 6, 30)
    later = date(2030, 7, 2)
    legs = dict(payment_cashflow_id=17, refund_cashflow_id=18)
    order = dict(account_code='fund', trade_id=1, currency='CNY', trade_type='fund_subscription',
                 source_evidence_status='verified', fund_execution_confirmed=True, net_cash=Decimal('-40'))
    debit = dict(id=17, account_code='cash', currency='CNY', source_evidence_status='verified',
                 transaction_date=day, posting_date=None, amount=Decimal('-100'))
    refund = dict(debit, id=18, transaction_date=later, amount=Decimal('60'))
    cur = Mock()
    binding = {'from_trade_id': 1, 'account_code': 'fund', 'legs': legs}
    cur.fetchall.return_value = [binding]
    monkeypatch.setattr(settlement, 'load_trade_rows', lambda *args, **kwargs: [order])
    monkeypatch.setattr(settlement, 'load_cash_rows', lambda *args, **kwargs: [debit, refund])
    monkeypatch.setattr(settlement, 'trade_evidence', lambda *args, **kwargs: [
        {'status': 'verified', 'fields': {'subscription_settlement': legs}}])
    assert settlement.subscription_cash_ids(cur, {17}) == {17, 18}
    assert settlement.subscription_refunds(cur, [order]) == [(day, later, Decimal('60'))]
    cur.fetchall.return_value = [binding, dict(binding, from_trade_id=2)]
    assert settlement.subscription_cash_ids(cur, {17}) == set()
    assert settlement.subscription_refunds(cur, [order]) == []
