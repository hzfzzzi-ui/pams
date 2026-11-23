from datetime import date
from decimal import Decimal

from domain.investments.monthly_overview import _conversion_spreads
from domain.ledger.conversion_settlement import monthly_conversion_settlements


def test_only_cross_currency_cash_settlements_count_signed_grouped_legs_at_common_personal_share():
    first, day = date(2030, 1, 1), date(2030, 1, 10)
    rows = [dict(id=i, account_code=account, amount_cny=Decimal(value), source_evidence_status='verified',
                 transaction_date=day, posting_date=None)
            for i, account, value in [(1, 'bank', '-170'), (2, 'card_a', '70'), (3, 'card_b', '98.50')]]
    match = dict(status='confirmed', both_cash_legs=True, currency_norm='CNY', source_row_id=1,
                 target_row_id=2, from_account_code='bank', to_account_code='card_a',
                 source_date=day, target_date=day, targets=[
                     dict(id=2, currency='CNY', account_code='card_a'), dict(id=3, currency='USD', account_code='card_b')])
    def read(legs):
        settlements = monthly_conversion_settlements(legs, [match, dict(match, targets=[]),
                                                            dict(match, both_cash_legs=False)])
        return {month: {'conversion_settlements': result} for month, result in settlements.items()}

    nav = {'2030-01-31': {'monthly_book': {'accounts': {
        r['account_code']: {'method': 'opening_and_cash_movements'} for r in rows}}}}
    def personal(legs, excluded=(), ratio=lambda _: Decimal('0.6')):
        return _conversion_spreads(None, read(legs), nav, excluded, ratio)

    assert personal(rows) == {first: Decimal('-0.90')}
    assert personal(rows, excluded={'card_b'}) == {}
    assert personal(rows, ratio=lambda code: Decimal('0.5') if code == 'card_b' else Decimal(1)) == {first: None}
    assert personal(rows[:-1]) == {first: None}
    assert personal([*rows[:-1], dict(rows[-1], amount_cny=None)]) == {first: None}
    cross = read([*rows[:-1], dict(rows[-1], posting_date=date(2030, 2, 1))])
    assert cross[date(2030, 2, 1)]['conversion_settlements'][0]['amount'] is None
