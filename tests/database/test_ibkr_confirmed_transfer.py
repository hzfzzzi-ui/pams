from datetime import date
from hashlib import sha256

import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI
from fastapi.testclient import TestClient
from scripts.bootstrap_isolated_sources import initialize_isolated_sources
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401


def test_confirm_bank_and_ibkr_cash_from_verified_originals(empty_source_db, monkeypatch):  # noqa: F811
    from collectors.ibkr_flex import parse_flex_report
    from domain.brokers.ibkr.ibkr_cash_events import import_cash_events
    from domain.brokers.ibkr.ibkr_report_batch import publish_report_batch
    from tests.database.test_ibkr_fact_source_verification import cash_report
    from domain.imports import account_import_statement as statement, account_import_store as store
    from endpoints import account_import, bank_reconcile
    day = date(2026, 10, 22)
    payload = cash_report(day).replace(b'currency="EUR"', b'currency="CNY"').replace(
        b'type="Dividends"', b'type="Deposits &amp; Withdrawals"')
    digest = sha256(payload).hexdigest()
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO pams_accounts(account_code,display_name,base_currency) VALUES ('bank_a','A','CNY')")
            cur.execute("UPDATE pams_accounts SET base_currency='CNY' WHERE account_code='ibkr'")
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'synthetic')", (day,))
        conn.commit()
        parsed = parse_flex_report(payload.decode(), account_code='ibkr')
        envelope = parsed.cash_events[0]
        envelope['source_evidence'] = {'broker_source':'ibkr_flex','source_sha256':digest,'confirmed_day':day.isoformat()}
        events = import_cash_events(conn,[envelope],account_code='ibkr',cutoff=date(2026,1,1),statement_as_of=day,defer_source_link=True)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            publish_report_batch(cur,report_bytes=payload,source_account_id='U_TEST',day=day,report_sha256=digest,
                                 cash_entries=events['_deferred_source_events'])
            cur.execute("SELECT trade_id FROM pams_investment_trade_fact WHERE source_table='ibkr_flex_cash_transaction'")
            trade_id = cur.fetchone()['trade_id']
    monkeypatch.setenv('PAMS_SOURCE_TEST_TOKEN_KEY','a1'*32)
    monkeypatch.setenv('PAMS_IMPORT_PUBLISH_ENABLED','true')
    for module in (store,bank_reconcile):
        monkeypatch.setattr(module,'get_conn',lambda:psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(statement,'config',lambda:{'profiles':{'a':{'account_code':'bank_a',
        'statement_template':'manual_statement_csv','currency':'CNY'}},'rules':[],'amount_rules':[]})
    app=FastAPI()
    app.include_router(account_import.router,prefix='/api')
    app.include_router(bank_reconcile.router,prefix='/api')
    with TestClient(app) as web:
        web.headers['Origin']='http://testserver'
        response=web.post('/api/account-import/batches',json={'account_code':'bank_a','mode':'upload','kind':'cash'})
        path='/api/account-import/batches/'+response.json()['batch_id']
        response=web.post(path+'/files?filename=bank.csv',content=f'transaction_date,amount,currency,summary\n{day},-10,CNY,transfer\n'.encode())
        batch=response.json()
        batch['rows'][0]['fields'].update(action='transfer',counter_account_code='ibkr')
        batch=web.put(path,json={'revision':batch['revision'],'rows':batch['rows']}).json()
        check=web.post(path+'/preview',json={'revision':batch['revision']}).json()
        assert check['can_publish'], __import__('json').dumps(check, ensure_ascii=False, default=str)
        response=web.post(path+'/publish',json={'revision':batch['revision'],'preview_token':check['preview_token']})
        assert response.status_code==200,response.text
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            cur.execute("SELECT cashflow_id FROM pams_cashflow_fact WHERE account_code='bank_a'")
            bank_id=cur.fetchone()[0]
        request={'from_kind':'cashflow','from_id':bank_id,'to_kind':'trade','to_id':trade_id,
                 'note':'人工核对双侧来源','confirmed':True}
        response=web.post('/api/bank-reconcile/transfers/confirm',json=request)
        assert response.status_code==200,response.text
        transfer_id=response.json()['transfer_id']
        assert web.post('/api/bank-reconcile/transfers/confirm',json=request).json()['replayed']
        assert web.get('/api/bank-reconcile/transfer-matches').json()['matches'][0]['evidence_status']=='confirmed'
        monkeypatch.delenv('PAMS_UNIFIED_SOURCE_ISOLATED',raising=False)
        assert web.get('/api/bank-reconcile/transfer-matches').json()['matches'][0]['evidence_status']=='confirmed'
        monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED','true')
        from domain.ledger.confirmed_transfers import read_one
        with psycopg2.connect(**empty_source_db) as conn:
            conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                assert read_one(cur, transfer_id)['evidence_status']=='confirmed'
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            cur.execute('ALTER TABLE source_files DISABLE TRIGGER USER')
            cur.execute("UPDATE source_files SET content=content || decode('00','hex') WHERE sha256=%s",(digest,))
            cur.execute('ALTER TABLE source_files ENABLE TRIGGER USER')
        assert web.get('/api/bank-reconcile/transfer-matches').json()['matches'][0]['evidence_status']=='invalidated'
        assert web.post('/api/bank-reconcile/transfers/confirm',json=request).status_code==409
        assert web.post(f'/api/bank-reconcile/transfers/{transfer_id}/revoke',json={'note':'correction','confirmed':True}).status_code==200
