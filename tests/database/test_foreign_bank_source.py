# ruff: noqa: F811
from copy import deepcopy
from datetime import date
from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient
import psycopg2
from psycopg2.extras import RealDictCursor

from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from scripts import bootstrap_isolated_sources as bootstrap


def test_foreign_statement_import_requires_dated_fx_and_preserves_evidence(empty_source_db, monkeypatch):
    from domain.imports import account_import_statement as statement, account_import_store as store
    from domain.imports.source_fact_evidence import verify_native_fact
    from endpoints import account_import, bank_reconcile, ledger
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO pams_accounts(account_code,display_name,base_currency) VALUES ('usd_bank','USD bank','USD')")
    monkeypatch.setenv('PAMS_SOURCE_TEST_TOKEN_KEY','a1'*32)
    monkeypatch.setenv('PAMS_IMPORT_PUBLISH_ENABLED','true')
    for module in (store,bank_reconcile,ledger):
        monkeypatch.setattr(module,'get_conn',lambda: psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(statement,'config',lambda: {'profiles':{'usd':{'account_code':'usd_bank','statement_template':'manual_statement_csv','currency':'USD'}}, 'rules':[],'amount_rules':[]})
    app=FastAPI()
    for router in (account_import.router,bank_reconcile.router,ledger.router):
        app.include_router(router,prefix='/api')
    day=date(2025,8,12)
    with TestClient(app) as web:
        web.headers['Origin']='http://testserver'
        response=web.post('/api/account-import/batches',json={'account_code':'usd_bank','mode':'upload','kind':'cash'})
        assert response.status_code==200,response.text
        path='/api/account-import/batches/'+response.json()['batch_id']
        content=f'transaction_date,amount,currency,summary\n{day},-10,USD,synthetic\n'.encode()
        response=web.post(path+'/files?filename=bank.csv',content=content)
        assert response.status_code==200,response.text
        batch=response.json()
        response=web.put(path,json={'revision':batch['revision'],'rows':batch['rows']})
        assert response.status_code==200,response.text
        batch=response.json()
        def preview():
            response=web.post(path+'/preview',json={'revision':batch['revision']})
            assert response.status_code==200,response.text
            return response.json()
        check=preview()
        assert not check['can_publish'],check
        # File currency cannot be rewritten, and an explicitly conflicting
        # reviewed direction is rejected by preflight.
        forged=deepcopy(batch['rows'])
        forged[0]['fields']['currency']='EUR'
        response=web.put(path,json={'revision':batch['revision'],'rows':forged})
        assert response.status_code==400 and response.json()['detail']['code']=='file_row_readonly'
        batch['rows'][0]['fields']['action']='income'
        response=web.put(path,json={'revision':batch['revision'],'rows':batch['rows']})
        assert response.status_code==200,response.text
        batch=response.json()
        assert not preview()['can_publish']
        batch['rows'][0]['fields']['action']='expense'
        response=web.put(path,json={'revision':batch['revision'],'rows':batch['rows']})
        assert response.status_code==200,response.text
        batch=response.json()
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7.12345678,'')",(day,))
        assert not preview()['can_publish']
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            cur.execute("UPDATE pams_fx_rates_daily SET source='synthetic_fx' WHERE as_of=%s AND quote_currency='USD'",(day,))
        check=preview()
        assert check['can_publish'],check
        # Updating the dated quote invalidates the preflight token.
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            cur.execute("UPDATE pams_fx_rates_daily SET rate=7.2 WHERE as_of=%s AND quote_currency='USD'",(day,))
        response=web.post(path+'/publish',json={'revision':batch['revision'],'preview_token':check['preview_token']})
        assert response.status_code==409,response.text
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            cur.execute('SELECT count(*) FROM pams_cashflow_fact')
            assert cur.fetchone()[0]==0
        check=preview()
        assert check['can_publish'],check
        response=web.post(path+'/publish',json={'revision':batch['revision'],'preview_token':check['preview_token']})
        assert response.status_code==200,response.text
        replay=web.post(path+'/publish',json={'revision':batch['revision'],'preview_token':check['preview_token']})
        assert replay.status_code==200 and replay.json()['replayed'],replay.text
        bank=web.get('/api/bank-reconcile/imports')
        assert bank.status_code==200 and bank.json()['summary'][0]['currency']=='USD',bank.text
        response=web.get('/api/ledger/workbench',params={'month':'2025-08','baseCurrency':'CNY'})
        assert response.status_code==200,response.text
        ledger_rows=[row for row in response.json()['recent_rows_month'] if row['account_code']=='usd_bank']
        assert len(ledger_rows)==1 and ledger_rows[0]['source_evidence_status']=='verified'
        assert ledger_rows[0]['amount_cny']==-72
        response=web.get('/api/ledger/workbench',params={'month':'2025-08','baseCurrency':'USD'})
        assert response.status_code==200,response.text
        ledger_rows=[row for row in response.json()['recent_rows_month'] if row['account_code']=='usd_bank']
        assert ledger_rows[0]['amount_base']==-10 and ledger_rows[0]['amount_cny'] is None
        with psycopg2.connect(**empty_source_db) as conn:
            conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute('SELECT * FROM pams_cashflow_fact')
                fact=cur.fetchone()
                assert fact['amount']==Decimal('-10') and fact['amount_cny']==Decimal('-72')
                assert fact['raw_json']['fx_evidence']['source']=='synthetic_fx'
                assert verify_native_fact(cur,table='pams_cashflow_fact',fact_id=fact['cashflow_id'])['status']=='verified'
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            cur.execute("UPDATE pams_fx_rates_daily SET source='replacement' WHERE as_of=%s AND quote_currency='USD'",(day,))
        with psycopg2.connect(**empty_source_db) as conn:
            conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                assert verify_native_fact(cur,table='pams_cashflow_fact',fact_id=fact['cashflow_id'])['status']=='unknown'


def test_foreign_transfer_requires_two_proven_sides_and_replays(empty_source_db, monkeypatch):
    from domain.imports import account_import_statement as statement, account_import_store as store
    from endpoints import account_import, bank_reconcile
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO pams_accounts(account_code,display_name,base_currency) VALUES ('usd_a','A','USD'),('usd_b','B','USD')")
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES ('2025-08-12','CNY','USD',7.2,'synthetic_fx')")
    monkeypatch.setenv('PAMS_SOURCE_TEST_TOKEN_KEY','a1'*32)
    monkeypatch.setenv('PAMS_IMPORT_PUBLISH_ENABLED','true')
    for module in (store,bank_reconcile):
        monkeypatch.setattr(module,'get_conn',lambda: psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(statement,'config',lambda: {'profiles':{
        'a':{'account_code':'usd_a','statement_template':'manual_statement_csv','currency':'USD'},
        'b':{'account_code':'usd_b','statement_template':'manual_statement_csv','currency':'USD'}},'rules':[],'amount_rules':[]})
    app=FastAPI()
    for router in (account_import.router,bank_reconcile.router):
        app.include_router(router,prefix='/api')
    with TestClient(app) as web:
        web.headers['Origin']='http://testserver'
        for account,counter,amount in [('usd_a','usd_b','-10'),('usd_b','usd_a','10')]:
            response=web.post('/api/account-import/batches',json={'account_code':account,'mode':'upload','kind':'cash'})
            assert response.status_code==200,response.text
            path='/api/account-import/batches/'+response.json()['batch_id']
            content=f'transaction_date,amount,currency,summary\n2025-08-12,{amount},USD,transfer\n'.encode()
            response=web.post(path+'/files?filename=bank.csv',content=content)
            assert response.status_code==200,response.text
            batch=response.json()
            batch['rows'][0]['fields'].update(action='transfer',counter_account_code=counter)
            response=web.put(path,json={'revision':batch['revision'],'rows':batch['rows']})
            assert response.status_code==200,response.text
            batch=response.json()
            response=web.post(path+'/preview',json={'revision':batch['revision']})
            assert response.status_code==200 and response.json()['can_publish'],response.text
            response=web.post(path+'/publish',json={'revision':batch['revision'],'preview_token':response.json()['preview_token']})
            assert response.status_code==200,response.text
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            cur.execute('SELECT cashflow_id FROM pams_cashflow_fact ORDER BY account_code')
            first,second=[row[0] for row in cur.fetchall()]
        payload={'from_kind':'cashflow','from_id':first,'to_kind':'cashflow','to_id':second,'note':'双方核对','confirmed':True}
        response=web.post('/api/bank-reconcile/transfers/confirm',json=payload)
        assert response.status_code==200,response.text
        response=web.post('/api/bank-reconcile/transfers/confirm',json=payload)
        assert response.status_code==200 and response.json()['replayed'],response.text
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            cur.execute("UPDATE pams_fx_rates_daily SET source='changed' WHERE as_of='2025-08-12' AND quote_currency='USD'")
        response=web.get('/api/bank-reconcile/transfer-matches')
        assert response.status_code==200,response.text
        assert response.json()['matches'][0]['evidence_status']=='invalidated'
