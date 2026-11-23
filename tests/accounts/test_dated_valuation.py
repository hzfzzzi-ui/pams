from datetime import date
from decimal import Decimal as D
import os

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

from domain.accounts import dated_valuation as valuation
from domain.accounts.account_history import read_account_history
from domain.imports.isolation import source_enabled
from domain.investments.valuation_nav import build_nav_weekly_payload


def test_detail_and_portfolio_use_the_same_statement_across_months(monkeypatch):
    dsn = os.environ.get('PAMS_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('requires explicitly isolated PostgreSQL')
    first, second = date(2025, 3, 31), date(2025, 4, 30)
    code = 'synthetic_cash'
    cfg = {'account_code': code, 'account_type': 'funding', 'include_in_nav': True,
           'base_currency': 'CNY', 'owner_ratio': D('.25'),
           'primary_balance': {'as_of': second, 'amount': D(25), 'currency': 'CNY'}}
    monkeypatch.setattr(valuation, 'statement_balance_history', lambda *args, **kwargs: {
        code: [{'amount': '0', 'as_of': first}, {'amount': '160', 'as_of': second}]})
    with psycopg2.connect(dsn) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        assert source_enabled(cur)
        history = read_account_history(cur, account_code=code, account_config={'accounts': {code: cfg}},
                                       start_date=first, end_date=second)
        assert {r['date']: r['net_value'] for r in history['rows']} == {first: 0, second: 160}
        for interval in ('weekly', 'daily'):
            nav = build_nav_weekly_payload(cur, start_date=first, terminal_date=second, interval=interval,
                base_currency='CNY', overview_payload={'accounts': [cfg]}, account_config={'accounts': {code: cfg}},
                include_ibkr=False, owner_ratio_case_sql=lambda _: ('1::numeric', []),
                account_owner_ratio=lambda _: D('.25'))
            assert D(nav['rows'][0]['netWorth']) == 0
            assert D(nav['rows'][-1]['netWorth']) == 40


def test_only_a_whole_dated_value_can_be_converted_to_personal_nav():
    day = date(2025, 2, 28)
    point = {'net_value': D(100), 'liabilities': D(50), 'position_value': D(140)}
    read = valuation.AccountValuation.from_measurement
    assert read(day, 'USD', point, 'net_value').personal_value(D('.5'), D(7)) == 350
    assert read(day, 'USD', point, 'net_value').personal_value(D('.5'), None) is None
    for field in ('liabilities', 'position_value'):
        assert read(day, 'USD', point, field).personal_value(D(1), D(7)) is None
    point.update(net_value_status='partial')
    assert read(day, 'USD', point, 'net_value').personal_value(D(1), D(7)) is None
