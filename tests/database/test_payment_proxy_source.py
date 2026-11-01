from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import psycopg2
from fastapi import FastAPI
from fastapi.testclient import TestClient
from scripts.bootstrap_isolated_sources import initialize_isolated_sources
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401


def test_proxy_candidates_confirm_dedup_revoke_and_ambiguity(empty_source_db, monkeypatch):  # noqa: F811
    from domain.imports import account_import_statement as statement, account_import_store as store
    from endpoints import account_import, bank_reconcile, ledger
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO pams_accounts(account_code,display_name,base_currency) VALUES ('ccb_7332','A','CNY'),('wechat','B','CNY') ON CONFLICT (account_code) DO UPDATE SET base_currency='CNY'")
    monkeypatch.setenv('PAMS_SOURCE_TEST_TOKEN_KEY','a1'*32)
    monkeypatch.setenv('PAMS_IMPORT_PUBLISH_ENABLED','true')
    for module in (store,bank_reconcile,ledger):
        monkeypatch.setattr(module,'get_conn',lambda:psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(bank_reconcile,'_load_config',lambda:{})
    monkeypatch.setattr(statement,'config',lambda:{'profiles':{
        'a':{'account_code':'ccb_7332','statement_template':'manual_statement_csv','currency':'CNY'},
        'b':{'account_code':'wechat','statement_template':'manual_statement_csv','currency':'CNY'}},'rules':[],'amount_rules':[]})
    app=FastAPI()
    for r in (account_import.router,bank_reconcile.router,ledger.router): app.include_router(r,prefix='/api')
    day=date.today()-timedelta(days=2)
    def publish(web, account, summary):
        batch=web.post('/api/account-import/batches',json={'account_code':account,'mode':'upload','kind':'cash'}).json()
        path='/api/account-import/batches/'+batch['batch_id']
        batch=web.post(path+'/files?filename=bank.csv',content=f'transaction_date,amount,currency,summary\n{day},-10,CNY,{summary}\n'.encode()).json()
        batch['rows'][0]['fields']['action']='expense'
        response=web.put(path,json={'revision':batch['revision'],'rows':batch['rows']})
        assert response.status_code==200,response.text
        batch=response.json()
        check=web.post(path+'/preview',json={'revision':batch['revision']}).json()
        assert check['can_publish'], __import__('json').dumps(check,ensure_ascii=False,default=str)
        response=web.post(path+'/publish',json={'revision':batch['revision'],'preview_token':check['preview_token']})
        assert response.status_code==200,response.text
    with TestClient(app) as web:
        web.headers['Origin']='http://testserver'
        publish(web,'ccb_7332','财付通')
        publish(web,'wechat','建设银行储蓄卡(7332)')
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            cur.execute('SELECT cashflow_id,account_code FROM pams_cashflow_fact')
            ids={account:ident for ident,account in cur.fetchall()}
        suggestions=web.post('/api/bank-reconcile/transfer-matches/run').json()
        assert any(x['match_type']=='payment_proxy' and x['status']=='review_required' for x in suggestions['matches']),suggestions
        url='/api/ledger/workbench'
        def data():
            r=web.get(url,params={'month':day.strftime('%Y-%m')})
            assert r.status_code==200,r.text
            return r.json()
        before=data()
        before_ids={r['id'] for r in before['recent_rows_month']}
        assert before['periods']['month']['expense'] == 20
        assert {ids['ccb_7332'],ids['wechat']} <= before_ids
        req={'source_id':ids['ccb_7332'],'target_id':ids['wechat'],'confirmed':True,'note':'人工核对支付通道和银行两侧'}
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses=list(pool.map(lambda _:web.post('/api/bank-reconcile/payment-proxy/confirm',json=req),range(2)))
        assert all(response.status_code==200 for response in responses),[response.text for response in responses]
        assert sorted(response.json()['replayed'] for response in responses)==[False,True]
        ident=responses[0].json()['confirmation_id']
        assert responses[1].json()['confirmation_id']==ident
        assert web.post('/api/bank-reconcile/payment-proxy/confirm',json=req).json()['replayed']
        after=data()
        after_ids={r['id'] for r in after['recent_rows_month']}
        assert ids['ccb_7332'] not in after_ids and ids['wechat'] in after_ids
        assert after['periods']['month']['expense'] == 10
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            cur.execute("SELECT i.file_id FROM source_items i JOIN pams_cashflow_fact f ON f.source_item_id=i.source_item_id WHERE f.cashflow_id=%s",(ids['wechat'],))
            file_id=cur.fetchone()[0]
            cur.execute('ALTER TABLE source_files DISABLE TRIGGER USER')
            cur.execute("UPDATE source_files SET content=content || decode('00','hex') WHERE file_id=%s",(file_id,))
            cur.execute('ALTER TABLE source_files ENABLE TRIGGER USER')
        assert web.get('/api/bank-reconcile/payment-proxy').json()['decisions'][0]['evidence_status']=='invalidated'
        assert {ids['ccb_7332'],ids['wechat']} <= {r['id'] for r in data()['recent_rows_month']}
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            cur.execute('ALTER TABLE source_files DISABLE TRIGGER USER')
            cur.execute('UPDATE source_files SET content=substring(content FROM 1 FOR octet_length(content)-1) WHERE file_id=%s',(file_id,))
            cur.execute('ALTER TABLE source_files ENABLE TRIGGER USER')
        # New competing same-day evidence invalidates an earlier decision.
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            cur.execute("""INSERT INTO pams_cashflow_fact(event_date,account_code,amount,currency,amount_cny,
                cashflow_type,source_table,source_id,source_hash) VALUES (%s,'wechat',-10,'CNY',-10,
                'operating_expense','synthetic','ambiguous-before',%s)""", (day,'c'*64))
        assert web.get('/api/bank-reconcile/payment-proxy').json()['decisions'][0]['evidence_status']=='invalidated'
        assert {ids['ccb_7332'],ids['wechat']} <= {r['id'] for r in data()['recent_rows_month']}
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            cur.execute("DELETE FROM pams_cashflow_fact WHERE source_id='ambiguous-before'")
        assert web.post(f'/api/bank-reconcile/payment-proxy/{ident}/revoke',json={'note':'错误核准','confirmed':True}).status_code==200
        assert {ids['ccb_7332'],ids['wechat']} <= {r['id'] for r in data()['recent_rows_month']}
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            cur.execute("""INSERT INTO pams_cashflow_fact(event_date,account_code,amount,currency,amount_cny,
                cashflow_type,source_table,source_id,source_hash) VALUES (%s,'wechat',-10,'CNY',-10,
                'operating_expense','synthetic','ambiguous',%s)""", (day,'b'*64))
        response=web.post('/api/bank-reconcile/payment-proxy/confirm',json=req)
        assert response.status_code==409 and 'ambiguous' in response.text
