from datetime import date
from decimal import Decimal

from domain.investments.valuation_nav import (
    _add_known_display_values, _apply_account_totals, current_nav_account_values_from_parts, format_weekly_nav_payload,
)
from domain.investments.nav_period_comparison import position_member, terminal_comparison_basis


def report(*rows):
    return format_weekly_nav_payload(start_date=rows[0]['date'],terminal_date=rows[-1]['date'],
                                     base_currency='CNY',interval='daily',rows=list(rows))


def point(day, amount):
    return {'date':date.fromisoformat(day), 'net_worth':Decimal(amount) if amount is not None else None,
            'available_accounts':['bank']}


def test_exact_period_endpoints_survive_middle_gap_and_zero_baseline():
    start = point('2025-12-31', '100')
    month = point('2026-02-28', '-4')
    week = point('2026-03-24', '0')
    last = point('2026-03-31', '115')
    result = report(start, month, week, point('2026-03-30',None), last)
    periods = result['period_comparisons']
    assert periods['ytd'] == {'start':'2025-12-31','end':'2026-03-31','status':'ok','change':'15','change_rate':'0.15'}
    assert periods['1m']['start'] == '2026-02-28' and periods['1m']['change'] == '119'
    assert periods['1w']['change'] == '115'
    assert periods['1w']['change_rate'] is None and periods['1m']['change_rate'] is None
    assert result['rows'][-2]['comparison_scope'] is None
    assert report(start,point('2026-03-31',None))['period_comparisons']['ytd']['status'] == 'missing_endpoint'
    assert report(month,last)['period_comparisons']['ytd']['status'] == 'missing_endpoint'


def test_partial_account_members_and_value_sources_define_comparison():
    stored = dict(asset_liability='负债',currency='CNY',asset_id='13',symbol='LOAN')
    latest = dict(asset_type='liability',currency='CNY',holding_snapshot_asset_id='13',asset_id='LOAN',symbol='LOAN')
    assert position_member(stored) == position_member(latest)
    def partial(day, source, missing=()):
        row = {'date':date.fromisoformat(day),'net_worth':None,'unavailable_accounts':['invest',*missing]}
        _add_known_display_values(row,{'invest':(Decimal(20),row['date'],row['date'])},basis={'invest':source})
        return row
    holdings = ['position_members',[['资产','CNY','ETF']]]
    first = partial('2025-12-31',holdings)
    for last in (partial('2026-03-31','balance_reference'),
                 partial('2026-03-31',['position_members',[['资产','USD','company']]]),
                 partial('2026-03-31',holdings,['cash'])):
        result = report(first,last)
        assert result['period_comparisons']['ytd']['status'] == 'different_coverage'
        assert result['rows'][0]['comparison_scope'] != result['rows'][1]['comparison_scope']
    assert report(first,partial('2026-03-31',holdings))['period_comparisons']['ytd']['status'] == 'ok'
    def platform(day, covered):
        row = partial(day,holdings)
        total = dict(account_code='invest',currency='CNY',net_asset='20',covered_account_codes=covered)
        _apply_account_totals(row,[total],lambda _:Decimal(1),lambda *_:Decimal(1))
        return row
    assert report(platform('2025-12-31',['cash','invest']),platform('2026-03-31',['invest','cash']))[
        'period_comparisons']['ytd']['status'] == 'ok'
    assert report(platform('2025-12-31',['cash','invest']), platform('2026-03-31',['invest']))[
        'period_comparisons']['ytd']['status'] == 'different_coverage'


def test_terminal_comparison_follows_account_scope_instead_of_amounts():
    start = point('2025-12-31', '100.00')
    start['_comparison_basis'] = {'bank':'cash_balance'}
    row = {**start, 'date':date(2026, 3, 31)}
    values = current_nav_account_values_from_parts(
        account_balances=[{'account_code':'bank','amount_cny':'112.345'}], investment_positions=[],
    )
    row['_comparison_basis'] = terminal_comparison_basis(row, {'bank'}, values, Decimal('112.35'))
    row['net_worth'] = Decimal('112.35')
    assert report(start, row)['period_comparisons']['ytd']['change'] == '12.35'
    # Even an exactly matching total cannot certify a missing or different account breakdown.
    for by_account in ({}, {'different_account': Decimal('100')}):
        row['_comparison_basis'] = terminal_comparison_basis(start, {'bank'}, by_account, Decimal('100'))
        row['net_worth'] = Decimal(100)
        assert report(start, row)['period_comparisons']['ytd']['status'] == 'different_coverage'
    # An untyped published breakdown requires complete corroborating same-day evidence.
    assert terminal_comparison_basis(start, {'bank'}, {'bank':'110'}, Decimal(110)) == start['_comparison_basis']
    assert terminal_comparison_basis({**start,'estimated_accounts':['bank']}, {'bank'},
                                     {'bank':'110'}, Decimal(110)) != start['_comparison_basis']
