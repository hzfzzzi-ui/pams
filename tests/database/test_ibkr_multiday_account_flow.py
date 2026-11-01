"""IBKR multi-day account flows use each day's original bridge and FX."""
# ruff: noqa: F811
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import xml.etree.ElementTree as ET

import psycopg2
import pytest
from fastapi.testclient import TestClient
from psycopg2.extras import RealDictCursor

from tests.database.test_account_performance_verified_flow import _document, _publish_scope
from tests.database.test_ibkr_holding_review_http import setup, HEADERS
from tests.database.test_ibkr_holding_identity_review import DAY
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401


def _statement(day, before_cash, after_cash, flow):
    from domain.brokers.ibkr.ibkr_daily_close import FLOW_FIELDS
    root=ET.fromstring(_document(day,cash_usd=str(after_cash)))
    statement=next(root.iter('FlexStatement'))
    equity=next(statement.iter('EquitySummaryInBase'))
    current=next(statement.iter('EquitySummaryByReportDateInBase'))
    previous=deepcopy(current)
    previous.set('reportDate',(day-timedelta(days=1)).strftime('%Y%m%d'))
    cash=Decimal(before_cash)+60; total=cash+123
    for key,value in {'cash':cash,'cashLong':cash,'total':total,'totalLong':total}.items():
        previous.set(key,str(value))
    equity.insert(0,previous)
    values={key:'0' for key in FLOW_FIELDS}
    values.update(startingValue=str(total),endingValue=str(Decimal(after_cash)+60+123),
                  depositsWithdrawals=str(flow))
    ET.SubElement(statement,'ChangeInNAV',accountId='U_TEST',currency='USD',
                  fromDate=day.strftime('%Y%m%d'),toDate=day.strftime('%Y%m%d'),**values)
    return root


def _report(first,last,flow1,flow2,variant):
    middle_cash=Decimal(30)+flow1
    final_cash=middle_cash+flow2
    first_root=_statement(first,30,middle_cash,flow1)
    final_root=_statement(last,middle_cash,final_cash,flow2)
    parent=next(final_root.iter('FlexStatements'))
    middle=next(first_root.iter('FlexStatement'))
    if variant!='gap':
        parent.insert(0,middle)
    if variant=='duplicate':
        parent.insert(0,deepcopy(middle))
    return ET.tostring(final_root,encoding='unicode'),final_cash


def _publish_other_account(web,day,holding_id,report):
    response=web.post('/api/ibkr/holding-review/stage',headers=HEADERS,json={
        'source_account_id':'U_OTHER','business_date':day.isoformat(),'xml_text':report.replace('U_TEST','U_OTHER')})
    assert response.status_code==200,response.text
    source=response.json(); base='/api/ibkr/holding-review/'+source['batch_id']
    rev=web.get(base,headers=HEADERS).json()['revision']
    row=web.post(base+'/review',headers=HEADERS,json={'item_id':source['position_item_ids'][0],
        'expected_revision':rev,'note':'review synthetic position'})
    assert row.status_code==200,row.text
    identity=web.post(base+'/identity',headers=HEADERS,json={'item_id':source['position_item_ids'][0],
        'holding_id':holding_id,'expected_revision':row.json()['revision'],'note':'review synthetic identity'})
    assert identity.status_code==200,identity.text
    scope=web.post(base+'/review',headers=HEADERS,json={'item_id':source['scope_item_id'],
        'expected_revision':identity.json()['revision'],'note':'review scope','scope_complete':True})
    assert scope.status_code==200,scope.text
    binding=web.post(base+'/account-binding',headers=HEADERS,json={'source_account_id':'U_OTHER',
        'expected_revision':scope.json()['revision'],'note':'review account mapping'})
    assert binding.status_code==200,binding.text
    reviewed=web.post(base+'/account-scope',headers=HEADERS,json={
        'expected_revision':binding.json()['revision'],'note':'review all synthetic members','confirmed':True})
    assert reviewed.status_code==200,reviewed.text
    published=web.post(base+'/publish',headers=HEADERS,json={'expected_revision':reviewed.json()['revision']})
    assert published.status_code==200,published.text
    return source


@pytest.mark.parametrize('variant,flow1,flow2',[
    ('complete',Decimal(100),Decimal(-100)),
    ('separate',Decimal(100),Decimal(-100)),
    ('independent_match',Decimal(100),Decimal(-100)),
    ('independent_conflict',Decimal(100),Decimal(-100)),
    ('independent_corrupt',Decimal(100),Decimal(-100)),
    ('independent_duplicate',Decimal(100),Decimal(-100)),
    ('different_account',Decimal(100),Decimal(-100)),
    ('reverse_conflict',Decimal(100),Decimal(-100)),
    ('aggregate_zero',Decimal(0),Decimal(0)),
    ('nonzero',Decimal(100),Decimal(0)),
    ('no_flow',Decimal(0),Decimal(0)),
    ('missing_fx',Decimal(100),Decimal(-100)),
    ('gap',Decimal(100),Decimal(-100)),
    ('duplicate',Decimal(100),Decimal(-100)),
])
def test_daily_original_bridges_use_event_day_fx_or_remain_unknown(empty_source_db,monkeypatch,variant,flow1,flow2):
    import main
    from domain.accounts.official_flow_coverage import verified_ibkr_period
    from domain.investments.investment_returns import build_account_performance_payload
    from domain.imports import account_import_store as store
    monkeypatch.setattr(store,'get_conn',lambda:psycopg2.connect(**empty_source_db))
    monkeypatch.setenv('PAMS_IMPORT_PUBLISH_ENABLED','true')
    opening=DAY-timedelta(days=2); middle=opening+timedelta(days=1); closing=DAY
    report,final_cash=_report(middle,closing,flow1,flow2,variant)
    if variant=='separate':
        report=ET.tostring(_statement(closing,Decimal(30)+flow1,final_cash,flow2),encoding='unicode')
    elif variant=='reverse_conflict':
        root=ET.fromstring(report)
        closing_statement=next(node for node in root.iter('FlexStatement')
                               if node.get('toDate')==closing.strftime('%Y%m%d'))
        prior=next(closing_statement.iter('EquitySummaryByReportDateInBase'))
        for key,value in {'cash':'200','cashLong':'200','total':'323','totalLong':'323'}.items():
            prior.set(key,value)
        next(closing_statement.iter('ChangeInNAV')).set('startingValue','323')
        report=ET.tostring(root,encoding='unicode')
    elif variant=='aggregate_zero':
        root=_statement(closing,30,final_cash,0)
        statement=next(root.iter('FlexStatement'))
        statement.set('fromDate',middle.strftime('%Y%m%d'))
        next(root.iter('ChangeInNAV')).set('fromDate',middle.strftime('%Y%m%d'))
        first_nav=next(root.iter('EquitySummaryByReportDateInBase'))
        first_nav.set('reportDate',opening.strftime('%Y%m%d'))
        report=ET.tostring(root,encoding='unicode')
    with psycopg2.connect(**empty_source_db) as conn:
        current_id=setup(conn,monkeypatch,empty_source_db)
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO pams_holdings_snapshot(account_code,name,symbol,currency,asset_liability,
                major_category,minor_category,as_of,quantity,cost,market_value) VALUES
                ('ibkr','Test','TEST','USD','资产','美股','stock',%s,10,80,123) RETURNING id""",(opening,))
            opening_id=cur.fetchone()[0]
            middle_id=None
            scopes=[(opening,Decimal(30)),(closing,final_cash)]
            if variant in {'separate','independent_match','independent_conflict','independent_corrupt','independent_duplicate','reverse_conflict','different_account'}:
                cur.execute("""INSERT INTO pams_holdings_snapshot(account_code,name,symbol,currency,asset_liability,
                    major_category,minor_category,as_of,quantity,cost,market_value) VALUES
                    ('ibkr','Test','TEST','USD','资产','美股','stock',%s,10,80,123) RETURNING id""",(middle,))
                middle_id=cur.fetchone()[0]
                middle_cash=Decimal(30)+flow1+(Decimal(10) if variant in {'independent_conflict','reverse_conflict'} else Decimal(0))
                scopes.append((middle,middle_cash))
            for day,cash in scopes:
                cur.execute("""INSERT INTO pams_holdings_snapshot(account_code,name,symbol,currency,asset_liability,
                    major_category,minor_category,as_of,quantity,cost,market_value) VALUES
                    ('ibkr','Cash USD','CASH_USD','USD','资产','现金','cash',%s,%s,%s,%s),
                    ('ibkr','Cash EUR','CASH_EUR','EUR','资产','现金','cash',%s,30,30,30)""",
                    (day,cash,cash,cash,day))
            for day,usd,eur in ((opening,7,14),(middle,7,14),(closing,8,16)):
                cur.execute("""INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source)
                    VALUES (%s,'CNY','USD',%s,'synthetic'),(%s,'CNY','EUR',%s,'synthetic')""",(day,usd,day,eur))
        conn.commit()
        with TestClient(main.app,base_url='http://pams.local') as web:
            _publish_scope(web,opening,opening_id,_document(opening,cash_usd='30'))
            middle_batch=None
            if middle_id:
                middle_report=ET.tostring(_statement(middle,30,middle_cash,flow1),encoding='unicode')
                middle_batch=(_publish_other_account(web,middle,middle_id,middle_report) if variant=='different_account'
                              else _publish_scope(web,middle,middle_id,middle_report))
                if variant=='independent_duplicate':
                    _publish_scope(web,middle,middle_id,middle_report+'\n<!-- independently published duplicate -->')
            _publish_scope(web,closing,current_id,report)
            if variant=='independent_conflict':
                csv_text=('account_code,period_start,period_end,event_date,side,currency,net_amount,external_id\n'
                          + f'ibkr,{middle.isoformat()},{closing.isoformat()},,none,,,\n')
                staged=web.post('/api/account-import/flow-coverage/stage?account_code=ibkr&filename=scope.csv',
                    headers={'origin':'http://pams.local','content-type':'text/csv'},content=csv_text.encode())
                assert staged.status_code==200,staged.text
                batch=staged.json()
                approved=web.post('/api/account-import/flow-coverage/'+batch['batch_id']+'/publish',
                    headers={'origin':'http://pams.local'},json={'revision':batch['revision'],'confirmed':True,
                    'mappings':{},'note':'synthetic coverage with no locally observed facts'})
                assert approved.status_code==200,approved.text
        if variant=='independent_corrupt':
            with conn.cursor() as cur:
                cur.execute('ALTER TABLE source_files DISABLE TRIGGER source_files_evidence_immutable')
                cur.execute('UPDATE source_files SET content=content || %s::bytea WHERE batch_id=%s',
                            (b'x',middle_batch['batch_id']))
                cur.execute('ALTER TABLE source_files ENABLE TRIGGER source_files_evidence_immutable')
            conn.commit()
        if variant=='missing_fx':
            with conn.cursor() as cur:
                cur.execute("UPDATE pams_fx_rates_daily SET source='' WHERE as_of=%s AND quote_currency='USD'",(middle,))
            conn.commit()
        gross=(Decimal(183)+final_cash)*8
        with psycopg2.connect(**empty_source_db) as read:
            read.set_session(readonly=True,isolation_level='REPEATABLE READ')
            with read.cursor(cursor_factory=RealDictCursor) as cur:
                proof=verified_ibkr_period(cur,account_code='ibkr',start=opening,end=closing,ending_gross_cny=gross)
                if variant=='independent_conflict':
                    from domain.accounts.flow_coverage import verified_period
                    # This report alone passes its own facts check; it must not
                    # mask contradictory official account evidence.
                    generic=verified_period(cur,account_code='ibkr',start=opening,end=closing,ending_gross_cny=gross)
                    assert generic is not None and generic['external_flow_gross_cny']==0
                    from domain.investments import portfolio_valuation_state as portfolio
                    account=[{'account_code':'ibkr','include_in_nav':True,'amount_cny':gross,'as_of':closing}]
                    state=portfolio.build_portfolio_valuation_state_from_parts(account_balances=account,investment_positions=[])
                    assert portfolio._verified_portfolio_daily_pnl(cur,state=state,account_cfg={},
                        account_balances=account,investment_positions=[]) is None
                perf=build_account_performance_payload(cur,account_code='ibkr',owner_ratio=Decimal(1),
                    initial_capital=Decimal(1491),initial_capital_as_of=opening,
                    net_liquidation_gross=Decimal(1491),net_liquidation_as_of=opening,daily_pnl_label_date=closing)
                assert Decimal(perf['net_liquidation_cny'])==gross
                if variant in {'missing_fx','gap','duplicate','aggregate_zero','independent_conflict','independent_corrupt','independent_duplicate','reverse_conflict','different_account'}:
                    assert proof is None and perf['daily_pnl_cny'] is None and perf['total_pnl_cny'] is None
                    if variant=='independent_conflict':
                        assert perf['daily_pnl_unavailable_reason']=='published_source_conflict_or_unverified'
                        assert perf['total_pnl_unavailable_reason']=='published_source_conflict_or_unverified'
                else:
                    flow_cny=flow1*7+flow2*8
                    expected=gross-Decimal(1491)-flow_cny
                    assert proof and proof['external_flow_gross_cny']==flow_cny
                    assert proof==verified_ibkr_period(cur,account_code='ibkr',start=opening,end=closing,ending_gross_cny=gross)
                    assert len(proof['daily_flow_fx_evidence'])==2
                    assert Decimal(perf['daily_pnl_cny'])==expected and Decimal(perf['total_pnl_cny'])==expected
                    if variant in {'complete','separate','independent_match'}:
                        assert flow1+flow2==0 and flow_cny==-100 and expected==313
    if variant in {'complete','missing_fx','independent_match','independent_conflict','independent_corrupt','independent_duplicate','reverse_conflict','different_account'}:
        from endpoints import accounts
        monkeypatch.setattr(main,'get_conn',lambda:psycopg2.connect(**empty_source_db))
        monkeypatch.setattr(main,'_account_owner_ratio',lambda account:Decimal(1))
        monkeypatch.setattr(main,'_account_initial_capital_cny',lambda account:Decimal(1491))
        monkeypatch.setattr(main,'_load_portfolio_accounts_config',lambda:{'accounts':{
            'ibkr':{'initial_capital_as_of':opening.isoformat()}}})
        monkeypatch.setattr(main,'_daily_pnl_view_as_of',lambda:closing)
        monkeypatch.setattr(main,'_reported_returns_cfg',lambda:{})
        monkeypatch.setattr(accounts,'_overview_payload',lambda **kwargs:{'accounts':[{
            'account_code':'ibkr','primary_balance':{'gross_amount':'1491','amount':'1491',
                                                     'as_of':opening.isoformat()}}]})
        with TestClient(main.app,base_url='http://pams.local') as web:
            response=web.get('/api/account-performance/ibkr')
            assert response.status_code==200,response.text
            data=response.json()
            assert Decimal(data['net_liquidation_cny'])==gross
            if variant in {'complete','independent_match'}:
                assert Decimal(data['daily_pnl_cny'])==Decimal(313)
                assert Decimal(data['total_pnl_cny'])==Decimal(313)
                assert len(data['total_pnl_evidence']['daily_flow_fx_evidence'])==2
            else:
                assert data['daily_pnl_cny'] is None and data['total_pnl_cny'] is None
