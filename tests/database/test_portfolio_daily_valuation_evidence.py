"""Portfolio daily P&L requires proven complete account periods on isolated PG."""
# ruff: noqa: F811
import csv
import io
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import psycopg2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.imports.test_account_import_publish import client, draft, holding, preview, publish  # noqa: F401


@pytest.mark.parametrize('pnl',[Decimal('0'),Decimal('10'),Decimal('-10')])
def test_daily_pnl_is_unknown_until_source_complete_then_roundtrips_over_http(client,monkeypatch,pnl):
    from domain.investments import portfolio_valuation_state as service
    from endpoints import portfolio_state as route

    web,params=client
    anchor=date.today()-timedelta(days=3); end=anchor+timedelta(days=1)
    with psycopg2.connect(**params) as conn,conn.cursor() as cur:
        for filename in ('004_fx_rates.sql','025_valuation_results.sql'):
            cur.execute((Path(__file__).resolve().parents[2]/'database/postgres/schema'/filename).read_text(encoding='utf-8'))

    def publish_source(fields, complete=False):
        batch=draft(web,fields,completeness='complete' if complete else 'partial')
        if complete:
            response=web.post('/api/account-import/batches/'+batch['batch_id']+'/confirm-scope',json={
                'revision':batch['revision'],'as_of':fields[0]['business_date'],'member_count':2,
                'cash_amount':fields[1]['net_cash'],'note':'checked full synthetic snapshot','confirmed':True})
            assert response.status_code==200,response.text
            batch=web.get('/api/account-import/batches/'+batch['batch_id']).json()
        result=preview(web,batch)
        assert result['can_publish'],result
        response=publish(web,batch,result)
        assert response.status_code==200,response.text

    def snapshot(day,price,cash):
        publish_source([holding(business_date=day.isoformat(),quantity='100',price=str(price),
            market_value=str(price*100),cost='900'),
            holding(business_date=day.isoformat(),asset_type='cash',symbol=None,action='balance',net_cash=str(cash))],True)

    snapshot(anchor,Decimal(10),100)
    publish_source([{'kind':'cash','business_date':end.isoformat(),'currency':'CNY',
        'action':'deposit','net_cash':'20','counter_account_code':'bank_test'}])
    snapshot(end,(Decimal(1000)+pnl)/100,120)
    net=Decimal(1120)+pnl
    account=[{'account_code':'tonghuashun','include_in_nav':True,'amount_cny':net,'as_of':end}]
    positions=[{'account_code':'tonghuashun','asset_type':'stock','include_in_nav':True,
        'market_value_cny':Decimal(1000)+pnl,'holding_pnl_cny':Decimal(100)+pnl,'as_of':end,
        'snapshot_scope_status':'complete'},
        {'account_code':'tonghuashun','asset_type':'cash','include_in_nav':True,
         'market_value_cny':Decimal(120),'holding_pnl_cny':Decimal(0),'as_of':end,'snapshot_scope_status':'complete'}]
    monkeypatch.setattr(service,'_load_accounts_config',lambda:{'defaults':{'owner_ratio':1},'accounts':{}})
    monkeypatch.setattr(service,'load_account_balances',lambda *args:account)
    monkeypatch.setattr(service,'load_investment_positions',lambda *args,**kwargs:positions)
    factory=lambda:psycopg2.connect(**params)
    monkeypatch.setattr(route,'build_portfolio_valuation_state_payload',lambda **kwargs:
                        service.build_portfolio_valuation_state_payload(**kwargs,conn_factory=factory))
    monkeypatch.setattr(route,'publish_portfolio_valuation_state_payload',lambda **kwargs:
                        service.publish_portfolio_valuation_state_payload(**kwargs,conn_factory=factory))
    app=FastAPI(); app.include_router(route.router,prefix='/api')
    with TestClient(app) as http:
        first=http.post('/api/portfolio/valuation-state/publish')
        assert first.status_code==200,first.text
        assert first.json()['daily_pnl_cny'] is None and Decimal(str(first.json()['net_liquidation_value']))==net
        assert http.get('/api/portfolio/valuation-state').json()['daily_pnl_cny'] is None
        with psycopg2.connect(**params) as conn,conn.cursor() as cur:
            cur.execute("SELECT trade_id,source_key FROM pams_investment_trade_fact WHERE account_code='tonghuashun' AND trade_type='deposit'")
            fact_id,key=cur.fetchone()
        text=io.StringIO(newline=''); writer=csv.writer(text,lineterminator='\n')
        writer.writerow(['account_code','period_start','period_end','event_date','side','currency','net_amount','external_id'])
        writer.writerow(['tonghuashun',end.isoformat(),end.isoformat(),end.isoformat(),'deposit','CNY','20',key])
        response=web.post('/api/account-import/flow-coverage/stage?account_code=tonghuashun&filename=scope.csv',
                          content=text.getvalue().encode(),headers={'Content-Type':'text/csv'})
        assert response.status_code==200,response.text
        batch=response.json()
        response=web.post('/api/account-import/flow-coverage/'+batch['batch_id']+'/publish',json={
            'revision':batch['revision'],'mappings':{key:fact_id},'confirmed':True,'note':'reviewed all movements'})
        assert response.status_code==200,response.text
        final=http.post('/api/portfolio/valuation-state/publish')
        assert final.status_code==200,final.text
        payload=final.json()
        assert Decimal(str(payload['daily_pnl_cny']))==pnl and Decimal(str(payload['net_liquidation_value']))==net
        assert payload['raw_json']['daily_pnl_evidence'][0]['account_code']=='tonghuashun'
        again=http.get('/api/portfolio/valuation-state')
        assert again.status_code==200 and Decimal(str(again.json()['daily_pnl_cny']))==pnl
        assert again.json()['source_hash']==payload['source_hash']
    with psycopg2.connect(**params) as conn,conn.cursor() as cur:
        cur.execute("SELECT status,daily_pnl_cny,net_liquidation_value FROM valuation_results WHERE result_type='portfolio_published'")
        assert cur.fetchall()==[('published',pnl,net)]


def test_empty_inputs_do_not_publish_fabricated_zero():
    from domain.investments.portfolio_valuation_state import build_portfolio_valuation_state_from_parts
    result=build_portfolio_valuation_state_from_parts(account_balances=[],investment_positions=[])
    assert result['data_completeness']=='partial' and result['net_liquidation_value'] is None
    assert result['daily_pnl_cny'] is None
