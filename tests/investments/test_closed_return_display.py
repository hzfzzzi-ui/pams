from datetime import date

from domain.investments.closed_return_display import closed_return_summary


def sample(opened, exited, pnl):
    return {'base_currency': 'CNY', 'amount': pnl, 'items': [{'account_code': 'account', 'symbol': 'product', 'cycles': [{
        'open_date': opened, 'exit_date': exited, 'trade_pnl': pnl, 'distributions': '0',
    }]}]}


def test_cross_year_profit_is_not_current_year_profit():
    result = closed_return_summary(sample('2025-12-12', '2026-03-24', '322.39'), start=date(2026, 1, 1))
    assert result['lifetime_amount'] == '322.39'
    assert result['period_amount'] is None
    assert result['period_status'] == 'opening_valuation_missing'
    assert result['included_in_return_curve'] is False


def test_same_period_exit_can_report_known_period_amount():
    result = closed_return_summary(sample('2026-01-12', '2026-03-24', '322.39'), start=date(2026, 1, 1))
    assert result['period_amount'] == '322.39'
    assert result['period_status'] == 'known'


def test_cross_year_period_uses_independently_reconciled_opening_value():
    lifetime = sample('2025-12-12', '2026-03-24', '322.39')
    period = sample('2025-12-12', '2026-03-24', '255.02832')
    result = closed_return_summary(lifetime, start=date(2026, 1, 1), period_closed=period)
    assert result['period_amount'] == '255.02832'
    assert result['period_status'] == 'known'
    assert result['included_in_return_curve'] is False
