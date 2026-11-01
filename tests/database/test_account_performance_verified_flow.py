"""Account returns recover only from verified, published period evidence."""
# ruff: noqa: F811
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import xml.etree.ElementTree as ET

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor
from fastapi.testclient import TestClient

from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from tests.database.test_ibkr_account_complete_scope import scope_report
from tests.database.test_ibkr_holding_review_http import setup, HEADERS
from tests.database.test_ibkr_holding_identity_review import DAY


def _document(day, *, cash_usd, variant='missing'):
    from domain.brokers.ibkr.ibkr_daily_close import FLOW_FIELDS
    root=ET.fromstring(scope_report().replace(b'20261022',day.strftime('%Y%m%d').encode()))
    statement=next(root.iter('FlexStatement'))
    row=next(statement.iter('EquitySummaryByReportDateInBase'))
    cash=Decimal(cash_usd)+Decimal(60)
    total=cash+Decimal(123)
    for key,value in {'cash':cash,'cashLong':cash,'total':total,'totalLong':total}.items():
        row.set(key,str(value))
    next(node for node in statement.iter('CashReportCurrency') if node.get('currency')=='USD').set('endingCash',cash_usd)
    if variant != 'missing':
        equity=next(statement.iter('EquitySummaryInBase'))
        previous=deepcopy(row)
        previous.set('reportDate',(day-timedelta(days=1)).strftime('%Y%m%d'))
        for key,value in {'cash':'90','cashLong':'90','total':'213','totalLong':'213'}.items():
            previous.set(key,value)
        equity.insert(0,previous)
        fields={key:'0' for key in FLOW_FIELDS}
        fields.update(startingValue='213',endingValue='233',depositsWithdrawals='20')
        bridge=ET.SubElement(statement,'ChangeInNAV',accountId='U_TEST',currency='USD',
            fromDate=day.strftime('%Y%m%d'),toDate=day.strftime('%Y%m%d'),**fields)
        if variant == 'duplicate':
            statement.append(deepcopy(bridge))
        elif variant == 'wrong_currency':
            bridge.set('currency','EUR')
        elif variant == 'wrong_account':
            bridge.set('accountId','OTHER')
        elif variant == 'wrong_period':
            bridge.set('fromDate',(day-timedelta(days=1)).strftime('%Y%m%d'))
    return ET.tostring(root,encoding='unicode')


def _publish_scope(web,day,holding_id,report):
    response=web.post('/api/ibkr/holding-review/stage',headers=HEADERS,json={
        'source_account_id':'U_TEST','business_date':day.isoformat(),'xml_text':report})
    assert response.status_code==200,response.text
    source=response.json(); base='/api/ibkr/holding-review/'+source['batch_id']
    state=web.get(base,headers=HEADERS).json(); rev=state['revision']
    row=web.post(base+'/review',headers=HEADERS,json={'item_id':source['position_item_ids'][0],
        'expected_revision':rev,'note':'review source position'})
    assert row.status_code==200,row.text
    identity=web.post(base+'/identity',headers=HEADERS,json={'item_id':source['position_item_ids'][0],
        'holding_id':holding_id,'expected_revision':row.json()['revision'],'note':'review identity'})
    assert identity.status_code==200,identity.text
    scope=web.post(base+'/review',headers=HEADERS,json={'item_id':source['scope_item_id'],
        'expected_revision':identity.json()['revision'],'note':'review scope','scope_complete':True})
    assert scope.status_code==200,scope.text
    binding=web.post(base+'/account-binding',headers=HEADERS,json={'source_account_id':'U_TEST',
        'expected_revision':scope.json()['revision'],'note':'review account mapping'})
    assert binding.status_code==200,binding.text
    reviewed=web.post(base+'/account-scope',headers=HEADERS,json={
        'expected_revision':binding.json()['revision'],'note':'review all cash and securities','confirmed':True})
    assert reviewed.status_code==200,reviewed.text
    published=web.post(base+'/publish',headers=HEADERS,json={'expected_revision':reviewed.json()['revision']})
    assert published.status_code==200,published.text
    return source


@pytest.mark.parametrize('variant',['valid','missing','duplicate','wrong_currency','wrong_account','wrong_period'])
def test_published_account_and_report_prove_period_flow_and_returns(empty_source_db,monkeypatch,variant):
    import main
    from domain.investments.investment_returns import build_account_performance_payload
    previous=DAY-timedelta(days=1)
    with psycopg2.connect(**empty_source_db) as conn:
        current_id=setup(conn,monkeypatch,empty_source_db)
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO pams_holdings_snapshot(account_code,name,symbol,currency,asset_liability,
                major_category,minor_category,as_of,quantity,cost,market_value) VALUES
                ('ibkr','Test','TEST','USD',%s,%s,'stock',%s,10,80,123) RETURNING id""",
                ('\u8d44\u4ea7','\u7f8e\u80a1',previous))
            previous_id=cur.fetchone()[0]
            for day,usd in ((previous,30),(DAY,50)):
                cur.execute("""INSERT INTO pams_holdings_snapshot(account_code,name,symbol,currency,asset_liability,
                    major_category,minor_category,as_of,quantity,cost,market_value) VALUES
                    ('ibkr','Cash USD','CASH_USD','USD',%s,%s,'cash',%s,%s,%s,%s),
                    ('ibkr','Cash EUR','CASH_EUR','EUR',%s,%s,'cash',%s,30,30,30)""",
                    ('\u8d44\u4ea7','\u73b0\u91d1',day,usd,usd,usd,'\u8d44\u4ea7','\u73b0\u91d1',day))
                cur.execute("""INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source)
                    VALUES (%s,'CNY','USD',7,'synthetic'),(%s,'CNY','EUR',14,'synthetic')""",(day,day))
        conn.commit()
        with TestClient(main.app,base_url='http://pams.local') as web:
            _publish_scope(web,previous,previous_id,_document(previous,cash_usd='30'))
            current=_publish_scope(web,DAY,current_id,_document(DAY,cash_usd='50',variant=variant))
        def performance(*,initial_day=previous):
            with psycopg2.connect(**empty_source_db) as read:
                read.set_session(readonly=True,isolation_level='REPEATABLE READ')
                with read.cursor(cursor_factory=RealDictCursor) as cur:
                    return build_account_performance_payload(cur,account_code='ibkr',owner_ratio=Decimal(1),
                        initial_capital=Decimal(1491),initial_capital_as_of=initial_day,
                        net_liquidation_gross=Decimal(1491),net_liquidation_as_of=previous,
                        daily_pnl_label_date=DAY)
        result=performance()
        assert Decimal(result['net_liquidation_cny'])==Decimal(1631)
        assert Decimal(result['cash_or_uninvested_cny'])==Decimal(770)
        if variant != 'valid':
            assert result['external_flow_coverage']=='unknown'
            assert result['daily_pnl_cny'] is None and result['total_pnl_cny'] is None
            return
        assert Decimal(result['observed_external_net_flow_cny'])==Decimal(140)
        assert Decimal(result['daily_pnl_cny'])==0 and Decimal(result['total_pnl_cny'])==0
        assert result['external_flow_coverage']=='verified'
        assert result['daily_pnl_evidence']['start']==previous.isoformat()
        assert result['daily_pnl_evidence']['source_sha256']
        assert result['total_pnl_basis']=='verified_anchor_period_not_inception'
        from endpoints import accounts
        monkeypatch.setattr(main,'get_conn',lambda:psycopg2.connect(**empty_source_db))
        monkeypatch.setattr(main,'_account_owner_ratio',lambda account:Decimal(1))
        monkeypatch.setattr(main,'_account_initial_capital_cny',lambda account:Decimal(1491))
        monkeypatch.setattr(main,'_load_portfolio_accounts_config',lambda:{'accounts':{
            'ibkr':{'initial_capital_as_of':previous.isoformat()}}})
        monkeypatch.setattr(main,'_daily_pnl_view_as_of',lambda:DAY)
        monkeypatch.setattr(main,'_reported_returns_cfg',lambda:{})
        monkeypatch.setattr(accounts,'_overview_payload',lambda **kwargs:{'accounts':[{
            'account_code':'ibkr','primary_balance':{'gross_amount':'1491','amount':'1491',
                                                     'as_of':previous.isoformat()}}]})
        with TestClient(main.app,base_url='http://pams.local') as web:
            response=web.get('/api/account-performance/ibkr')
            assert response.status_code==200,response.text
            payload=response.json()
            assert Decimal(payload['net_liquidation_cny'])==Decimal(1631)
            assert Decimal(payload['daily_pnl_cny'])==0 and Decimal(payload['total_pnl_cny'])==0
            assert payload['external_flow_coverage']=='verified'
            assert payload['daily_pnl_evidence']['source_sha256']
        assert performance(initial_day=None)['total_pnl_cny'] is None
        assert performance(initial_day=previous-timedelta(days=1))['total_pnl_cny'] is None
        with psycopg2.connect(**empty_source_db) as read:
            read.set_session(readonly=True,isolation_level='REPEATABLE READ')
            with read.cursor(cursor_factory=RealDictCursor) as cur:
                current_only=build_account_performance_payload(cur,account_code='ibkr',owner_ratio=Decimal('0.5'),
                    initial_capital=None,daily_pnl_label_date=DAY)
                assert Decimal(current_only['net_liquidation_cny'])==Decimal('815.5')
                assert Decimal(current_only['cash_or_uninvested_cny'])==Decimal(385)
                assert Decimal(current_only['daily_pnl_cny'])==0
                assert Decimal(current_only['observed_external_net_flow_cny'])==Decimal(70)
                assert current_only['total_pnl_cny'] is None
        # Prior-day USD and EUR prices can differ from the current day; the
        # USD deposit uses its own event day, while prior NAV uses prior FX.
        with conn.cursor() as cur:
            cur.execute("UPDATE pams_fx_rates_daily SET rate=6.8 WHERE as_of=%s AND quote_currency='USD'",(previous,))
            cur.execute("UPDATE pams_fx_rates_daily SET rate=13.6 WHERE as_of=%s AND quote_currency='EUR'",(previous,))
        conn.commit()
        with psycopg2.connect(**empty_source_db) as read:
            read.set_session(readonly=True,isolation_level='REPEATABLE READ')
            with read.cursor(cursor_factory=RealDictCursor) as cur:
                cross_day=build_account_performance_payload(cur,account_code='ibkr',owner_ratio=Decimal(1),
                    initial_capital=Decimal('1448.4'),initial_capital_as_of=previous,
                    net_liquidation_gross=Decimal('1448.4'),net_liquidation_as_of=previous,
                    daily_pnl_label_date=DAY)
                assert Decimal(cross_day['net_liquidation_cny'])==Decimal(1631)
                assert Decimal(cross_day['observed_external_net_flow_cny'])==Decimal(140)
                assert Decimal(cross_day['daily_pnl_cny'])==Decimal('42.6')
                assert Decimal(cross_day['total_pnl_cny'])==Decimal('42.6')
        with conn.cursor() as cur:
            cur.execute("UPDATE pams_fx_rates_daily SET rate=7 WHERE as_of=%s AND quote_currency='USD'",(previous,))
            cur.execute("UPDATE pams_fx_rates_daily SET rate=14 WHERE as_of=%s AND quote_currency='EUR'",(previous,))
        conn.commit()
        # An undocumented FX update invalidates the period, and must not be 1.
        with conn.cursor() as cur:
            cur.execute("UPDATE pams_fx_rates_daily SET source='' WHERE as_of=%s AND quote_currency='USD'",(DAY,))
        conn.commit()
        assert performance() is None
        # Corrupt original evidence under test-only superuser privileges; the
        # published hash must invalidate the claimed scope and all returns.
        with conn.cursor() as cur:
            cur.execute("UPDATE pams_fx_rates_daily SET source='synthetic' WHERE as_of=%s AND quote_currency='USD'",(DAY,))
            cur.execute("ALTER TABLE source_files DISABLE TRIGGER source_files_evidence_immutable")
            cur.execute("UPDATE source_files SET content=content || %s::bytea WHERE batch_id=%s",(b'x',current['batch_id']))
            cur.execute("ALTER TABLE source_files ENABLE TRIGGER source_files_evidence_immutable")
        conn.commit()
        assert performance() is None
