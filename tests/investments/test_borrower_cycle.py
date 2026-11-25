from datetime import date
from decimal import Decimal

import pytest

from domain.ledger.evidence import loan_cycle_evidence as cycles
from domain.ledger.cashflow.ledger_view import _classify_cash_direction


def bank(ident, amount, day):
    return dict(id=ident, amount=Decimal(amount), transaction_date=date.fromisoformat(day),
                account_code='synthetic_cash', currency='CNY', source_evidence_status='verified',
                source_item_id=f'line-{ident}', pair_evidence={'source_id':'bank-original', 'source_hash':'a' * 64})


def test_completed_principal_cycle_preserves_bank_classification_and_rejects_an_unmatched_leg():
    receipts = {1: bank(1, '70', '2020-01-21'), 2: bank(2, '-70', '2020-02-14')}
    claim = dict(scope=cycles.PRINCIPAL, loan='synthetic-cycle', declaration='Both are principal, no interest.',
                 currency='CNY', cash=[cycles._bank_ref(r) for r in receipts.values()],
                 **{'from':'2020-01-21','through':'2020-02-14'})
    cycles._validate(claim, receipts)
    for ident, original in receipts.items():
        row = dict(original, classification_status='confirmed', classification_source='manual',
                   category='旧分类', subcategory='旧明细', verified_cycle_principal=True)
        actual = _classify_cash_direction(row, set())
        assert actual['flow_type'] == 'personal_loan' and not actual['is_operating']
        assert row['category'] == '旧分类'
    with pytest.raises(ValueError):
        cycles._validate(claim, {**receipts, 2: bank(2, '-71', '2020-02-14')})
    bad = {**claim,'cash':[claim['cash'][0], claim['cash'][0]]}
    with pytest.raises(ValueError):
        cycles._validate(bad, receipts)
