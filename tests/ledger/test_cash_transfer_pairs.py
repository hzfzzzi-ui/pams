from datetime import date, timedelta
from decimal import Decimal

from domain.ledger.cash_transfer_pairs import suggest


def cash_row(ident, account, amount, *, currency='CNY', day_offset=0, hint='internal_transfer', source=None):
    evidence = {'account_code': account, 'currency': currency, 'amount': Decimal(amount),
                'source_item_id': ident, 'source_id': source or ident, 'source_hash': None}
    return {**evidence, 'id': ident, 'pair_evidence': evidence, 'source_evidence_status': 'verified',
            'transaction_date': date(2026, 1, 2) + timedelta(days=day_offset),
            'classification_status': 'confirmed', 'cashflow_type': hint}


def test_equal_pairs_share_one_rule_across_account_types():
    profiles = {'card': {'account_type': 'liability'}, 'broker': {'account_type': 'brokerage'}}
    rows = [cash_row(1, 'bank_a', '-100'), cash_row(2, 'bank_b', '100'),
            cash_row(3, 'bank_a', '-200'), cash_row(4, 'card', '200', hint='debt_repayment'),
            cash_row(5, 'bank_a', '-300'), cash_row(6, 'broker', '300', day_offset=2),
            cash_row(7, 'broker', '-400'), cash_row(8, 'bank_a', '400')]
    assert {m['match_key']: m['match_type'] for m in suggest(rows, profiles, set(), limit=10)} == {
        'candidate:1:2': 'internal_transfer', 'candidate:3:4': 'credit_card_payment',
        'candidate:5:6': 'investment_funding', 'candidate:7:8': 'investment_withdrawal',
    }


def test_ambiguity_and_evidence_are_checked_before_limiting_results():
    rows = [cash_row(1, 'bank_a', '-100'), cash_row(2, 'bank_b', '100'), cash_row(3, 'bank_c', '100'),
            cash_row(4, 'bank_a', '100'), cash_row(5, 'bank_b', '100', currency='USD'),
            cash_row(6, 'bank_b', '100.0001'), cash_row(7, 'bank_b', '100', day_offset=4),
            cash_row(8, 'bank_b', '100', source=1), cash_row(9, 'bank_b', '100'),
            {**cash_row(10, 'bank_b', '100'), 'source_evidence_status': 'unknown'}]
    matches = suggest(rows, {}, {9}, limit=10)
    assert [(m['target_row_id'], m['ambiguity_status']) for m in matches] == [(2, 'ambiguous'), (3, 'ambiguous')]
    assert suggest(rows, {}, {9}, limit=1)[0]['ambiguity_status'] == 'ambiguous'


def test_cross_currency_card_candidates_require_independent_same_day_repayment_evidence():
    profiles = {'bank_a': {'account_type': 'funding'},
                'card': {'account_type': 'liability', 'institution': '合成银行'}}
    debit = {**cash_row(1, 'bank_a', '-700'), 'amount_cny': Decimal('-700'),
             'summary': '银联代收 合成银行信用卡还款'}
    credit = {**cash_row(2, 'card', '100', currency='USD'), 'amount_cny': Decimal('698'),
              'description': '跨行购汇还款'}
    match, = suggest([debit, credit], profiles, set())
    assert match['match_type'] == 'credit_card_payment'
    assert (match['currency'], match['amount'], match['target_currency'], match['target_amount']) == (
        'CNY', Decimal('700'), 'USD', Decimal('100'))
    assert match['settlement_rate'] == Decimal('7')
    for invalid in (
        {**credit, 'description': '合并购汇还款'},
        {**credit, 'description': 'MC rebate'},
        {**credit, 'amount_cny': Decimal('650')},
        {**credit, 'transaction_date': credit['transaction_date'] + timedelta(days=1)},
        {**credit, 'pair_evidence': {**credit['pair_evidence'], 'source_id': 1}},
    ):
        assert suggest([debit, invalid], profiles, set()) == []
    assert suggest([debit, credit], profiles, {2}) == []
