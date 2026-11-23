from datetime import date
from decimal import Decimal as D
from unittest.mock import Mock

from domain.accounts import monthly_cash_attribution as attribution
from domain.ledger.ledger_view import _monthly_close


def close(iso, native, rate, *, fixing=None):
    return {'date': iso, 'account_contributions': {'wallet': D(native) * D(rate)},
            'latest_known_components': {'wallet': {'currency': 'USD', 'native_balance': native, 'fx_rate': rate,
                                                   'owner_ratio': '1', 'fx_as_of': fixing or iso}},
            'monthly_book': {'accounts': {'wallet': {'method': 'opening_and_cash_movements',
                                         'currency': 'USD', 'status': 'book_only'}}, 'in_transit': []}}


def test_currency_revaluation_and_posting_timing_exclude_transfer_principal(monkeypatch):
    dec, jan, feb = (date.fromisoformat(x) for x in ('2025-12-31', '2026-01-31', '2026-02-28'))
    nav = {dec.isoformat(): close(dec.isoformat(), '120', '7'),
           jan.isoformat(): close(jan.isoformat(), '20', '6.9', fixing='2026-01-30'),
           feb.isoformat(): close(feb.isoformat(), '10', '7.1')}
    del nav[dec.isoformat()]['monthly_book']
    jan_book = nav[jan.isoformat()]['monthly_book']
    jan_book['accounts']['wallet'].update(status='unmatched', difference='-100')
    jan_book['in_transit'].append({'from_account': 'wallet', 'currency': 'USD', 'native': '100'})
    cur = Mock()
    cur.fetchall.return_value = [
        {'account_code': 'wallet', 'event_date': jan, 'posted': jan, 'cashflow_id': 1},
        {'account_code': 'wallet', 'event_date': jan, 'posted': date(2026, 2, 2), 'cashflow_id': 2},
        {'account_code': 'wallet', 'event_date': dec, 'posted': jan, 'cashflow_id': 3},
    ]
    facts = [dict(fact_id=idx, amount=D(amount), amount_cny=D(converted), currency='USD',
                  source_evidence_status='verified') for idx, amount, converted in
             [(1, '-100', '-695'), (2, '-10', '-69'), (3, '-30', '-210')]]
    monkeypatch.setattr(attribution, 'load_cash_rows', lambda *args, **kwargs: facts)
    monkeypatch.setattr(attribution, 'transaction_opening_accounts', lambda *_: {'wallet'})
    # Dec's purchase is already included in the opening. Jan's remittance is a transfer.
    ledger = {jan.replace(day=1): {'operating_timing_ids': [2]}, feb.replace(day=1): {'operating_timing_ids': []}}
    def calculate():
        return [month['wallet'] for month in attribution.monthly_cash_attribution(
            cur, nav, ledger, start=jan.replace(day=1), end=feb).values()]
    first, second = calculate()
    assert (first['exchange'], first['posting'], first['closing_fx_date']) == (D(-7), D(69), '2026-01-30')
    assert (second['exchange'], second['posting']) == (D(2), D(-69))
    # Unsupported comparison, missing FX and omitted native movement must never generate a balancing number.
    jan_book['in_transit'].clear()
    assert all(month['exchange'] is None for month in calculate())
    jan_book['accounts']['wallet']['status'] = 'book_only'
    facts[0]['amount_cny'] = None
    assert calculate()[0]['exchange'] is None
    facts[1]['amount'] = D(-9)
    assert calculate()[1]['reason'] == 'native_movement_mismatch'


def test_monthly_ledger_only_discloses_timing_of_included_operating_facts():
    jan = date(2026, 1, 31)
    base = dict(account_code='card', fact_id=1, transaction_date_obj=jan, posting_date=date(2026, 2, 2),
                amount=-1, amount_cny=-7, flow_type='expense', classification_status='confirmed',
                category='消费', subcategory='餐饮', cashflow_type='operating_expense')
    result = _monthly_close([base, {**base, 'fact_id': 2, 'flow_type': 'transfer'},
                            {**base, 'fact_id': 3, 'flow_type': 'cash_reversal'},
                            {**base, 'fact_id': 4, 'posting_date': jan}], [], jan.replace(day=1), jan,
                            redesigned=True, account_profiles={}, counted_income_sources=set())
    assert result['operating_timing_ids'] == [1]
