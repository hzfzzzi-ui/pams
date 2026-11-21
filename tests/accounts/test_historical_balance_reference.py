from datetime import date
from decimal import Decimal

from domain.accounts.balance_reference import historical_account_balance


def test_reference_retains_native_currency_and_ownership():
    cfg = {'base_currency': 'MXN', 'account_balance_reference': {
        'amount': '100.25', 'currency': 'MXN', 'as_of': '2025-12-31', 'source': 'statement'}}
    result = historical_account_balance(cfg, date(2026, 1, 1), Decimal('0.5'))
    assert result['balance_amount'] == '50.125'
    assert result['balance_currency'] == 'MXN'
    assert result['balance_as_of'] == date(2025, 12, 31)


def test_reference_does_not_invent_fx_or_use_future_or_unattributed_data():
    ref = {'amount': '0', 'currency': 'USD', 'as_of': '2025-12-31', 'source': 'statement'}
    assert historical_account_balance({'base_currency': 'CNY', 'account_balance_reference': ref}, date(2026, 1, 1), Decimal('1')) is None
    assert historical_account_balance({'base_currency': 'USD', 'account_balance_reference': ref}, date(2025, 12, 30), Decimal('1')) is None
    assert historical_account_balance({'base_currency': 'USD', 'account_balance_reference': {**ref, 'source': ''}}, date(2026, 1, 1), Decimal('1')) is None
    assert historical_account_balance({'base_currency': 'USD', 'account_balance_reference': ref}, date(2026, 1, 1), Decimal('1'))['balance_amount'] == '0'


def test_existing_cny_reference_remains_available():
    cfg = {'base_currency': 'CNY', 'account_balance_reference': {
        'amount': '123.45', 'currency': 'CNY', 'as_of': '2025-12-31', 'source': 'statement'}}
    assert historical_account_balance(cfg, date(2026, 1, 1), Decimal('1'))['balance_amount'] == '123.45'


def test_unconverted_foreign_reference_never_enters_cny_terminal_value():
    from domain.investments.valuation_nav import latest_nav_display_values

    accounts = {'accounts': {
        'foreign': {'base_currency': 'USD', 'account_balance_reference': {
            'amount': '100', 'currency': 'USD', 'as_of': '2025-12-31', 'source': 'statement'}},
        'domestic': {'base_currency': 'CNY', 'account_balance_reference': {
            'amount': '100', 'currency': 'CNY', 'as_of': '2025-12-31', 'source': 'statement'}},
    }}
    values = latest_nav_display_values(
        account_balances=[{'account_code': 'foreign'}, {'account_code': 'domestic'}],
        investment_positions=[], terminal_date=date(2026, 1, 1), confirmed_values={},
        account_config=accounts,
    )
    assert 'foreign' not in values
    assert values['domestic'][0] == Decimal('100')
