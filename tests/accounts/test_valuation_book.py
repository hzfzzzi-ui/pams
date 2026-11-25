from datetime import date
from decimal import Decimal as D

from domain.accounts.monthly.valuation_book import _daily_row, current_balances, render_histories
from domain.investments.valuation.valuation_nav import _apply_account_totals


def test_native_ownership_debt_and_partial_platform_are_accounted_once():
    day = date(2026, 3, 31)
    def book(unit, **fields):
        observed = dict(date=day, observed_on=day, quality='confirmed', basis='net_value', owner_ratio=D(1))
        return dict(currency=unit, series=['net_value'], rows={day: {**observed, **fields}})
    values = {
        'broker': book('USD', signed_balance=D(80), net_value=D(80), gross_assets=D(130), gross_debt=D(50), owner_ratio=D('.5')),
        'card': book('CNY', signed_balance=D(6), liabilities=D(0)),
        'platform': book('CNY', known_component_value=D(20), position_value=D(20), signed_balance=None),
        'cash': book('CNY', signed_balance=D(5)),
    }
    accounts = {code: {'reconcile_method': 'balance' if code in ('cash', 'card') else 'position_nav'} for code in values}
    row = _daily_row(day, values, accounts, {'USD': [(day, D(7))]}, 'CNY')
    assert row['available_net_worth'] == 311 and row['net_worth'] is None and row['assets'] is None
    assert row['unavailable_accounts'] == ['platform']
    assert values['broker']['rows'][day]['personal_contribution'] == 280
    assert current_balances(render_histories(values, day, day))['broker']['balance_amount'] == '80'
    _apply_account_totals(row, [dict(account_code='platform', covered_account_codes=['platform', 'cash'],
        currency='CNY', net_asset='40', observed_total=True)], lambda _: D(1), lambda *_: D(1))
    assert row['_account_contributions'] == {'broker': 280, 'card': 6, 'platform': 40}
    assert row['available_net_worth'] == sum(row['_account_contributions'].values()) == 326


def test_unavailable_fx_preserves_native_observation_and_confirmed_zero():
    day = date(2026, 4, 30)
    values = {code: dict(series=['net_value'], currency='USD', rows={day: dict(date=day, signed_balance=amount,
        observed_on=day, quality='confirmed', basis='cash_balance', owner_ratio=D(1))})
        for code, amount in [('zero', D(0)), ('unpriced', D(12))]}
    row = _daily_row(day, values, {code: {} for code in values}, {}, 'CNY')
    assert row['_account_contributions'] == {'zero': D(0)}
    assert row['unavailable_accounts'] == ['unpriced'] and row['net_worth'] is None
    assert current_balances(render_histories(values, day, day))['unpriced']['balance_amount'] == '12'
