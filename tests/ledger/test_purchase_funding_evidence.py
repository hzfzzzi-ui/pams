from decimal import Decimal

import pytest

from domain.ledger.confirmed_transfers_v2 import PURCHASE_PURPOSE, is_manual_funding_pair


def legs():
    cash = {'kind': 'cashflow', 'id': 1, 'account_code': 'bank', 'source_item_id': 'item-a',
            'source_id': 'source-a', 'source_hash': 'hash-a', 'day': '2026-01-29',
            'currency': 'CNY', 'amount': Decimal('-100')}
    trade = {'kind': 'trade', 'id': 2, 'account_code': 'investment', 'source_item_id': 'item-b',
             'source_id': 'source-b', 'source_hash': 'hash-b', 'day': '2026-01-29',
             'currency': 'CNY', 'amount': Decimal('100'), 'purpose': PURCHASE_PURPOSE,
             'bank_cashflow_id': 1, 'bank_source_item_id': 'item-a',
             'bank_source_id': 'source-a', 'bank_source_hash': 'hash-a'}
    return cash, trade


def test_purchase_uses_existing_bank_evidence():
    cash, trade = legs()
    assert is_manual_funding_pair(cash, trade)


@pytest.mark.parametrize('field,value', [
    ('bank_cashflow_id', 3), ('bank_source_item_id', 'other'), ('bank_source_id', 'other'),
    ('bank_source_hash', 'other'), ('amount', Decimal('99')), ('currency', 'USD'),
    ('day', '2026-01-30'), ('account_code', 'bank'),
])
def test_purchase_rejects_conflicting_evidence(field, value):
    cash, trade = legs()
    trade[field] = value
    assert not is_manual_funding_pair(cash, trade)
