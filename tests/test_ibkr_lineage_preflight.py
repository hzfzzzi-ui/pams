"""Source-link candidates are not repair instructions or accounting facts."""
from copy import deepcopy
import hashlib
from pathlib import Path
import sys

import psycopg2.extras
from psycopg2.extensions import connection, ISOLATION_LEVEL_REPEATABLE_READ
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'backend/api'))
from collectors.ibkr_flex import parse_flex_report
from services.ibkr_report_state import PublishedReport
from services.ibkr_lineage_preflight import preflight_lineage, read_lineage_preflight
from services.investment_trade_fact import build_investment_trade_fact
from test_ibkr_reconciliation import XML
from test_ibkr_sync_transaction import database as database, sync_case as sync_case, business_state, rows


@pytest.fixture
def case(tmp_path):
    trade = parse_flex_report(XML).trades[0]
    row = {**trade, 'source_table':'pams_ibkr_trades', 'source_id':'10',
           'source_key':'ibkr:'+trade['source_trade_key'], 'raw_json':trade['raw'],
           'gross_amount':abs(trade['gross_amount']), 'fee_amount':abs(trade['commission']), 'tax_amount':0}
    fact = {**build_investment_trade_fact(row), 'trade_id':101}
    return {'report':PublishedReport(tmp_path/'report.xml', parse_flex_report(XML).statement_as_of,
              1, hashlib.sha256(XML.encode()).hexdigest(), None),
            'xml_text':XML,'quality':{'status':'ok','legacy_unverified':False},
            'trades':[{**trade,'id':11}], 'facts':[fact]}


def reasons(result):
    return {v['reason'] for r in result['records'] for v in r['blockers']}


def test_unique_candidate_is_metadata_only_never_executable_and_deterministic(case):
    original = deepcopy(case)
    result = preflight_lineage(**case)
    assert result == preflight_lineage(**case) and case == original
    assert result['counts'] == {'facts':1,'link_mismatches':1,'candidate':1,'blocked':0,'unchanged':0}
    row = result['records'][0]
    assert row['proposal'] == {'field':'source_id','before':'10','after':'11',
                               'source_hash_unchanged':True,'requires_user_approval':True}
    assert not result['executable'] and not result['repair_implemented'] and not row['executable']
    assert row['fact_id']=='101' and row['old_reference']['state']=='absent'
    assert all(c['equal'] and isinstance(c['expected'],str) for c in row['invariants'])


def test_existing_correct_link_has_no_proposal(case):
    case['trades'][0]['id']=10
    result = preflight_lineage(**case)
    assert result['records'][0]['status']=='unchanged' and result['records'][0]['proposal'] is None
    assert result['counts']['link_mismatches']==0


def test_target_unique_constraint_occupant_blocks_even_when_other_fact_could_move(case):
    other = deepcopy(case['facts'][0])
    other.update(trade_id=102,source_id='11',source_key='ibkr:other')
    case['facts'].append(other)
    result = preflight_lineage(**case)
    row = next(r for r in result['records'] if r['fact_id']=='101')
    assert row['status']=='blocked' and any(b.get('conflicting_fact_ids')==['102'] for b in row['blockers'])
    assert result['counts']['candidate']==0


@pytest.mark.parametrize('field,value', [('quantity',3),('price',171),('gross_amount',341),
    ('fee_amount',2),('net_cash',341.25),('tax_amount',1)])
def test_invariant_changes_block_and_are_not_repaired(case,field,value):
    case['facts'][0][field]=value
    result=preflight_lineage(**case)
    assert 'value_mismatch' in reasons(result) and result['counts']['candidate']==0


@pytest.mark.parametrize('value,reason', [(None,'missing_value'),('NaN','invalid_value'),('Infinity','invalid_value')])
def test_missing_or_nonfinite_values_never_default_to_zero(case,value,reason):
    case['facts'][0]['quantity']=value
    assert reason in reasons(preflight_lineage(**case))


@pytest.mark.parametrize('field,value',[('account_code','other'),('currency','HKD'),('symbol','OTHER'),
                                      ('trade_date','2026-10-21'),('side','sell')])
def test_scope_mismatch_blocks(case,field,value):
    case['facts'][0][field]=value
    result=preflight_lineage(**case)
    assert 'scope_mismatch' in reasons(result) and result['counts']['candidate']==0


@pytest.mark.parametrize('table',['trades','facts'])
def test_duplicate_stable_keys_block_instead_of_deduplicating(case,table):
    case[table].append(deepcopy(case[table][0]))
    assert 'duplicate_identity' in reasons(preflight_lineage(**case))


def test_duplicate_official_execution_blocks_even_with_changed_stable_key(case):
    start=case['xml_text'].index('<Trade accountId')
    end=case['xml_text'].index('/>',start)+2
    first=case['xml_text'][start:end]
    case['xml_text']=case['xml_text'].replace(first,first+first.replace('tradeMoney="340"','tradeMoney="350"').replace('netCash="-341.25"','netCash="-351.25"'))
    assert 'duplicate_identity' in reasons(preflight_lineage(**case))


@pytest.mark.parametrize('change', ['missing_source','missing_evidence','old_id','execution','source_hash','outside_report'])
def test_incomplete_lineage_blocks(case,change):
    expected={'missing_source':'missing_source','missing_evidence':'embedded_evidence_mismatch',
              'old_id':'embedded_evidence_mismatch','execution':'embedded_evidence_mismatch',
              'source_hash':'source_hash_mismatch','outside_report':'outside_report'}[change]
    if change=='missing_source': case['trades']=[]
    elif change=='missing_evidence': case['facts'][0]['raw_json']={}
    elif change=='old_id': case['facts'][0]['raw_json']['source_row']['source_id']='99'
    elif change=='execution': case['facts'][0]['raw_json']['source_row']['raw_json']['tradeID']='OTHER'
    elif change=='source_hash': case['facts'][0]['source_hash']='bad'
    else: case['facts'][0]['source_key']='ibkr:uncovered'
    result=preflight_lineage(**case)
    assert expected in reasons(result) and result['counts']['candidate']==0


@pytest.mark.parametrize('quality',[{'status':'stale'},{'status':'review'},{'status':'ok','legacy_unverified':True}])
def test_stale_or_unverified_report_still_traces_but_blocks_candidates(case,quality):
    case['quality']=quality
    result=preflight_lineage(**case)
    assert result['counts']['link_mismatches']==1 and 'report_unverified' in reasons(result)
    assert result['counts']['candidate']==0


def test_cross_currency_fee_keeps_raw_values_but_is_not_blessed(case):
    case['xml_text']=case['xml_text'].replace('ibCommission="-1.25"','ibCommission="-1.25" ibCommissionCurrency="HKD"')
    result=preflight_lineage(**case)
    assert 'fee_currency_unverified' in reasons(result) and result['counts']['candidate']==0
    assert all(r['field'] not in {'commission','fee_amount'} or r['stage']=='embedded' for r in result['records'][0]['invariants'])


def test_other_execution_at_old_id_is_evidence_not_a_financial_match(case):
    unrelated={**case['trades'][0],'id':10,'source_trade_key':'other','trade_id':'OTHER','net_cash':99999}
    case['trades'].append(unrelated)
    result=preflight_lineage(**case)
    assert result['records'][0]['old_reference']['state']=='other_or_ambiguous_execution'
    assert result['counts']['candidate']==1 and all(c['equal'] for c in result['records'][0]['invariants'])


def test_swapped_source_ids_are_a_blocked_batch_not_two_executable_candidates(case):
    source=case['trades'][0]
    second_xml=XML.replace('tradeID="T1"','tradeID="T2"')
    other_source=parse_flex_report(second_xml).trades[0]
    case['xml_text']=XML.replace('</Trades>',second_xml[second_xml.index('<Trade accountId'):second_xml.index('</Trades>')]+'</Trades>')
    row={**other_source,'source_table':'pams_ibkr_trades','source_id':'11',
         'source_key':'ibkr:'+other_source['source_trade_key'],'raw_json':other_source['raw'],
         'gross_amount':340,'fee_amount':1.25,'tax_amount':0}
    case['facts'].append({**build_investment_trade_fact(row),'trade_id':102})
    case['trades']=[source,{**other_source,'id':10}]
    result=preflight_lineage(**case)
    assert result['counts']['link_mismatches']==2 and result['counts']['candidate']==0
    assert result['reason_counts']['target_id_in_use']==2


def test_empty_fact_set_cannot_hide_bad_source_quality(case):
    case.update(facts=[],quality={'status':'stale'})
    result=preflight_lineage(**case)
    assert result['status']=='blocked' and result['global_blockers'][0]['reason']=='report_unverified'


def test_missing_raw_amount_blocks_despite_parser_defaults(case):
    case['xml_text']=case['xml_text'].replace('tradeMoney="340"','')
    assert 'missing_value' in reasons(preflight_lineage(**case))


def test_existing_writer_repeated_upsert_retains_physical_id(sync_case,database):
    from collectors.ibkr_flex import _write_trades
    trade=parse_flex_report(XML.replace('tradeID="T1"','tradeID="T0"')).trades[0]
    old=next(r for r in rows(database,'pams_ibkr_trades') if r['source_trade_key']==trade['source_trade_key'])
    conn=database()
    try:
        for _ in range(2):
            with conn: _write_trades(conn,[trade],account_code='ibkr')
        new=next(r for r in rows(database,'pams_ibkr_trades') if r['source_trade_key']==trade['source_trade_key'])
        assert old['id']==new['id']
    finally: conn.close()


def test_pagination_is_complete_deterministic_and_counts_do_not_truncate(case):
    fact=case['facts'][0]
    case['facts']=[{**fact,'trade_id':i,'source_key':f'ibkr:outside-{i:03}'} for i in range(75)]
    first=preflight_lineage(**case,limit=40)
    last=preflight_lineage(**case,offset=40,limit=40)
    assert first['total']==last['total']==75 and first['reason_counts']['outside_report']==75
    assert len(first['records'])==40 and len(last['records'])==35 and first['has_more'] and not last['has_more']
    assert {r['fact_id'] for r in first['records']}.isdisjoint(r['fact_id'] for r in last['records'])


@pytest.mark.parametrize('args',[{'offset':-1},{'limit':0},{'limit':501}])
def test_invalid_page_rejected_before_opening_database(args):
    def forbidden(): pytest.fail('No connection for invalid parameters')
    with pytest.raises(ValueError): read_lineage_preflight(connection_factory=forbidden,**args)


def test_real_postgres_readonly_repeatable_read_route_and_no_business_changes(sync_case,database,monkeypatch):
    from services import ibkr_lineage_preflight as service
    statements=[]
    class GuardCursor(psycopg2.extras.RealDictCursor):
        def execute(self,query,args=None):
            assert self.connection.readonly and self.connection.isolation_level==ISOLATION_LEVEL_REPEATABLE_READ
            assert str(query).lstrip().upper().startswith(('SELECT','SET LOCAL'))
            statements.append(str(query))
            return super().execute(query,args)
    class GuardConnection(connection):
        def cursor(self,*args,**kwargs):
            kwargs['cursor_factory']=GuardCursor
            return super().cursor(*args,**kwargs)
    monkeypatch.setattr(service,'get_conn',lambda:database(connection_factory=GuardConnection))
    client,_,_=sync_case
    before={**business_state(database),'runs':rows(database,'pams_data_sync_runs'),'jobs':rows(database,'pams_data_job_run')}
    for _ in range(2):
        result=client.get('/api/ibkr/lineage-preflight?limit=1').json()
        assert result['read_only'] and result['counts']['unchanged']==1 and not result['executable']
    for query in ('limit=0','offset=-1','limit=501'):
        assert client.get('/api/ibkr/lineage-preflight?'+query).status_code==422
    assert statements and before=={**business_state(database),'runs':rows(database,'pams_data_sync_runs'),'jobs':rows(database,'pams_data_job_run')}


def test_server_rejects_accidental_write_and_errors_are_sanitized(sync_case,database,monkeypatch):
    from services import ibkr_lineage_preflight as service
    def write(conn,raw_dir):
        with conn.cursor() as cur: cur.execute('UPDATE pams_holdings_snapshot SET quantity=0')
    monkeypatch.setattr(service,'select_report',write)
    before=business_state(database)
    result=read_lineage_preflight(connection_factory=database)
    assert result['error_type']=='ReadOnlySqlTransaction' and result['status']=='unavailable'
    assert before==business_state(database)
    def secret(): raise RuntimeError('postgresql://token=secret@host')
    result=read_lineage_preflight(connection_factory=secret)
    assert result['error_type']=='RuntimeError' and 'secret' not in str(result)


def test_missing_table_never_created_and_hash_changes_are_unavailable(sync_case,database,monkeypatch):
    from services import ibkr_lineage_preflight as service
    select=service.select_report
    def tampered(conn,raw_dir):
        report,latest=select(conn,raw_dir)
        report.path.write_text('changed',encoding='utf-8')
        return report,latest
    monkeypatch.setattr(service,'select_report',tampered)
    assert read_lineage_preflight(connection_factory=database)['error_type']=='ValueError'
    monkeypatch.setattr(service,'select_report',select)
    # The next test uses a missing table independently of whether this fixture's file was changed.
    conn=database()
    with conn,conn.cursor() as cur: cur.execute('DROP TABLE pams_ibkr_trades')
    conn.close()
    monkeypatch.setattr(service,'select_report',lambda *args:(PublishedReport(Path('unused'),None,1,'x',None),None))
    result=read_lineage_preflight(connection_factory=database)
    assert result['reason']=='missing_table'
