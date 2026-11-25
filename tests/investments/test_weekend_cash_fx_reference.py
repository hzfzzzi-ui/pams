from datetime import date, timedelta
from decimal import Decimal as D

from domain.investments.valuation.valuation_nav import (
    _add_weekend_cash_references, _bank_backcast_series, format_weekly_nav_payload, fx_rate_at,
)


def test_weekend_reuses_only_an_unchanged_dated_foreign_bank_valuation():
    friday = date(2026, 5, 29)
    saturday, sunday, monday = (friday + timedelta(days=i) for i in (1, 2, 3))
    days = [friday, saturday, sunday, monday]
    anchor = friday - timedelta(days=3)
    rates = {'USD':[(friday,D(7))], 'EUR':[(friday,D(8))], 'CHF':[(friday-timedelta(days=1),D(9))]}
    rate_at = lambda currency, day: fx_rate_at(rates, currency, day, 'CNY')

    def bank(code, currency, as_of=anchor):
        return {'account_code':code, 'account_type':'funding', 'truth_source':'bank_statement',
                'base_currency':currency, 'owner_ratio':'0.5',
                'primary_balance':{'source':'bound bank statement','amount':'20','as_of':as_of,'currency':currency}}

    accounts = [bank('unchanged','USD'), bank('changed','EUR'), bank('too_old','CHF'),
                bank('new_anchor','USD',saturday), bank('mixed','USD')]
    flows = {
        'unchanged':[{'transaction_date':friday,'currency':'USD','amount':D(2)}],
        'changed':[{'transaction_date':saturday,'currency':'EUR','amount':D(4)}],
        'mixed':[{'transaction_date':friday,'currency':'EUR','amount':D(3)}],
    }
    series = _bank_backcast_series(accounts=accounts,rows_by_account=flows,dates=days,terminal_date=monday,
                                   fx_timelines=rates,base_currency='CNY')
    def display(day):
        row = {'date':day, 'net_worth':None, 'unavailable_accounts':series[day]['unavailable_accounts']}
        _add_weekend_cash_references(row, series, rate_at)
        return row

    row = display(saturday)
    assert row['net_worth'] is None and row['available_accounts'] == row['estimated_accounts'] == ['unchanged']
    assert row['available_net_worth'] == D('147')  # personal anchor 20 plus one half of the deposit 2
    proof = row['latest_known_components']['unchanged']
    assert (proof['balance_anchor_as_of'], proof['balance_as_of'], proof['fx_as_of']) == (
        anchor.isoformat(), friday.isoformat(), friday.isoformat())
    assert proof['native_balance'] == '21.0' and proof['currency'] == 'USD'
    _add_weekend_cash_references(row, series, rate_at)
    assert row['available_net_worth'] == D(147) and row['unavailable_accounts'] == series[saturday]['unavailable_accounts']
    assert display(sunday)['available_net_worth'] == D(147)
    assert 'available_net_worth' not in display(monday)
    # A weekend reference is never eligible for a comparison of current-dated balances.
    week_before = {**row,'date':saturday-timedelta(days=7),'_comparison_basis':{'unchanged':['historical_cash_fx','prior']}}
    report = format_weekly_nav_payload(start_date=week_before['date'],terminal_date=saturday,
                                       base_currency='CNY',rows=[week_before,row])
    period = report['period_comparisons']['1w']
    assert period['status'] == 'different_coverage' and period['comparable_accounts']['status'] == 'unavailable'
    proven = {'date':saturday,'net_worth':D('12'),'unavailable_accounts':[]}
    _add_weekend_cash_references(proven,series,rate_at)
    assert proven['net_worth'] == D(12) and 'available_net_worth' not in proven


def test_month_end_statement_changes_native_cash_before_applying_independent_fixing():
    from domain.accounts.dated_valuation import AccountValuation
    from domain.investments.valuation.valuation_nav import _apply_historical_cash_points, _add_month_end_marks
    from domain.market.month_end_marks import month_end_fx
    friday, day = date(2026, 1, 30), date(2026, 1, 31)
    rates = {'MXN': [(friday, D('.40')), (day + timedelta(days=2), D('.48'))]}
    code = 'foreign_bank'
    bank = {'account_code': code, 'account_type': 'funding', 'base_currency': 'MXN', 'owner_ratio': '.5'}
    period = AccountValuation(day, 'MXN', D('240'), 'statement')
    bank_series = {day: {'unavailable_accounts': [code], 'net_worth': D(0)}}
    _apply_historical_cash_points(None, accounts=[bank], bank_series=bank_series, dates=[day],
                                 points={}, periods={(code, day): period}, rate_at=lambda *_: None)
    marks = bank_series[day]['_month_end_marks']
    row = {'date': day, 'net_worth': None, 'unavailable_accounts': [code]}
    for _ in range(2):
        _add_month_end_marks(row, marks, rates, 'CNY')
    assert row['available_net_worth'] == 48 and row['net_worth'] is None
    detail = row['latest_known_components'][code]
    assert (detail['balance_as_of'], detail['fx_as_of']) == (day.isoformat(), friday.isoformat())
    assert month_end_fx(rates, 'MXN', date(2026, 2, 15), 'CNY') is None
    assert month_end_fx(rates, 'MXN', date(2026, 3, 31), 'CNY') is None
    blocked = {'date': day, 'net_worth': None, 'unavailable_accounts': [code],
               'source_evidence_unavailable_accounts': [code]}
    _add_month_end_marks(blocked, marks, rates, 'CNY')
    assert 'available_net_worth' not in blocked
