from datetime import date
from decimal import Decimal
from unittest.mock import Mock

from domain.investments import installment_principal
from domain.ledger.investment_receipts import counted_receipt_ids


def test_receipt_only_removed_for_the_same_verified_source_and_counted_return():
    day = date(2030, 3, 9)
    receipt = dict(id=1, account_code='cash', source_evidence_status='verified', source_item_id='item',
                   amount=Decimal('52'), transaction_date_obj=day, currency='CNY',
                   pair_evidence={'source_hash': 'document-hash', 'source_id': 'source'})
    reference = dict(source_table='pams_cashflow_fact', account_code='cash', cashflow_id=1,
                     source_id='source', source_key='item', source_hash='document-hash')
    trade = dict(trade_date=day, currency='CNY', net_cash=Decimal('52'), receipt=reference)
    cur = Mock()
    cur.fetchall.return_value = [trade]
    assert counted_receipt_ids(cur, [receipt], ()) == set()
    cur.execute.assert_not_called()
    assert counted_receipt_ids(cur, [receipt], ['counted-trade']) == {1}
    assert counted_receipt_ids(cur, [dict(receipt, source_item_id='different')], ['counted-trade']) == set()
    cur.fetchall.return_value = [trade, trade]
    assert counted_receipt_ids(cur, [receipt], ['counted-trade']) == set()


def test_verified_financing_funding_and_billed_principal_leave_interest_in_expenses(monkeypatch):
    def contract(account, funding, parts):
        postings = [dict(fact_ids=[principal, interest], principal_fact=principal) for principal, interest in parts]
        return ({'raw_fields': dict(bank_funding_fact=funding, verified_postings=postings)},
                'position', (account, 'order'), {})

    contract_one = contract('card', 1, [(2, 3), (4, 5)])
    contract_two = contract('other', 6, [(7, 4)])
    monkeypatch.setattr(installment_principal, '_verified_contracts', lambda *_: [contract_one, contract_two])
    assert installment_principal.load_principal_cash_ids(None, {}, date(2030, 1, 1)) == {1, 2, 6, 7}
