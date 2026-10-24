"""Fee evidence is not permission to recalculate or repair money."""
from copy import deepcopy
from datetime import date
from pathlib import Path
import sys

import psycopg2.extras
from psycopg2.extensions import connection, ISOLATION_LEVEL_REPEATABLE_READ
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend/api'))
from collectors.ibkr_flex import parse_flex_report
from services.ibkr_fee_review import review_fees
from services.investment_trade_fact import build_investment_trade_fact
from test_ibkr_sync_transaction import database as database, sync_case as sync_case, business_state, rows

XML = '''<FlexQueryResponse><FlexStatements><FlexStatement accountId="U_TEST" toDate="20261022">
<AccountInformation accountId="U_TEST" currency="USD" />
<OpenPosition accountId="U_TEST" symbol="TEST" currency="USD" reportDate="20261022"
 position="2" markPrice="190.5" positionValue="381" costBasisMoney="340" assetCategory="STK" />
<CashReportCurrency currency="BASE_SUMMARY" fromDate="20261022" toDate="20261022"
 endingCash="-500" depositWithdrawalsYTD="100" commissions="-3" />
<CashReportCurrency accountId="U_TEST" currency="USD" fromDate="20261022" toDate="20261022" endingCash="-500" />
<Trades><Trade accountId="U_TEST" symbol="USD.MXN" assetCategory="CASH" currency="MXN" tradeID="FX1"
 tradeDate="20261022" buySell="BUY" quantity="100" tradePrice="20" tradeMoney="2000"
 ibCommission="-2" ibCommissionCurrency="USD" netCash="0" fxRateToBase="0.05" />
<Trade accountId="U_TEST" symbol="TEST" assetCategory="STK" currency="USD" tradeID="T1"
 tradeDate="20261022" buySell="BUY" quantity="2" tradePrice="170" tradeMoney="340"
 ibCommission="-1" ibCommissionCurrency="USD" netCash="-341" fxRateToBase="1" />
</Trades></FlexStatement></FlexStatements></FlexQueryResponse>'''


def make_case(xml=XML):
    parsed = parse_flex_report(xml)
    trades, facts = [], []
    for i, row in enumerate(parsed.trades):
        source = {**row, 'id': i + 11, 'raw_json': row['raw']}
        trades.append(source)
        fact = build_investment_trade_fact({**source, 'source_table': 'pams_ibkr_trades',
            'source_id': str(source['id']), 'source_key': 'ibkr:' + row['source_trade_key'],
            'market': row['asset_category'], 'fee_amount': abs(row['commission']), 'fx_rate_to_cny': 7})
        fact['trade_id'] = i + 21
        facts.append(fact)
    return {'xml_text': xml, 'quality': {'status': 'ok', 'legacy_unverified': False,
            'statement_as_of': '2026-10-22', 'expected_statement_as_of': '2026-10-22'},
            'trades': trades, 'facts': facts}


def record(case):
    return review_fees(**case)['records'][0]


def test_cross_currency_numeric_evidence_and_group_cash_do_not_confirm_units_or_repair():
    case = make_case()
    before = deepcopy(case)
    result = review_fees(**case)
    item = result['records'][0]
    assert case == before and result['read_only'] and not result['executable']
    assert result['status'] == 'review' and result['summary']['numeric_evidence_matched_rows'] == 1
    assert result['summary']['signed_official_fees_by_currency'] == {'USD': '-2'}
    assert item['fee_unit_evidence'] == 'raw_and_embedded_only_no_fact_fee_currency_column'
    assert not item['stored_fee_numeric_currency_is_explicit']
    assert item['daily_cash_commission_evidence']['status'] == 'matched'
    assert item['daily_cash_commission_evidence']['expected'] == '-3'
    assert not item['daily_cash_commission_evidence']['individual_allocation_verified']
    assert item['impact']['cash']['status'] == 'source_value_preserved'
    assert item['impact']['cash']['fee_inclusion_in_individual_cash'] == 'unverified'
    assert item['impact']['cost']['status'] == 'excluded_from_equity_reconstruction'
    assert item['impact']['pnl']['individual_fee_effect_on_nav_or_pnl'] is None
    assert result['existing_fee_and_lineage_blockers_preserved']


@pytest.mark.parametrize('value', ['0', '2', '-2'])
def test_explicit_zero_and_signed_commission_are_not_guessed(value):
    case = make_case(XML.replace('ibCommission="-2"', f'ibCommission="{value}"'))
    item = record(case)
    assert item['checks']['fact_fee_numeric']['status'] == 'matched'
    assert item['official_commission_signed'] == value
    assert item['commission_sign'] == ('zero' if value == '0' else 'positive' if value == '2' else 'negative')
    assert (review_fees(**case)['summary']['explicit_zero_fee_rows'] == 1) == (value == '0')


@pytest.mark.parametrize('value', ['', 'NaN', 'Infinity', 'invalid'])
def test_missing_or_nonfinite_official_fees_never_become_zero(value):
    case = make_case()
    case['xml_text'] = XML.replace('ibCommission="-2"', f'ibCommission="{value}"')
    result = review_fees(**case)
    assert result['summary']['explicit_zero_fee_rows'] == 0
    assert result['summary']['numeric_evidence_matched_rows'] == 0
    assert record(case)['official_commission_signed'] is None


@pytest.mark.parametrize('field,value', [('fee_amount', 3), ('fee_amount', None), ('net_cash', 2), ('quantity', 3)])
def test_fact_numeric_changes_are_evidence_mismatches_not_repaired(field, value):
    case = make_case()
    case['facts'][0][field] = value
    before = deepcopy(case)
    item = record(case)
    assert 'numeric_evidence_not_matched' in item['issues']
    assert review_fees(**case)['summary']['numeric_evidence_matched_rows'] == 0
    assert case == before


@pytest.mark.parametrize('field,value', [('currency', 'HKD'), ('account_code', 'other'),
    ('trade_date', date(2026,10,21)), ('symbol', 'OTHER'), ('side', 'sell'), ('source_table', 'other')])
def test_scope_mismatch_never_compares_money(field, value):
    case = make_case()
    case['facts'][0][field] = value
    item = record(case)
    assert item['issues'] == ['scope_mismatch'] and item['checks'] == {} and item['impact'] is None


@pytest.mark.parametrize('table', ['trades', 'facts'])
def test_duplicate_or_missing_identity_does_not_select_first(table):
    case = make_case()
    case[table].append(deepcopy(case[table][0]))
    assert record(case)['issues'] == ['missing_or_ambiguous_execution_or_fact']
    case[table] = case[table][1:2]
    assert record(case)['issues'] == ['missing_or_ambiguous_execution_or_fact']


def test_duplicate_official_execution_is_not_deduplicated():
    original = XML[XML.index('<Trade '):XML.index('/>', XML.index('<Trade ')) + 2]
    case = make_case(XML.replace(original, original + original))
    result = review_fees(**case)
    assert result['total'] == 2
    assert all(item['issues'] == ['missing_or_ambiguous_execution_or_fact'] for item in result['records'])
    assert result['summary']['signed_official_fees_by_currency'] is None
    assert not result['summary']['official_subtotal_complete']


def test_recycled_physical_id_uses_stable_key_without_repairing_link():
    case = make_case()
    case['facts'][0]['source_id'] = '999'
    item = record(case)
    assert not item['source_link_matches'] and item['checks']['fact_fee_numeric']['status'] == 'matched'
    assert case['facts'][0]['source_id'] == '999' and not item['executable']


@pytest.mark.parametrize('path', ['source', 'embedded', 'embedded_identity'])
def test_conflicting_currency_or_embedded_identity_does_not_prove_fee_unit(path):
    case = make_case()
    if path == 'source':
        case['trades'][0]['raw_json']['ibCommissionCurrency'] = 'EUR'
        case['trades'][0]['raw'] = case['trades'][0]['raw_json']
    elif path == 'embedded':
        case['facts'][0]['raw_json']['source_row']['raw_json']['ibCommissionCurrency'] = 'EUR'
    else:
        case['facts'][0]['raw_json']['source_row']['raw_json']['tradeID'] = 'OTHER'
    assert record(case)['fee_unit_evidence'] == 'unverified'


def test_missing_official_currency_is_not_defaulted_to_trade_currency():
    case = make_case(XML.replace('ibCommissionCurrency="USD"', '', 1))
    item = record(case)
    assert item['fee_currency'] is None and 'missing_official_fee_currency' in item['issues']


@pytest.mark.parametrize('attribute', ['currency="MXN"', 'tradeDate="20261022"'])
def test_missing_trade_scope_does_not_accept_parser_fallback(attribute):
    case = make_case(XML.replace(attribute, '', 1))
    assert record(case)['issues'] == ['scope_mismatch']


@pytest.mark.parametrize('replacement', [
    ('<AccountInformation accountId="U_TEST" currency="USD" />', ''),
    ('fromDate="20261022" toDate="20261022"', 'fromDate="20260101" toDate="20261022"'),
    ('commissions="-3"', 'commissions="NaN"'),
])
def test_missing_daily_base_cash_evidence_is_unverified(replacement):
    case = make_case(XML.replace(*replacement))
    assert record(case)['daily_cash_commission_evidence']['status'] == 'unverified'


def test_daily_aggregate_difference_is_not_allocated_to_one_execution():
    case = make_case(XML.replace('commissions="-3"', 'commissions="-1"'))
    cash = record(case)['daily_cash_commission_evidence']
    assert cash['status'] == 'mismatch' and cash['delta'] == '2'
    assert not cash['individual_allocation_verified']


def test_cash_report_other_account_is_not_comparable():
    case = make_case(XML.replace('currency="BASE_SUMMARY"', 'accountId="OTHER" currency="BASE_SUMMARY"'))
    result = review_fees(**case)
    if result['total'] is not None:
        assert result['records'][0]['daily_cash_commission_evidence']['status'] == 'unverified'
    else:
        assert result['reason'] == 'unverified_report_or_account'


def test_duplicate_daily_cash_is_not_summed_or_first_selected():
    start = XML.index('<CashReportCurrency currency="BASE_SUMMARY"')
    cash = XML[start:XML.index('/>', start) + 2]
    case = make_case(XML.replace(cash, cash + cash))
    assert record(case)['daily_cash_commission_evidence']['reason'] == 'missing_or_duplicate_daily_cash_report'


def test_missing_fx_for_third_currency_is_not_replaced_with_latest_rate():
    case = make_case(XML.replace('ibCommissionCurrency="USD"', 'ibCommissionCurrency="EUR"', 1))
    assert record(case)['daily_cash_commission_evidence']['reason'] == 'missing_official_fee_fx'


@pytest.mark.parametrize('rate', ['0', '-1', 'NaN', ''])
def test_foreign_same_currency_daily_fee_requires_official_positive_fx(rate):
    xml = XML.replace('symbol="TEST" assetCategory="STK" currency="USD"', 'symbol="TEST" assetCategory="STK" currency="EUR"')
    xml = xml.replace('ibCommissionCurrency="USD" netCash="-341" fxRateToBase="1"',
                      f'ibCommissionCurrency="EUR" netCash="-341" fxRateToBase="{rate}"')
    assert record(make_case(xml))['daily_cash_commission_evidence']['reason'] == 'missing_official_fee_fx'


def test_foreign_commissions_convert_only_with_same_report_rate():
    xml = XML.replace('symbol="TEST" assetCategory="STK" currency="USD"', 'symbol="TEST" assetCategory="STK" currency="EUR"')
    xml = xml.replace('ibCommissionCurrency="USD" netCash="-341" fxRateToBase="1"',
                      'ibCommissionCurrency="EUR" netCash="-341" fxRateToBase="1.5"')
    xml = xml.replace('commissions="-3"', 'commissions="-3.5"')
    cash = record(make_case(xml))['daily_cash_commission_evidence']
    assert cash['status'] == 'matched' and cash['expected'] == '-3.5'


def test_equity_cross_currency_fee_does_not_claim_cost_path_excluded():
    xml = XML.replace('symbol="USD.MXN" assetCategory="CASH"', 'symbol="FOREIGN_STOCK" assetCategory="STK"')
    item = record(make_case(xml))
    assert item['impact']['cost']['status'] == 'requires_review'
    assert item['impact']['cost']['affects_cost_basis'] is True


def test_unexpected_diagnostic_failure_is_safe(monkeypatch):
    from services import ibkr_fee_review as audit
    def fail(**kwargs):
        raise RuntimeError('secret postgres password')
    monkeypatch.setattr(audit, '_review_fees', fail)
    result = audit.review_fees(**make_case())
    assert result['status'] == 'unavailable' and result['error_type'] == 'RuntimeError'
    assert 'secret' not in str(result)


@pytest.mark.parametrize('quality', [{'status': 'stale'}, {'legacy_unverified': True},
    {'expected_statement_as_of': '2026-10-23'}])
def test_stale_legacy_and_wrong_date_never_confirms_fees(quality):
    case = make_case()
    case['quality'].update(quality)
    assert review_fees(**case)['total'] is None


def test_missing_trade_section_is_not_confirmed_zero():
    case = make_case(XML[:XML.index('<Trades>')] + '</FlexStatement></FlexStatements></FlexQueryResponse>')
    assert review_fees(**case)['reason'] == 'missing_trade_section'


def test_sample_limit_does_not_lose_total(monkeypatch):
    from services import ibkr_fee_review as audit
    case = make_case(XML.replace('ibCommissionCurrency="USD" netCash="-341"', 'ibCommissionCurrency="EUR" netCash="-341"'))
    monkeypatch.setattr(audit, 'RECORD_LIMIT', 1)
    result = audit.review_fees(**case)
    assert result['total'] == 2 and len(result['records']) == 1 and result['truncated']


def test_malformed_xml_has_safe_error_type_not_source_text():
    case = make_case()
    case['xml_text'] = '<secret-token'
    result = review_fees(**case)
    assert result['status'] == 'unavailable' and 'secret-token' not in str(result)


def test_real_postgres_additive_api_preserves_checks_and_never_writes(sync_case, database, monkeypatch, tmp_path):
    from services import ibkr_reconciliation as audit
    statements = []
    class GuardCursor(psycopg2.extras.RealDictCursor):
        def execute(self, query, args=None):
            assert self.connection.readonly is True
            assert self.connection.isolation_level == ISOLATION_LEVEL_REPEATABLE_READ
            assert str(query).lstrip().upper().startswith(('SELECT', 'SET LOCAL'))
            statements.append(str(query))
            return super().execute(query, args)
    class GuardConnection(connection):
        def cursor(self, *args, **kwargs):
            kwargs['cursor_factory'] = GuardCursor
            return super().cursor(*args, **kwargs)
    client, _, report = sync_case
    report['xml'] = XML
    assert client.post('/api/ibkr/flex/sync', json={}).status_code == 200
    monkeypatch.setattr(audit, 'get_conn', lambda: database(connection_factory=GuardConnection))
    before = {**business_state(database), 'runs': rows(database, 'pams_data_sync_runs'),
              'jobs': rows(database, 'pams_data_job_run')}
    files = sorted(str(path) for path in tmp_path.rglob('*.xml'))
    seen = []
    real_review = audit.review_fees
    def observe(**kwargs):
        seen.append(deepcopy(kwargs))
        return real_review(**kwargs)
    monkeypatch.setattr(audit, 'review_fees', observe)
    first = client.get('/api/ibkr/reconciliation').json()
    second = client.get('/api/ibkr/reconciliation').json()
    assert first == second and first['fee_review']['summary']['reviewed_rows'] == 1
    assert first['fee_review']['summary']['numeric_evidence_matched_rows'] == 1
    assert first['read_only'] and first['fee_review']['existing_fee_and_lineage_blockers_preserved']
    original = {key: value for key, value in first.items() if key != 'fee_review'}
    monkeypatch.setattr(audit, 'review_fees', lambda **kwargs: {})
    no_fee = client.get('/api/ibkr/reconciliation').json()
    no_fee.pop('fee_review')
    assert original == no_fee and seen and statements
    from services import ibkr_fee_review as fee_audit
    def fail(**kwargs):
        raise RuntimeError('secret diagnostic error')
    monkeypatch.setattr(fee_audit, '_review_fees', fail)
    monkeypatch.setattr(audit, 'review_fees', real_review)
    failed_child = client.get('/api/ibkr/reconciliation').json()
    assert failed_child.pop('fee_review')['status'] == 'unavailable'
    assert failed_child == original and 'secret' not in str(failed_child)
    assert before == {**business_state(database), 'runs': rows(database, 'pams_data_sync_runs'),
                      'jobs': rows(database, 'pams_data_job_run')}
    assert files == sorted(str(path) for path in tmp_path.rglob('*.xml'))
