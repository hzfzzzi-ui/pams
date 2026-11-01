"""Real isolated empty-schema sync and reconciliation, with synthetic data only."""
# ruff: noqa: F811
from datetime import date, datetime, timezone
from hashlib import sha256

import psycopg2
import pytest
from fastapi.testclient import TestClient

from scripts.bootstrap_isolated_sources import initialize_isolated_sources
from tests.brokers.ibkr.test_ibkr_sync_transaction import XML
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401


@pytest.fixture
def synced(empty_source_db, monkeypatch, tmp_path):
    import main
    from collectors import ibkr_flex
    from domain.brokers.ibkr import ibkr_report_state, ibkr_performance, ibkr_reconciliation
    from endpoints import ibkr

    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'synthetic')", (date(2026, 10, 22),))
    monkeypatch.setenv('PAMS_HOLDING_EVIDENCE_ISOLATED', 'true')
    monkeypatch.setenv('PAMS_DATA_DIR', str(tmp_path))
    monkeypatch.delenv('PAMS_IBKR_CASH_EVENTS_AFTER', raising=False)
    for module in (main, ibkr, ibkr_performance, ibkr_reconciliation):
        monkeypatch.setattr(module, 'get_conn', lambda: psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(ibkr, '_credentials', lambda: ('token', 'query'))
    monkeypatch.setattr(ibkr_flex, 'request_flex_reference', lambda *a, **k: {'reference_code': 'TEST'})
    monkeypatch.setattr(ibkr_report_state, 'utc_now', lambda: datetime(2026, 10, 22, 22, tzinfo=timezone.utc))
    raw = XML.replace('ibCommission="-1.25"', 'ibCommission="-1.25" ibCommissionCurrency="USD" taxAmount="0"')
    raw = raw.replace('<FlexStatement accountId="U_TEST" toDate="20261022">',
                      '<FlexStatement accountId="U_TEST" toDate="20261022"><AccountInformation accountId="U_TEST" currency="USD" />')
    monkeypatch.setattr(ibkr_flex, 'fetch_flex_statement', lambda *a, **k: raw)
    with TestClient(main.app) as client:
        response = client.post('/api/ibkr/flex/sync', json={})
        assert response.status_code == 200, response.text
        assert response.json()['sync_run']['status'] == 'success'
        yield client, empty_source_db, monkeypatch


def checks(result):
    return {entry['id']: entry for entry in result['checks']}


def test_empty_schema_http_comparison_does_not_certify_identity_or_scope(synced):
    client, database, _ = synced
    result = client.get('/api/ibkr/reconciliation').json()
    parts = checks(result)
    assert result['status'] == 'review'
    assert result['scope']['source_authentication'] == result['scope']['holding_identity'] == 'unknown'
    assert result['scope']['snapshot_coverage'] == 'unknown'
    assert result['scope']['snapshot_cash_status'] == 'unknown'
    assert parts['trade_facts']['compared'] == 1
    record = parts['trade_facts']['records'][0]
    assert record['currency'] == 'USD' and record['raw_amounts']['gross_amount'] == '340'
    assert record['fact_amounts']['net_cash'] == '-341.2500000000'
    assert record['source_authentication'] == 'unknown'
    assert parts['current_snapshot']['compared'] == 1
    assert any(issue.get('field') == 'holding_identity' for issue in parts['current_snapshot']['issues'])
    assert all(issue['reason'] != 'missing_table' for part in parts.values() for issue in part['issues'])
    assert client.get('/api/ibkr/reconciliation').json() == result
    with psycopg2.connect(**database) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.pams_ibkr_trades'), to_regclass('public.pams_ibkr_position_history')")
        assert cur.fetchone() == (None, None)


def test_changed_fact_and_incomplete_position_values_are_not_certified(synced):
    client, database, _ = synced
    with psycopg2.connect(**database) as conn, conn.cursor() as cur:
        cur.execute("UPDATE pams_investment_trade_fact SET gross_amount=999 WHERE source_table='ibkr_flex_trade'")
        cur.execute("UPDATE pams_holdings_snapshot SET cost=NULL,current_price=NULL WHERE account_code='ibkr' AND symbol='TEST'")
    result = client.get('/api/ibkr/reconciliation').json()
    parts = checks(result)
    assert parts['trade_facts']['compared'] == 0
    assert parts['trade_facts']['records'][0]['raw_amounts']['gross_amount'] == '340'
    assert parts['trade_facts']['records'][0]['fact_amounts'] is None
    assert any(issue.get('field') == 'fact_source_verification' for issue in parts['trade_facts']['issues'])
    assert {'cost', 'current_price'} <= {issue.get('field') for issue in parts['current_snapshot']['issues']}
    assert result['scope']['snapshot_coverage'] == 'unknown'
    assert result['status'] != 'ok'


def test_disabling_writes_does_not_disable_verified_readonly_reconciliation(synced):
    client, _, monkeypatch = synced
    monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED', 'false')
    result = client.get('/api/ibkr/reconciliation').json()
    parts = checks(result)
    assert parts['trade_facts']['compared'] == 1
    assert parts['trade_facts']['records'][0]['status'] in {'consistent', 'partial'}
    assert result['scope']['source_authentication'] == 'unknown'
    assert result['status'] != 'ok'


def test_candidate_value_difference_does_not_assert_same_contract(synced):
    client, database, _ = synced
    with psycopg2.connect(**database) as conn, conn.cursor() as cur:
        cur.execute("UPDATE pams_holdings_snapshot SET quantity=3 WHERE account_code='ibkr' AND symbol='TEST'")
    result = client.get('/api/ibkr/reconciliation').json()
    current = checks(result)['current_snapshot']
    quantity = [issue for issue in current['issues'] if issue.get('field') == 'quantity']
    assert len(quantity) == 1 and quantity[0]['reason'] == 'value_mismatch'
    assert quantity[0]['comparison'] == 'candidate_only_identity_unknown'
    assert quantity[0]['status'] == 'review' and current['status'] == 'review'
    assert result['scope']['holding_identity'] == 'unknown'


def test_cross_report_observation_does_not_claim_fact_link(synced):
    _, database, _ = synced
    from psycopg2.extras import RealDictCursor
    from domain.brokers.ibkr.ibkr_report_batch import publish_report_batch
    from domain.brokers.ibkr.ibkr_report_state import PublishedReport, StoredReport
    from domain.brokers.ibkr.ibkr_unified_reconciliation import reconcile_unified_report
    with psycopg2.connect(**database) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT content FROM source_files WHERE file_format='xml'")
        content = bytes(cur.fetchone()['content']) + b'\n'
        digest = sha256(content).hexdigest()
        batch = publish_report_batch(cur, report_bytes=content, source_account_id='U_TEST',
                                     day=date(2026, 10, 22), report_sha256=digest)
        assert batch['existing_verified'] == 1
    with psycopg2.connect(**database) as conn:
        conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
        report = PublishedReport(StoredReport(content, 'synthetic.xml'), date(2026, 10, 22), 1, digest, None)
        result = reconcile_unified_report(conn, report, {'status': 'ok'})
    facts = checks(result)['trade_facts']
    assert facts['compared'] == 0 and facts['records'][0]['reason'] == 'source_link'
    assert facts['records'][0]['raw_amounts']['gross_amount'] == '340'
    assert facts['records'][0]['fact_amounts'] is None


def test_two_thousand_report_rows_share_one_xml_index_and_cache_clear(monkeypatch):
    """Every row is independently matched while the immutable XML is indexed once."""
    import re
    from domain.brokers.ibkr import ibkr_fact_source_verification as verifier
    from xml.etree import ElementTree as ET
    trade = re.search(r'<Trade\b[^>]*/>', XML).group()
    rows = [trade.replace('tradeID="T1"', f'tradeID="T{i}"') for i in range(2000)]
    content = XML.replace(trade, '<Trades>' + ''.join(rows) + '</Trades>').encode()
    digest = sha256(content).hexdigest()
    cache = verifier.EvidenceCache()
    original_document = verifier._document
    calls = []
    def counted(*args):
        calls.append(1)
        return original_document(*args)
    monkeypatch.setattr(verifier, '_document', counted)
    for row in rows:
        attrs = dict(ET.fromstring(row).attrib)
        raw = {'source_account_id':'U_TEST', 'official_attributes':attrs,
               'report_evidence':{'source_sha256':digest, 'confirmed_day':'2026-10-22',
                                  'broker_source':'ibkr_flex', 'evidence_role':'source_report'}}
        result = verifier._report_node(raw, content, 'ibkr_flex_trade', cache)
        assert result[-1] == ('tradeid', attrs['tradeID'])
    assert len(calls) == 1 and len(cache.node_indexes) == 1 and len(cache.statement_checks) == 1
    with pytest.raises(verifier.EvidenceUnavailable, match='report_hash_mismatch'):
        verifier._report_node(raw, content + b' ', 'ibkr_flex_trade', cache)
    cache.clear()
    verifier._report_node(raw, content, 'ibkr_flex_trade', cache)
    assert len(calls) == 2


def test_unified_fee_review_is_readonly_and_does_not_certify_tampered_fact(synced):
    client,database,_=synced
    initial=client.get('/api/ibkr/reconciliation').json()['fee_review']
    assert initial['status']=='ok' and initial['total']==0 and initial['read_only']
    assert {'fact_fee_currency_matched_rows','daily_cash_aggregate_matched_rows',
            'equity_reconstruction_excluded_rows','nonzero_fee_rows','explicit_zero_fee_rows'} <= set(initial['summary'])
    assert initial['settlement_evidence']['read_only'] and not initial['settlement_evidence']['executable']
    with psycopg2.connect(**database) as conn,conn.cursor() as cur:
        cur.execute("UPDATE pams_investment_trade_fact SET fee_currency='EUR' WHERE source_table='ibkr_flex_trade'")
    first=client.get('/api/ibkr/reconciliation').json()['fee_review']
    assert first==client.get('/api/ibkr/reconciliation').json()['fee_review']
    assert first['status']=='review' and first['total']==1 and first['read_only'] and not first['executable']
    item=first['records'][0]
    assert item['official_commission_signed']=='-1.25' and item['fee_currency']=='USD'
    assert item['fact_source_status']=='unknown' and 'fee_fact_source_unverified' in item['issues']
    assert item['unit_review']['status']=='unverified' and item['source_authentication']=='unknown'
    assert item['unit_review']['fact_currency_check']['status']=='unverified'
    assert item['unit_review']['read_model'] is None and not item['unit_review']['additional_deduction_proposed']
    assert first['summary']['official_subtotal_complete'] is False


def test_unified_fee_review_reports_missing_and_cross_currency_without_fabricated_totals():
    from xml.etree import ElementTree as ET
    from domain.brokers.ibkr.ibkr_unified_fee_review import review_unified_fees
    missing=ET.fromstring('<Trade tradeID="M" currency="USD" ibCommission="-1"/>')
    cross=ET.fromstring('<Trade tradeID="C" currency="USD" ibCommission="-2" ibCommissionCurrency="EUR"/>')
    result=review_unified_fees([(missing,{'execution_id':'M','status':'unknown'}),
                                (cross,{'execution_id':'C','status':'unknown'})],section_present=True)
    assert result['total']==2 and result['status']=='review'
    assert 'missing_official_fee_currency' in result['records'][0]['issues']
    assert 'cross_currency_fee' in result['records'][1]['issues']
    assert result['summary']['signed_official_fees_by_currency'] is None
    assert not result['executable'] and all(item['impact'] is None for item in result['records'])
    assert review_unified_fees([],section_present=False)['total'] is None


@pytest.mark.parametrize('table',['pams_investment_trade_fact','source_items','account_snapshot_version'])
def test_missing_required_source_or_fact_is_explicitly_unknown_without_ddl(synced,table):
    from psycopg2 import sql
    client,database,_=synced
    with psycopg2.connect(**database) as conn,conn.cursor() as cur:
        cur.execute(sql.SQL('DROP TABLE {} CASCADE').format(sql.Identifier(table)))
    response=client.get('/api/ibkr/reconciliation')
    assert response.status_code==200,response.text
    result=response.json()
    assert result['status']=='review' and result['read_only'] and result['scope']['source_status']=='unknown'
    assert any(issue['reason']=='missing_table' and issue['source_table']==table
               for check in result['checks'] for issue in check['issues'])
    with psycopg2.connect(**database) as conn,conn.cursor() as cur:
        cur.execute('SELECT to_regclass(%s)',('public.'+table,))
        assert cur.fetchone()[0] is None
