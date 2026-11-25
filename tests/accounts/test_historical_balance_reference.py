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
    from domain.investments.valuation.valuation_nav import latest_nav_display_values

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


def test_terminal_broker_reference_requires_official_net_value_and_fx_matches_its_date():
    from domain.investments.valuation.valuation_nav import latest_nav_display_values
    day, later = date(2026, 1, 5), date(2026, 1, 6)
    bank = dict(account_code='foreign', currency='USD', amount_cny=Decimal(70), as_of=day, fx_rate_as_of=day)
    broker = dict(account_code='ibkr', currency='CNY', amount_cny=Decimal(50), as_of=day,
                  daily_close={'status':'ok', 'as_of':str(day)})
    position = dict(account_code='ibkr', currency='CNY', market_value_cny=Decimal(150), as_of=day)
    args = dict(account_balances=[bank,broker], investment_positions=[position], terminal_date=later,
                confirmed_values={'ibkr':Decimal(50),'foreign':Decimal(70)})
    assert latest_nav_display_values(**args) == {'ibkr':(Decimal(50), day, day)}
    broker['daily_close']['status']='unconfirmed'
    assert not latest_nav_display_values(**args)
    other = 'another_broker'
    assert not latest_nav_display_values(
        account_balances=[{'account_code':other}], investment_positions=[{**position,'account_code':other}],
        confirmed_values={other:Decimal(150)}, terminal_date=later,
        account_config={'accounts':{other:{'portfolio_value_evidence':'official_broker_close'}}})


def test_bound_principal_is_estimated_and_source_conflict_disables_config_fallback(monkeypatch):
    import os
    from uuid import uuid4
    import psycopg2
    from psycopg2.extras import RealDictCursor
    import pytest
    from domain.accounts.balance_reference import load_bound_balance_references
    from domain.imports import isolation
    from domain.imports.sources import source_records_v2 as sources
    from domain.investments.valuation.valuation_input_evidence import _bound_input, _hash, record_result_version
    from domain.investments.valuation import valuation_input_evidence
    from domain.investments.holdings.position_nav_projection import load_position_nav_estimates
    from domain.investments.valuation.valuation_nav import _add_known_display_values, latest_nav_display_values

    dsn = os.getenv('PAMS_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('requires explicitly isolated PostgreSQL')
    code, day = 'benefit_principal_fixture', date(2025, 12, 31)
    source, item = uuid4(), uuid4()
    fields = dict(account_code=code, business_date=day.isoformat(), currency='CNY',
                  reference_basis='contributed_principal', reference_amount='17')
    ref = dict(amount='17.00', as_of=day.isoformat(), currency='CNY', source='annual statement')
    cfg = {'base_currency': 'CNY', 'account_balance_reference': ref}
    with psycopg2.connect(dsn) as conn:
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                assert isolation.source_mode() == 'test' and isolation.source_enabled(cur)
                cur.execute("INSERT INTO pams_accounts(account_code,display_name) VALUES (%s,'Synthetic')", (code,))
                sources.create_source(cur, source_id=source, account_code=code, source_name='Synthetic',
                                      acquisition_method='file', document_type='statement', filename='statement.pdf',
                                      file_format='pdf', content=b'synthetic principal reference')
                sources.add_item(cur, source_id=source, item_id=item, parse_method='manual',
                                 raw_fields={'entered_fields': fields}, parsed_fields=fields)
                sources.review_item(cur, source_id=source, item_id=item, expected_revision=2,
                                    parsed_fields=fields, review_status='confirmed', note='Principal checked')
                cur.execute("UPDATE source_records SET status='published',revision=revision+1 WHERE source_id=%s", (str(source),))
                inp, _ = _bound_input(cur, 'source_item', {'source_item_id': str(item)})
                inp['role'] = 'reported_principal_reference'
                record_result_version(cur, result_kind='portfolio', scope_key='account-balance-reference:'+code,
                                      account_code=code, business_date=day, base_currency='CNY',
                                      calculation_version='historical-principal-reference-v1',
                                      result_hash=_hash(fields), inputs=[inp])
                configs = {code: cfg, 'unbound': cfg}
                bindings = load_bound_balance_references(cur, configs)
                assert set(bindings) == {code}
                assert historical_account_balance(cfg, day, Decimal('0.5'), account_code=code, bindings=bindings) == {
                    'balance_amount': '8.5', 'balance_currency': 'CNY', 'balance_label': '历史参考本金',
                    'balance_basis': 'historical_account_reference', 'balance_as_of': day,
                    'balance_reference_basis': 'contributed_principal'}
                args = dict(account_balances=[{'account_code': code}], investment_positions=[], terminal_date=day,
                            confirmed_values={}, account_config={'accounts': configs}, cur=cur)
                row = {'available_net_worth': Decimal(0), 'unavailable_accounts': [code], 'net_worth': None}
                _add_known_display_values(row, latest_nav_display_values(**args))
                assert row['estimated_accounts'] == [code] and row['available_net_worth'] == Decimal('17')
                assert row['net_worth'] is None and row['unavailable_accounts'] == [code]
                following = date(2026, 1, 1)
                estimates = dict(accounts={code: cfg}, dates=[day, following], account_owner_ratio=lambda _: Decimal('0.5'),
                                 owner_ratio_case_sql=lambda _: ('1', []), rate_at=lambda *_: Decimal(1))
                assert load_position_nav_estimates(cur, **estimates)[following][code] == (Decimal('8.5'), day, day)
                for bad_ref in ({**ref, 'amount': '18'}, {**ref, 'as_of': '2026-01-01'}):
                    assert load_bound_balance_references(cur, {code: {**cfg, 'account_balance_reference': bad_ref}}) == {code: None}
                def invalidated_original(*args, **kwargs):
                    current, raw = _bound_input(*args, **kwargs)
                    current['snapshot']['source_integrity'] = 'unknown'
                    return current, raw
                monkeypatch.setattr(valuation_input_evidence, '_bound_input', invalidated_original)
                assert code not in latest_nav_display_values(**args)
                assert not load_position_nav_estimates(cur, **estimates)
        finally:
            conn.rollback()
