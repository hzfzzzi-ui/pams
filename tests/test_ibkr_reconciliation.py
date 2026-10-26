"""Read-only evidence must never become another asset or performance source."""
from copy import deepcopy
from dataclasses import replace
from datetime import date
import hashlib
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

import pytest
import psycopg2.extras
from psycopg2.extensions import connection, ISOLATION_LEVEL_REPEATABLE_READ

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend/api'))
from collectors.ibkr_flex import parse_flex_report
from services.ibkr_report_state import PublishedReport
from services.ibkr_reconciliation import reconcile_report, read_ibkr_reconciliation
from test_ibkr_sync_transaction import database as database, sync_case as sync_case, business_state, rows

XML = '''<FlexQueryResponse><FlexStatements><FlexStatement accountId="U_TEST" fromDate="20261001" toDate="20261022">
<OpenPositions><OpenPosition accountId="U_TEST" symbol="TEST" description="TEST STOCK" currency="USD"
 reportDate="20261022" position="2" markPrice="190.5" positionValue="381" marketValue="381" costBasisMoney="340" /></OpenPositions>
<CashReportCurrency currency="BASE_SUMMARY" toDate="20261022" endingCash="-500" depositWithdrawalsYTD="100" />
<CashReportCurrency accountId="U_TEST" currency="USD" toDate="20261022" endingCash="-500" />
<Trades><Trade accountId="U_TEST" symbol="TEST" currency="USD" tradeID="T1" tradeDate="20261022"
 buySell="BUY" quantity="2" tradePrice="170" tradeMoney="340" ibCommission="-1.25" netCash="-341.25" /></Trades>
</FlexStatement></FlexStatements></FlexQueryResponse>'''


@pytest.fixture
def case(tmp_path):
    parsed = parse_flex_report(XML)
    stock = next(r for r in parsed.rows if r['symbol'] == 'TEST')
    return {'report': PublishedReport(tmp_path/'report.xml', date(2026,10,22), 10,
                hashlib.sha256(XML.encode()).hexdigest(), None),
            'xml_text': XML, 'quality': {'status':'ok','legacy_unverified':False,
                'statement_as_of':'2026-10-22','expected_statement_as_of':'2026-10-22'},
            'trades': [{**parsed.trades[0], 'id':11}],
            'facts': [{'source_id':'11','source_key':'ibkr:'+parsed.trades[0]['source_trade_key'],
                'account_code':'ibkr','currency':'USD','symbol':'TEST',
                'trade_date':date(2026,10,22),'side':'buy','quantity':2,'gross_amount':340,
                'fee_amount':1.25,'net_cash':-341.25}],
            'history':[{**stock,'id':21}], 'snapshots':[{**stock,'id':31}]}


def check(result, name):
    return next(row for row in result['checks'] if row['id'] == name)


def test_match_is_read_only_and_ignores_price_cost_nav_and_fx(case):
    case['snapshots'][0].update(market_value=999999, cost=-500, current_price=2000)
    before = deepcopy(case)
    result = reconcile_report(**case)
    assert result['status'] == 'ok' and result['read_only']
    assert case == before
    assert all(row['issue_count'] == 0 for row in result['checks'])
    assert 'NAV' in result['scope']['not_compared']


@pytest.mark.parametrize('table,field,value', [
    ('trades','net_cash',341.25), ('trades','commission',-2.25),
    ('facts','net_cash',341.25), ('facts','fee_amount',2.25),
    ('facts','gross_amount',341), ('history','quantity',3), ('snapshots','quantity',3),
])
def test_native_amount_fee_sign_and_quantity_mismatches(case, table, field, value):
    case[table][0][field] = value
    result = reconcile_report(**case)
    assert result['status'] == 'error'
    issues = [i for row in result['checks'] for i in row['issues']]
    item = next(i for i in issues if i.get('field') == field)
    assert item['currency'] == 'USD' and item['delta'] and item['tolerance']


@pytest.mark.parametrize('table', ['trades','facts','history','snapshots'])
def test_missing_and_duplicate_rows_are_not_success(case, table):
    original = case[table][0]
    case[table] = []
    assert reconcile_report(**case)['status'] != 'ok'
    case[table] = [original, {**original, 'id':999}]
    assert reconcile_report(**case)['status'] != 'ok'


@pytest.mark.parametrize('field,value', [('currency','HKD'),('source_account_id','OTHER'),('trade_date',date(2026,10,21))])
def test_wrong_account_currency_or_date_is_not_net_compared(case, field, value):
    case['trades'][0][field] = value
    case['trades'][0]['net_cash'] = 123
    result = reconcile_report(**case)
    assert result['status'] != 'ok'
    assert not any(i.get('field') == 'net_cash' for i in check(result,'trade_ledger')['issues'])


@pytest.mark.parametrize('attribute', ['tradeMoney="340"', 'ibCommission="-1.25"', 'netCash="-341.25"'])
def test_missing_official_values_are_not_filled_with_zero_or_inferred(case, attribute):
    case['xml_text'] = case['xml_text'].replace(attribute, '')
    result = reconcile_report(**case)
    assert result['status'] != 'ok'
    assert any(i['reason'] == 'missing_value' for i in check(result,'trade_ledger')['issues'])


def test_current_snapshot_different_day_is_review_not_price_or_quantity_error(case):
    case['snapshots'][0].update(as_of=date(2026,10,23), quantity=100)
    result = reconcile_report(**case)
    snapshot = check(result,'current_snapshot')
    assert snapshot['status'] == 'review' and snapshot['compared'] == 0
    assert snapshot['issues'][0]['reason'] == 'snapshot_date_mismatch'


@pytest.mark.parametrize('quality', [{'status':'stale'}, {'status':'review','legacy_unverified':True}])
def test_legacy_or_stale_report_never_confirms_today(case, quality):
    case['quality'].update(quality)
    result = reconcile_report(**case)
    assert result['status'] == 'review'
    assert check(result,'source')['status'] == 'review'


def test_source_duplicate_is_ambiguous_not_silently_deduplicated(case):
    start = case['xml_text'].index('<Trade ')
    stop = case['xml_text'].index('/>',start)+2
    case['xml_text'] = case['xml_text'][:stop] + case['xml_text'][start:stop] + case['xml_text'][stop:]
    assert check(reconcile_report(**case),'trade_ledger')['status'] == 'review'


def test_read_failure_does_not_leak_credentials(tmp_path):
    def fail():
        raise RuntimeError('postgresql://token=secret@host')
    result = read_ibkr_reconciliation(connection_factory=fail, raw_dir=tmp_path)
    assert result['status'] == 'error' and result['read_only']
    assert 'secret' not in str(result)


@pytest.mark.parametrize('value', [None, 'NaN', 'Infinity', 'invalid'])
def test_invalid_stored_numbers_are_not_zero(case, value):
    case['facts'][0]['net_cash'] = value
    result = reconcile_report(**case)
    assert check(result,'trade_facts')['status'] == 'review'


def test_empty_account_scope_does_not_report_unexecuted_checks_as_ok(case):
    case['xml_text'] = case['xml_text'].replace('accountId="U_TEST"','')
    result = reconcile_report(**case)
    assert result['status'] == 'review' and result['counts']['ok'] == 0


def test_cross_currency_fee_is_not_subtracted_or_converted(case):
    case['xml_text'] = case['xml_text'].replace('ibCommission="-1.25"','ibCommission="-1.25" ibCommissionCurrency="HKD"')
    result = reconcile_report(**case)
    assert result['status'] == 'review'
    assert any(i['reason'] == 'fee_currency_mismatch' for i in check(result,'trade_ledger')['issues'])


def test_numeric_precision_and_issue_limit_are_explicit(case):
    case['facts'][0]['quantity'] = '2.000000005'
    assert reconcile_report(**case)['status'] == 'ok'
    trade = case['trades'][0]
    for number in range(50):
        case['trades'].append({**trade, 'id':number+100, 'trade_id':f'OTHER{number}'})
    result = reconcile_report(**case)
    assert result['scope']['ledger_rows_not_covered'] == 50
    assert result['status'] == 'review'  # Not silently declared 50 extra erroneous trades.


def test_wrong_ledger_side_cannot_pass(case):
    case['trades'][0]['side'] = 'SELL'
    assert check(reconcile_report(**case),'trade_ledger')['status'] == 'review'


def test_recycled_physical_id_never_compares_an_unrelated_fact(case):
    case['facts'][0].update(source_key='ibkr:another-execution', net_cash=999, symbol='OTHER')
    result = reconcile_report(**case)
    fact = check(result,'trade_facts')
    assert fact['status'] == 'review' and fact['compared'] == 0
    assert fact['issues'][0]['reason'] == 'source_link_mismatch'


def test_stable_key_can_compare_amounts_but_does_not_repair_old_source_id(case):
    case['facts'][0]['source_id'] = '999'
    before = deepcopy(case)
    result = reconcile_report(**case)
    fact = check(result,'trade_facts')
    assert fact['status'] == 'review' and fact['compared'] == 1
    assert fact['issues'][0]['reason'] == 'source_link_mismatch' and case == before


def test_cash_only_empty_trade_section_is_supported_without_cash_valuation(case):
    start,stop = case['xml_text'].index('<OpenPosition '),case['xml_text'].index('</OpenPositions>')
    case['xml_text'] = case['xml_text'][:start] + case['xml_text'][stop:]
    start,stop = case['xml_text'].index('<Trade '),case['xml_text'].index('</Trades>')
    case['xml_text'] = case['xml_text'][:start] + case['xml_text'][stop:]
    case['trades'],case['facts'],case['history'] = [],[],[]
    case['snapshots'] = [{'symbol':'CASH_USD','as_of':date(2026,10,22),'quantity':-999}]
    assert reconcile_report(**case)['status'] == 'ok'


def test_missing_trade_section_is_not_a_confirmed_zero(case):
    start,stop = case['xml_text'].index('<Trades>'),case['xml_text'].index('</Trades>')+len('</Trades>')
    case['xml_text'] = case['xml_text'][:start]+case['xml_text'][stop:]
    result = reconcile_report(**case)
    assert check(result,'trade_facts')['status'] == 'review'


def test_reconciliation_issue_samples_are_bounded_without_losing_total(case):
    original = case['xml_text'][case['xml_text'].index('<Trade '):case['xml_text'].index('</Trades>')]
    case['xml_text'] = case['xml_text'].replace(original, ''.join(original.replace('T1',f'T{i}') for i in range(60)))
    case['trades'] = [{**trade,'id':i,'quantity':100} for i,trade in enumerate(parse_flex_report(case['xml_text']).trades)]
    result = reconcile_report(**case)
    ledger = check(result,'trade_ledger')
    assert ledger['issue_count'] == 60 and len(ledger['issues']) == 30 and ledger['truncated']


def test_real_postgres_is_readonly_and_does_not_mutate_any_business_or_audit_rows(sync_case, database, monkeypatch, tmp_path):
    from services import ibkr_reconciliation as audit
    statements = []
    class GuardCursor(psycopg2.extras.RealDictCursor):
        def execute(self, query, args=None):
            assert self.connection.readonly is True
            assert self.connection.isolation_level == ISOLATION_LEVEL_REPEATABLE_READ
            assert str(query).lstrip().upper().startswith(('SELECT','SET LOCAL'))
            statements.append(str(query))
            return super().execute(query,args)
    class GuardConnection(connection):
        def cursor(self, *args, **kwargs):
            kwargs['cursor_factory'] = GuardCursor
            return super().cursor(*args,**kwargs)
    monkeypatch.setattr(audit, 'get_conn', lambda: database(connection_factory=GuardConnection))
    client, _, _ = sync_case
    before = {**business_state(database), 'runs':rows(database,'pams_data_sync_runs'),
              'jobs':rows(database,'pams_data_job_run')}
    files = sorted(str(p) for p in tmp_path.rglob('*.xml'))
    for _ in range(2):
        response = client.get('/api/ibkr/reconciliation')
        assert response.status_code == 200
        result = response.json()
        assert result['status'] == 'ok' and result['read_only']
        assert result['scope']['cash_rows_excluded'] == 1
    assert statements and not any('CREATE ' in sql or 'UPDATE ' in sql for sql in statements)
    after = {**business_state(database), 'runs':rows(database,'pams_data_sync_runs'),
             'jobs':rows(database,'pams_data_job_run')}
    assert before == after and files == sorted(str(p) for p in tmp_path.rglob('*.xml'))


def test_server_rejects_accidental_write_and_diagnostics_do_not_hide_it(sync_case, database, monkeypatch):
    from services import ibkr_reconciliation as audit
    before = business_state(database)
    def write_by_mistake(conn, raw_dir):
        with conn.cursor() as cur:
            cur.execute('UPDATE pams_holdings_snapshot SET quantity=0')
        pytest.fail('PostgreSQL must reject the write')
    monkeypatch.setattr(audit,'select_report',write_by_mistake)
    result = read_ibkr_reconciliation(connection_factory=database)
    assert result['checks'][0]['issues'][0]['error_type'] == 'ReadOnlySqlTransaction'
    assert business_state(database) == before


def test_missing_table_is_not_created_at_runtime(sync_case, database):
    conn = database()
    with conn, conn.cursor() as cur:
        cur.execute('DROP TABLE pams_investment_trade_fact')
    conn.close()
    result = read_ibkr_reconciliation(connection_factory=database)
    assert result['status'] == 'review'
    assert result['checks'][0]['issues'][0]['reason'] == 'missing_table'
    conn = database()
    with conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.pams_investment_trade_fact')")
        assert cur.fetchone()[0] is None
    conn.close()


@pytest.fixture
def read_case(case, monkeypatch):
    from services import ibkr_report_state as state

    expected_day = case['report'].as_of
    monkeypatch.setattr(state, 'expected_statement_date', lambda: expected_day)

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, query, args=None):
            assert query.lstrip().upper().startswith(('SELECT', 'SET LOCAL'))
            self.query = query

        def fetchone(self):
            return {'exists': True, 'present': True}

        def fetchall(self):
            if 'FROM pams_data_sync_runs' in self.query:
                report = case['report']
                return [{'id': report.run_id, 'status': 'success', 'summary_json': {
                    'raw_file': str(report.path), 'statement_as_of': str(report.as_of),
                    'publication': {'state': 'published', 'version': 1, 'account_code': 'ibkr',
                                    'sha256': report.sha256, 'report_date': str(report.as_of)}}}]
            table = self.query.split('FROM ', 1)[1].split()[0]
            key = {'pams_ibkr_trades': 'trades', 'pams_investment_trade_fact': 'facts',
                   'pams_ibkr_position_history': 'history', 'pams_holdings_snapshot': 'snapshots'}[table]
            return deepcopy(case[key])

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def set_session(self, **kwargs):
            assert kwargs == {'readonly': True, 'isolation_level': 'REPEATABLE READ'}

        def cursor(self, **kwargs):
            return Cursor()

        def close(self):
            pass

    def read():
        content = case['xml_text'].encode('utf-8')
        case['report'].path.write_bytes(content)
        case['report'] = replace(case['report'], sha256=hashlib.sha256(content).hexdigest())
        return read_ibkr_reconciliation(connection_factory=Connection, raw_dir=case['report'].path.parent)

    return read


def test_request_reuses_parse_and_tree_without_cross_request_cache(case, read_case, monkeypatch):
    from collectors import ibkr_flex as collector
    from services import ibkr_fee_review as fees, ibkr_reconciliation as audit

    inputs = {**case, 'quality': read_case()['report']['quality']}
    expected = reconcile_report(**inputs)
    expected['fee_review'] = fees.review_fees(**{key: inputs[key] for key in ('xml_text', 'quality', 'trades', 'facts')})
    counts = {'parsed': 0, 'xml': 0}
    real_parse, real_xml = collector.parse_flex_report, ET.fromstring

    def parse(text):
        counts['parsed'] += 1
        return real_parse(text)

    def root(text):
        counts['xml'] += 1
        return real_xml(text)

    monkeypatch.setattr(audit, 'parse_flex_report', parse)
    monkeypatch.setattr(fees, 'parse_flex_report', parse)
    monkeypatch.setattr(ET, 'fromstring', root)
    for request in range(1, 3):
        assert read_case() == expected
        # One selector inspection, one collector parse, one shared ordinary tree.
        assert counts == {'parsed': request, 'xml': 3 * request}


@pytest.mark.parametrize('change', ['none', 'cross_currency', 'missing_fee', 'amount',
                                  'scope', 'source_link', 'duplicate', 'snapshot_date', 'stale'])
def test_shared_request_dto_deeply_matches_standalone_calls(case, read_case, change):
    from services.ibkr_fee_review import review_fees

    if change == 'cross_currency':
        case['xml_text'] = XML.replace('ibCommission="-1.25"', 'ibCommission="-1.25" ibCommissionCurrency="HKD"')
    elif change == 'missing_fee':
        case['xml_text'] = XML.replace('ibCommission="-1.25"', '')
    elif change == 'amount':
        case['facts'][0]['net_cash'] = 99
    elif change == 'scope':
        case['trades'][0]['currency'] = 'HKD'
    elif change == 'source_link':
        case['facts'][0]['source_id'] = '999'
    elif change == 'duplicate':
        case['facts'].append(deepcopy(case['facts'][0]))
    elif change == 'snapshot_date':
        case['snapshots'][0]['as_of'] = date(2026, 10, 23)
    elif change == 'stale':
        case['xml_text'] = XML.replace('20261022', '20261021')
        case['report'] = replace(case['report'], as_of=date(2026, 10, 21))
    result = read_case()
    # Use the exact quality emitted by the selector, including its warning fields.
    inputs = {**case, 'quality': result['report']['quality']}
    expected = reconcile_report(**inputs)
    expected['fee_review'] = review_fees(**{key: inputs[key] for key in ('xml_text', 'quality', 'trades', 'facts')})
    assert result == expected


@pytest.mark.parametrize('child', ['_review_fees', '_review_fee_settlement'])
def test_shared_request_child_failure_preserves_other_diagnostics(case, read_case, monkeypatch, child):
    from services import ibkr_fee_review as fees

    expected = read_case()

    def fail(**kwargs):
        raise RuntimeError('private diagnostic text')

    monkeypatch.setattr(fees, child, fail)
    result = read_case()
    actual_fees, expected_fees = result.pop('fee_review'), expected.pop('fee_review')
    assert result == expected and 'private diagnostic text' not in str(actual_fees)
    if child == '_review_fees':
        assert actual_fees['status'] == 'unavailable' and actual_fees['error_type'] == 'RuntimeError'
        assert actual_fees['settlement_evidence'] == expected_fees['settlement_evidence']
    else:
        unavailable = actual_fees.pop('settlement_evidence')
        expected_fees.pop('settlement_evidence')
        assert actual_fees == expected_fees
        assert unavailable['status'] == 'unavailable' and unavailable['error_type'] == 'RuntimeError'


def test_shared_request_rechecks_original_bytes_before_parsing(case, read_case, monkeypatch):
    from services import ibkr_reconciliation as audit

    select = audit.select_report

    def changed_report(conn, raw_dir):
        report, latest = select(conn, raw_dir)
        report.path.write_bytes(report.path.read_bytes() + b' ')
        return report, latest

    def unexpected_parse(text):
        pytest.fail('Changed report bytes must be rejected before the collector parse')

    monkeypatch.setattr(audit, 'select_report', changed_report)
    monkeypatch.setattr(audit, 'parse_flex_report', unexpected_parse)
    result = read_case()
    assert result['status'] == 'error' and 'fee_review' not in result
    assert check(result, 'source')['issues'][0]['error_type'] == 'ValueError'
