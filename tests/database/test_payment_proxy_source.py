from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from uuid import uuid4

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
        root = '/api/account-import/source-records'
        content = f'transaction_date,amount,currency,summary\n{day},-10,CNY,{summary}\n'.encode()
        response = web.post(root + f'?account_code={account}&source_name=synthetic&filename=bank.csv', content=content)
        assert response.status_code == 200, response.text
        path = root + '/' + response.json()['source_id']
        staged = web.get(path).json()
        response = web.put(path + '/reviews', json={'revision': staged['revision'], 'items': [
            {'source_item_id': row['source_item_id'], 'parsed_fields': row['parsed_fields'],
             'review_status': 'excluded' if row['parsed_fields'].get('action') == 'balance' else 'confirmed'}
            for row in staged['items']]})
        assert response.status_code == 200, response.text
        payload = {'revision': response.json()['revision'], 'mode': 'all'}
        response = web.post(path + '/cash-preview', json=payload)
        assert response.status_code == 200 and response.json()['can_publish'], response.text
        payload['preview_token'] = response.json()['preview_token']
        response = web.post(path + '/cash-publish', json=payload)
        assert response.status_code == 200, response.text

    def add_competing_fact():
        from psycopg2.extras import RealDictCursor
        from domain.imports import source_records_v2 as source

        with psycopg2.connect(**empty_source_db) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            source_id, item_id = uuid4(), uuid4()
            fields = {'business_date': day.isoformat(), 'currency': 'CNY', 'net_cash': '-10'}
            source.create_source(cur, source_id=source_id, account_code='wechat', source_name='synthetic',
                                 acquisition_method='manual', document_type='cash')
            source.add_item(cur, source_id=source_id, item_id=item_id, parse_method='manual',
                            raw_fields={'entered_fields': fields}, parsed_fields=fields)
            source.review_item(cur, source_id=source_id, item_id=item_id, expected_revision=2,
                               parsed_fields=fields, review_status='confirmed')
            cur.execute("UPDATE source_records SET status='published' WHERE source_id=%s", (str(source_id),))
            cur.execute("""INSERT INTO pams_cashflow_fact(event_date,account_code,amount,currency,source_item_id,fact_role)
                VALUES (%s,'wechat',-10,'CNY',%s,'statement_cashflow') RETURNING cashflow_id""", (day, str(item_id)))
            return cur.fetchone()['cashflow_id']

    with TestClient(app) as web:
        web.headers['Origin']='http://testserver'
        publish(web,'ccb_7332','财付通')
        publish(web,'wechat','建设银行储蓄卡(7332)')
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            cur.execute('SELECT cashflow_id,account_code FROM pams_cashflow_fact')
            ids={account:ident for ident,account in cur.fetchall()}
        for fact_id in ids.values():
            response = web.post(f'/api/ledger/review/{fact_id}/confirm', json={})
            assert response.status_code == 200, response.text
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
            cur.execute("SELECT i.source_id FROM source_items i JOIN pams_cashflow_fact f ON f.source_item_id=i.source_item_id WHERE f.cashflow_id=%s",(ids['wechat'],))
            source_record_id=cur.fetchone()[0]
            cur.execute('ALTER TABLE source_records DISABLE TRIGGER USER')
            cur.execute("UPDATE source_records SET content=content || decode('00','hex') WHERE source_id=%s",(source_record_id,))
            cur.execute('ALTER TABLE source_records ENABLE TRIGGER USER')
        assert web.get('/api/bank-reconcile/payment-proxy').json()['decisions'][0]['evidence_status']=='invalidated'
        assert {ids['ccb_7332'],ids['wechat']} <= {r['id'] for r in data()['recent_rows_month']}
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            cur.execute('ALTER TABLE source_records DISABLE TRIGGER USER')
            cur.execute('UPDATE source_records SET content=substring(content FROM 1 FOR octet_length(content)-1) WHERE source_id=%s',(source_record_id,))
            cur.execute('ALTER TABLE source_records ENABLE TRIGGER USER')
        # New competing same-day evidence invalidates an earlier decision.
        competing = add_competing_fact()
        assert web.get('/api/bank-reconcile/payment-proxy').json()['decisions'][0]['evidence_status']=='invalidated'
        assert {ids['ccb_7332'],ids['wechat']} <= {r['id'] for r in data()['recent_rows_month']}
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            cur.execute("DELETE FROM pams_cashflow_fact WHERE cashflow_id=%s", (competing,))
        assert web.post(f'/api/bank-reconcile/payment-proxy/{ident}/revoke',json={'note':'错误核准','confirmed':True}).status_code==200
        assert {ids['ccb_7332'],ids['wechat']} <= {r['id'] for r in data()['recent_rows_month']}
        add_competing_fact()
        response=web.post('/api/bank-reconcile/payment-proxy/confirm',json=req)
        assert response.status_code==409 and 'ambiguous' in response.text
