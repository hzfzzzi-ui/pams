"""A sale paid into a bank account must match its exact receipt."""
import unittest

from domain.ledger.transfers.confirmed_transfers_v2 import BANK_REDEMPTION_PURPOSE, is_manual_withdrawal_pair


class SaleSettlementTest(unittest.TestCase):
    def test_only_the_evidenced_receipt_matches(self):
        outgoing = dict(kind='trade', purpose=BANK_REDEMPTION_PURPOSE, day='2026-01-05',
                        receipt_day='2026-01-07', amount='-125', currency='CNY', account_code='investment',
                        source_item_id='trade-item', source_id='trade-source', source_hash=None,
                        bank_cashflow_id=13, bank_source_item_id='bank-item', target_account='bank')
        receipt = dict(kind='cashflow', id=13, day='2026-01-07', amount='125', currency='CNY',
                       account_code='bank', source_item_id='bank-item', source_id='bank-source', source_hash=None)
        self.assertTrue(is_manual_withdrawal_pair(outgoing, receipt))
        for change in ({'id': 14}, {'source_item_id': 'other-item'}, {'day': '2026-01-08'},
                       {'account_code': 'other-bank'}, {'currency': 'USD'}, {'amount': '126'}):
            with self.subTest(change=change):
                self.assertFalse(is_manual_withdrawal_pair(outgoing, {**receipt, **change}))
