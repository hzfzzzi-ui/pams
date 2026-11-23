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
