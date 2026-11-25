from datetime import date
from decimal import Decimal

from domain.investments.valuation.valuation_nav import (
    _add_known_display_values, _apply_account_totals, _bank_backcast_series,
    current_nav_account_values_from_parts, format_weekly_nav_payload,
)
from domain.investments.valuation.nav_period_comparison import apply_terminal_breakdown, position_member, terminal_comparison_basis


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
    assert periods['ytd'] == {'start':'2025-12-31','end':'2026-03-31','status':'ok','change':'15','change_rate':'0.15',
        'comparable_accounts':{'status':'unavailable','opening':None,'closing':None,'change':None,'change_rate':None,
                               'account_codes':[],'account_labels':[],'estimated_account_codes':[]}}
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
    # Equal portfolio sums cannot resolve conflicting, undated archived account amounts.
    row.update(_account_contributions={'bank':Decimal(10),'other':Decimal(5)})
    apply_terminal_breakdown(row, {'bank':'cash_balance','other':'cash_balance'}, {'bank':'9','other':'6'}, {})
    assert row['_account_contributions'] == {'bank':Decimal(9),'other':Decimal(6)}
    assert row['_undated_terminal_accounts'] == {'bank','other'}


def test_overlap_uses_only_dated_nonoverlapping_personal_contributions():
    def sample(day, values, sources):
        return {'date':date.fromisoformat(day), 'available_net_worth':sum(map(Decimal,values.values())),
                'available_accounts':list(values), '_account_contributions':values, '_comparison_basis':sources,
                '_dated_cash_accounts':{'bank', 'part'}, 'estimated_accounts':['bank']}
    cash = {'bank':'cash_balance','loan':'balance_reference'}
    group = ['account_total',['fund','part']]
    start = sample('2025-12-31', {'bank':'0','loan':'50','fund':'80'}, {**cash,'fund':group})
    end = sample('2026-03-31', {'bank':'6','loan':'50','fund':'95','part':'5'},
                 {**cash,'fund':['position_members',['ETF']],'part':'cash_balance'})
    period = format_weekly_nav_payload(
        start_date=start['date'], terminal_date=end['date'], base_currency='CNY', rows=[start,end], account_labels={'bank':'活期账户'},
    )['period_comparisons']['ytd']
    assert period['status'] == 'different_coverage' and period['change'] is None
    assert period['comparable_accounts'] == {'status':'ok','opening':'0','closing':'6','change':'6','change_rate':None,
                                              'account_codes':['bank'],'account_labels':['活期账户'],'estimated_account_codes':['bank']}
    # Same complete platform can be used once, including its cash, on both dates.
    end = sample('2026-03-31', {'bank':'6','fund':'95'}, {**cash,'fund':group})
    actual = report(start,end)['period_comparisons']['ytd']['comparable_accounts']
    assert (actual['account_codes'],actual['opening'],actual['closing']) == (['bank','fund'],'80','101')
    # A stale closing quote for the same recorded member is display-only.
    for row in (start,end):
        row['_comparison_basis']['fund'] = ['position_members',['ETF']]
        row['latest_known_components'] = {'fund':dict.fromkeys(('from','through'), '2025-12-31')}
    end['_undated_terminal_accounts'] = {'bank'}
    assert report(start,end)['period_comparisons']['ytd']['comparable_accounts']['status'] == 'unavailable'


def test_missing_financial_fields_remain_unknown_and_explicit_zero_survives():
    result = report({'date':date(2025,12,31)}, {'date':date(2026,1,1),'liabilities':Decimal(0),'net_worth':Decimal(0)})
    empty, zero = result['rows']
    assert [empty[key] for key in ('assets','liabilities','netWorth')] == [None,None,None]
    assert [zero[key] for key in ('assets','liabilities','netWorth')] == [None,'0','0']


def test_account_estimate_stops_being_comparable_after_last_dated_evidence():
    days = [date(2026,1,day) for day in (1,2,3,4)]
    bank = {'account_code':'cash', 'account_type':'funding', 'base_currency':'CNY',
            'primary_balance':{'as_of':days[2], 'amount':'100', 'source':'verified_bank_balance_snapshot'}}
    series = _bank_backcast_series(accounts=[bank], rows_by_account={'cash':[{
        'transaction_date':days[0], 'currency':'CNY', 'amount':'5',
    }]}, dates=days, terminal_date=days[-1], fx_timelines={}, base_currency='CNY')
    assert all('cash' in series[day]['_dated_cash_accounts'] for day in days[:3])
    assert series[days[-1]]['account_values']['cash'] == Decimal(100)
    assert 'cash' not in series[days[-1]].get('_dated_cash_accounts', ())
