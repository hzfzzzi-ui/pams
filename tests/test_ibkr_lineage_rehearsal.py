"""Structural success is not production repair approval or accounting validation."""
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys

import psycopg2
from psycopg2 import sql
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'backend/api'))
from collectors.ibkr_flex import parse_flex_report
from services.ibkr_lineage_rehearsal import (plan_rehearsal, safe_plan_rehearsal, fingerprint,
                                           protected_facts_fingerprint, review_rehearsal_evidence)
from services.ibkr_lineage_preflight import preflight_lineage
from services.ibkr_report_state import PublishedReport
from services.investment_trade_fact import build_investment_trade_fact
from test_ibkr_reconciliation import XML
from test_ibkr_sync_transaction import database as database, sync_case as sync_case, rows
from ibkr_rehearsal_support import apply_in_scratch


def make_case(path, count=1, links=None, *, fee_currency='USD'):
    trade = XML[XML.index('<Trade accountId'):XML.index('</Trades>')]
    trade = trade.replace('ibCommission="-1.25"', f'ibCommission="-1.25" ibCommissionCurrency="{fee_currency}"')
    xml = XML[:XML.index('<Trade accountId')] + ''.join(trade.replace('tradeID="T1"', f'tradeID="T{i+1}"')
                                                       for i in range(count)) + XML[XML.index('</Trades>'):]
    sources, facts = [], []
    for index, parsed in enumerate(parse_flex_report(xml).trades):
        sources.append({**parsed, 'id':10+index, 'raw_json':parsed['raw']})
        row = {**parsed,'source_table':'pams_ibkr_trades','source_id':str((links or [9]*count)[index]),
               'source_key':'ibkr:'+parsed['source_trade_key'],'raw_json':parsed['raw'],
               'gross_amount':abs(parsed['gross_amount']),'fee_amount':abs(parsed['commission']),'tax_amount':0}
        facts.append({**build_investment_trade_fact(row),'trade_id':101+index})
    return {'report':PublishedReport(path/'report.xml',date(2026,10,22),1,hashlib.sha256(xml.encode()).hexdigest(),None),
            'xml_text':xml,'quality':{'status':'ok','legacy_unverified':False,'statement_as_of':'2026-10-22',
             'expected_statement_as_of':'2026-10-22'},'trades':sources,'facts':facts}


@pytest.fixture
def case(tmp_path):
    return make_case(tmp_path)


def test_free_target_plan_is_deterministic_and_does_not_approve_repairs(case):
    old = deepcopy(case)
    plan = plan_rehearsal(**case)
    assert plan == plan_rehearsal(**case) and case == old
    assert plan['status']=='ready_for_isolated_rehearsal' and not plan['executable']
    assert not plan['production_write_supported'] and not plan['production_repair_ready']
    assert plan['counts']=={'mismatches':1,'occupied_targets':0,'free_targets':1}
    assert plan['components']==[{'fact_ids':['101'],'size':1,'free_targets':1,'kind':'free_target'}]
    assert plan['projected_after_counts']['link_mismatches']==0
    assert plan['projected_after_reasons']=={'embedded_evidence_mismatch':1}


@pytest.mark.parametrize('links,kind,occupied', [([11,10],'cycle',2),([9,10],'chain',1)])
def test_batch_graph_preserves_occupancy_blockers(tmp_path,links,kind,occupied):
    case=make_case(tmp_path,2,links)
    plan=plan_rehearsal(**case)
    assert plan['status']=='ready_for_isolated_rehearsal' and plan['components'][0]['kind']==kind
    assert plan['counts']['occupied_targets']==occupied
    assert plan['retained_before_reasons']['target_id_in_use']==occupied
    assert plan['projected_after_reasons']['embedded_evidence_mismatch']==2
    case['facts'].reverse(); case['trades'].reverse()
    assert plan == plan_rehearsal(**case)


def test_missing_official_fee_currency_is_not_accepted_from_parser_default(case):
    case['xml_text']=case['xml_text'].replace(' ibCommissionCurrency="USD"','')
    case['report']=PublishedReport(case['report'].path,date(2026,10,22),1,hashlib.sha256(case['xml_text'].encode()).hexdigest(),None)
    assert plan_rehearsal(**case)['blockers']==['fee_evidence_not_matched']


@pytest.mark.parametrize('change',['hash','date','expected_date','stale','legacy','missing_fact','duplicate_fact',
    'value','source_hash','raw_evidence','outside_batch','outside_report'])
def test_invalid_or_incomplete_evidence_blocks(case,change):
    if change=='hash': case['xml_text']+=' '
    elif change=='date': case['quality']['statement_as_of']='2026-10-21'
    elif change=='expected_date': case['quality']['expected_statement_as_of']='2026-10-23'
    elif change=='stale': case['quality']['status']='stale'
    elif change=='legacy': case['quality']['legacy_unverified']=True
    elif change=='missing_fact': case['facts']=[]
    elif change=='duplicate_fact': case['facts'].append(deepcopy(case['facts'][0]))
    elif change=='value': case['facts'][0]['quantity']=999
    elif change=='source_hash': case['facts'][0]['source_hash']='bad'
    elif change=='raw_evidence': case['facts'][0]['raw_json']={}
    elif change=='outside_report': case['facts'][0]['source_key']='ibkr:missing'
    else:
        other=deepcopy(case['facts'][0]); other.update(trade_id=102,source_id='10',source_key='ibkr:outsider')
        case['facts'].append(other)
    result=plan_rehearsal(**case)
    assert result['status']=='blocked' and result['blockers'] and not result['executable']


def test_cross_currency_scalar_unit_warning_is_retained_for_isolated_experiment(tmp_path):
    case=make_case(tmp_path,fee_currency='HKD')
    plan=plan_rehearsal(**case)
    assert plan['status']=='ready_for_isolated_rehearsal'
    assert plan['retained_before_reasons']['fee_currency_unverified']==1
    assert plan['projected_after_reasons']['fee_currency_unverified']==1
    assert not plan['production_repair_ready']


def test_unexpected_exception_is_sanitized(case):
    case['xml_text']='<secret-token'
    case['report']=PublishedReport(case['report'].path,date(2026,10,22),1,hashlib.sha256(case['xml_text'].encode()).hexdigest(),None)
    result=safe_plan_rehearsal(**case)
    assert result['status']=='unavailable' and 'secret-token' not in str(result)


@pytest.fixture
def scratch(sync_case,database,tmp_path):
    case=make_case(tmp_path,2,[11,10])
    templates={table:rows(database,table)[0] for table in ('pams_ibkr_trades','pams_investment_trade_fact')}
    conn=database()
    with conn, conn.cursor() as cur:
        cur.execute('TRUNCATE pams_investment_trade_fact,pams_ibkr_trades')
        for table,group in [('pams_ibkr_trades','trades'),('pams_investment_trade_fact','facts')]:
            for row in case[group]:
                content={**templates[table],**{k:v for k,v in row.items() if k in templates[table]}}
                cur.execute(sql.SQL('INSERT INTO {} SELECT * FROM json_populate_record(NULL::{},%s::json)').format(
                    sql.Identifier(table),sql.Identifier(table)),(json.dumps(content,default=str),))
        cur.execute(sql.SQL("COMMENT ON DATABASE {} IS 'pams-isolated-lineage-rehearsal'").format(sql.Identifier(conn.info.dbname)))
    conn.close()
    case.update(trades=rows(database,'pams_ibkr_trades'),facts=rows(database,'pams_investment_trade_fact'))
    return case


def test_real_postgres_cycle_atomic_change_idempotence_and_historical_warning(scratch,database):
    case=scratch
    plan=plan_rehearsal(**case)
    conn=database()
    try:
        assert apply_in_scratch(conn,plan,xml_text=case['xml_text'])=={'updated':2,'already_applied':False}
        assert apply_in_scratch(conn,plan,xml_text=case['xml_text'])=={'updated':0,'already_applied':True}
    finally: conn.close()
    after=rows(database,'pams_investment_trade_fact')
    assert protected_facts_fingerprint(after)==protected_facts_fingerprint(case['facts'])
    result=preflight_lineage(**{**case,'facts':after},limit=500)
    assert result['counts']['link_mismatches']==0 and result['reason_counts']=={'embedded_evidence_mismatch':2}


@pytest.mark.parametrize('phase',['after_stage','after_first_final','before_commit'])
def test_real_postgres_interruption_rolls_back_whole_batch(scratch,database,phase):
    plan=plan_rehearsal(**scratch)
    conn=database()
    try:
        with pytest.raises(RuntimeError): apply_in_scratch(conn,plan,xml_text=scratch['xml_text'],interrupt_at=phase)
    finally: conn.close()
    assert fingerprint(rows(database,'pams_investment_trade_fact'))==fingerprint(scratch['facts'])


@pytest.mark.parametrize('change',['source','financial','link','plan','marker'])
def test_real_postgres_changed_input_or_forged_plan_refused(scratch,database,change):
    plan=plan_rehearsal(**scratch)
    conn=database()
    try:
        with conn,conn.cursor() as cur:
            if change=='source': cur.execute("UPDATE pams_ibkr_trades SET price=999 WHERE id=10")
            elif change=='financial': cur.execute("UPDATE pams_investment_trade_fact SET quantity=999 WHERE trade_id=101")
            elif change=='link': cur.execute("UPDATE pams_investment_trade_fact SET source_id='changed' WHERE trade_id=101")
            elif change=='marker': cur.execute(sql.SQL('COMMENT ON DATABASE {} IS NULL').format(sql.Identifier(conn.info.dbname)))
            else: plan['mappings'][0]['after']='999'
        before=rows(database,'pams_investment_trade_fact')
        with pytest.raises(ValueError): apply_in_scratch(conn,plan,xml_text=scratch['xml_text'])
        assert fingerprint(rows(database,'pams_investment_trade_fact'))==fingerprint(before)
    finally: conn.close()


def test_fingerprint_preserves_exact_numbers_types_and_postgres_timestamp_rendering():
    assert fingerprint([{'n':Decimal('2.0000000000')}])==fingerprint([{'n':2.0}])
    assert fingerprint([{'t':datetime(2026,10,22,1,2,3,120000,tzinfo=timezone.utc)}]) == fingerprint([{'t':'2026-10-22T01:02:03.12+00:00'}])
    values=[0,False,None,'0',{'number':'0'},['number','0'],Decimal('0.123456789012345678901')]
    assert len({fingerprint([{'v':v}]) for v in values})==len(values)
    assert fingerprint([{'n':Decimal('0.123456789012345678901')}]) != fingerprint([{'n':0.12345678901234568}])


@pytest.mark.parametrize('guard',['dsn','name','report'])
def test_scratch_guard_refuses_wrong_server_database_or_report_before_write(scratch,database,monkeypatch,guard):
    plan=plan_rehearsal(**scratch)
    conn=database()
    try:
        if guard=='dsn': monkeypatch.delenv('PAMS_TEST_POSTGRES_DSN')
        elif guard=='name':
            class OtherDatabase:
                class info:
                    dsn_parameters={**conn.info.dsn_parameters,'dbname':'pams'}
            conn.close(); conn=OtherDatabase()
        with pytest.raises(ValueError):
            apply_in_scratch(conn,plan,xml_text=scratch['xml_text']+(' ' if guard=='report' else ''))
        assert fingerprint(rows(database,'pams_investment_trade_fact'))==fingerprint(scratch['facts'])
    finally:
        if hasattr(conn,'close'): conn.close()


@pytest.mark.parametrize('late',['mutated_field','commit_error'])
def test_real_postgres_trigger_side_effect_or_deferred_commit_failure_rolls_back(scratch,database,late):
    plan=plan_rehearsal(**scratch)
    conn=database()
    try:
        with conn,conn.cursor() as cur:
            if late=='mutated_field':
                cur.execute("CREATE FUNCTION rehearsal_fault() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN NEW.price=999; RETURN NEW; END $$")
                cur.execute('CREATE TRIGGER rehearsal_fault BEFORE UPDATE ON pams_investment_trade_fact FOR EACH ROW EXECUTE FUNCTION rehearsal_fault()')
            else:
                cur.execute("CREATE FUNCTION rehearsal_fault() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'Injected deferred commit failure'; END $$")
                cur.execute('CREATE CONSTRAINT TRIGGER rehearsal_fault AFTER UPDATE ON pams_investment_trade_fact DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION rehearsal_fault()')
        with pytest.raises((ValueError,psycopg2.Error)):
            apply_in_scratch(conn,plan,xml_text=scratch['xml_text'])
        assert fingerprint(rows(database,'pams_investment_trade_fact'))==fingerprint(scratch['facts'])
    finally: conn.close()


def test_rehearsal_has_no_production_entrypoint_and_executor_is_not_packaged():
    root=Path(__file__).resolve().parents[1]
    for folder in ('backend/api/endpoints','backend/scheduler'):
        assert all('ibkr_lineage_rehearsal' not in p.read_text(encoding='utf-8') for p in (root/folder).glob('*.py'))
    assert 'tests/' not in (root/'backend/api/Dockerfile').read_text()
    source=(root/'backend/api/services/ibkr_lineage_rehearsal.py').read_text()
    assert 'psycopg2' not in source and 'get_conn' not in source


@pytest.mark.parametrize('table',['pams_ibkr_trades','pams_investment_trade_fact'])
def test_real_postgres_concurrent_writer_lock_blocks_and_leaves_no_partial_batch(scratch,database,table):
    plan=plan_rehearsal(**scratch)
    blocker=database(); conn=database()
    try:
        with blocker.cursor() as cur:
            cur.execute(sql.SQL('LOCK TABLE {} IN ROW EXCLUSIVE MODE').format(sql.Identifier(table)))
        with pytest.raises(psycopg2.errors.LockNotAvailable):
            apply_in_scratch(conn,plan,xml_text=scratch['xml_text'])
        assert fingerprint(rows(database,'pams_investment_trade_fact'))==fingerprint(scratch['facts'])
    finally:
        blocker.rollback();blocker.close();conn.close()


def test_resigned_plan_cannot_redirect_stable_identity(scratch,database):
    plan=plan_rehearsal(**scratch)
    plan['mappings'][0]['after']='999'
    plan['plan_sha256']=fingerprint([{k:v for k,v in plan.items() if k!='plan_sha256'}])
    conn=database()
    try:
        with pytest.raises(ValueError,match='stable source identity'):
            apply_in_scratch(conn,plan,xml_text=scratch['xml_text'])
        assert fingerprint(rows(database,'pams_investment_trade_fact'))==fingerprint(scratch['facts'])
    finally: conn.close()


def project(case, plan):
    facts = deepcopy(case['facts'])
    targets = {row['fact_id']: row['after'] for row in plan['mappings']}
    for fact in facts:
        if str(fact['trade_id']) in targets:
            fact['source_id'] = targets[str(fact['trade_id'])]
    return facts


@pytest.mark.parametrize('links', [[11,10], [9,10], [8,9]])
def test_history_explanation_requires_exact_full_projection_and_keeps_legacy_blockers(tmp_path, links):
    case = make_case(tmp_path, 2, links)
    before = deepcopy(case)
    plan = plan_rehearsal(**case)
    projected = project(case, plan)
    result = review_rehearsal_evidence(baseline=case, plan=plan, projected_facts=projected)
    assert result == plan['audit_evidence'] and result['status'] == 'review'
    assert result['counts'] == {'historical_ids_explained': 2, 'fee_units_verified': 0, 'unresolved_evidence': 0}
    assert plan['projected_after_reasons'] == {'embedded_evidence_mismatch': 2}
    assert all(row['status'] == 'historical_reference_preserved' for row in result['history_records'])
    assert not result['executable'] and not result['production_repair_ready']
    assert 'user_approval_required' in result['remaining_requirements']
    assert case == before


def test_projected_cross_currency_unit_is_explained_but_cash_and_model_remain_unverified(tmp_path):
    case = make_case(tmp_path, fee_currency='HKD')
    plan = plan_rehearsal(**case)
    result = review_rehearsal_evidence(baseline=case, plan=plan, projected_facts=project(case, plan))
    assert result['counts'] == {'historical_ids_explained': 1, 'fee_units_verified': 1, 'unresolved_evidence': 0}
    assert result['fee_records'][0]['unit_review']['read_model']['currency'] == 'HKD'
    assert plan['projected_after_reasons'] == {'embedded_evidence_mismatch': 1, 'fee_currency_unverified': 1}
    assert {'fee_currency_not_modeled', 'individual_cash_settlement_unverified'} <= set(result['remaining_requirements'])
    assert result['legacy_blockers_preserved'] and not result['production_write_supported']


@pytest.mark.parametrize('change', ['partial', 'extra', 'historical_id', 'hash', 'quantity', 'fee', 'net_cash',
                                  'timestamp', 'wrong_target', 'omitted_fact'])
def test_changed_projection_cannot_explain_away_real_errors(case, change):
    plan = plan_rehearsal(**case)
    projected = project(case, plan)
    fact = projected[0]
    if change == 'partial': fact['source_id'] = case['facts'][0]['source_id']
    elif change == 'extra': projected.append(deepcopy(fact))
    elif change == 'historical_id': fact['raw_json']['source_row']['source_id'] = 'forged'
    elif change == 'hash': fact['source_hash'] = 'bad'
    elif change == 'quantity': fact['quantity'] += 1
    elif change == 'fee': fact['fee_amount'] += 1
    elif change == 'net_cash': fact['net_cash'] += 1
    elif change == 'timestamp': fact['updated_at'] = '2030-01-01'
    elif change == 'wrong_target': fact['source_id'] = '999'
    else: projected.clear()
    result = review_rehearsal_evidence(baseline=case, plan=plan, projected_facts=projected)
    assert result['status'] == 'blocked' and result['history_records'] == []
    assert not result['executable'] and not result['production_repair_ready']


@pytest.mark.parametrize('change', ['mapping', 'history', 'quality', 'source', 'report'])
def test_changed_baseline_or_rehashed_plan_is_not_proof(case, change):
    plan = plan_rehearsal(**case)
    projected = project(case, plan)
    if change == 'mapping':
        plan['mappings'][0]['after'] = '999'
        plan['plan_sha256'] = fingerprint([{k:v for k,v in plan.items() if k != 'plan_sha256'}])
    elif change == 'history': case['facts'][0]['raw_json']['source_row']['source_id'] = 'forged'
    elif change == 'quality': case['quality']['expected_statement_as_of'] = '2026-10-23'
    elif change == 'source': case['trades'][0]['id'] = 999
    else: case['xml_text'] += ' '
    result = review_rehearsal_evidence(baseline=case, plan=plan, projected_facts=projected)
    assert result['status'] == 'blocked' and not result['executable']


def test_omitted_official_execution_cannot_pass_complete_scope_check(tmp_path):
    case = make_case(tmp_path, 2, [8,9])
    case['facts'].pop()
    case['trades'].pop()
    plan = plan_rehearsal(**case)
    assert plan['audit_evidence']['status'] == 'blocked'
    assert not plan['audit_evidence']['complete_scope']
    assert plan['audit_evidence']['history_records'] == []


def test_new_review_does_not_suppress_non_id_history_mismatch(case, monkeypatch):
    from services import ibkr_lineage_rehearsal as audit
    original = audit.preflight_lineage
    def with_error(**kwargs):
        result = original(**kwargs)
        if kwargs['facts'][0]['source_id'] == '10':
            result['records'][0]['blockers'].append({'reason':'embedded_evidence_mismatch', 'field':'execution_id'})
        return result
    monkeypatch.setattr(audit, 'preflight_lineage', with_error)
    plan = plan_rehearsal(**case)
    result = plan['audit_evidence']
    assert result['status'] == 'blocked' and result['counts']['unresolved_evidence'] == 1
    assert result['unresolved_evidence'][0]['field'] == 'execution_id'


def test_real_postgres_history_review_preserves_rows_after_isolated_mapping(scratch, database):
    baseline = deepcopy(scratch)
    plan = plan_rehearsal(**baseline)
    conn = database()
    try:
        assert apply_in_scratch(conn, plan, xml_text=baseline['xml_text'])['updated'] == 2
    finally:
        conn.close()
    after = rows(database, 'pams_investment_trade_fact')
    old = fingerprint(after)
    first = review_rehearsal_evidence(baseline=baseline, plan=plan, projected_facts=after)
    second = review_rehearsal_evidence(baseline=baseline, plan=plan, projected_facts=after)
    assert first == second and first['counts']['historical_ids_explained'] == 2
    assert fingerprint(rows(database, 'pams_investment_trade_fact')) == old
    assert protected_facts_fingerprint(after) == protected_facts_fingerprint(baseline['facts'])
    assert not first['executable'] and first['status'] == 'review'
