"""Trade list FX evidence, using only a disposable bootstrapped database."""
# ruff: noqa: F811
from datetime import date, timedelta
from decimal import Decimal
from fastapi import FastAPI
from fastapi.testclient import TestClient
import psycopg2
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from scripts.bootstrap_isolated_sources import initialize_isolated_sources
from endpoints import investment_transactions as endpoint


def test_investment_cashflow_keeps_native_amount_when_fx_missing(empty_source_db):
    from psycopg2.extras import RealDictCursor
    from domain.ledger.ledger_view import _load_investment_trade_account_rows, _account_summary, _serialize_row
    day = date(2026, 10, 22)
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""INSERT INTO pams_investment_trade_fact
                (trade_date,account_code,currency,trade_type,side,gross_amount,net_cash,net_cash_cny,
                 source_table,source_id,source_hash)
                VALUES (%s,'ibkr','USD','deposit','deposit',100,100,100,'synthetic','funding','funding-hash')""", (day,))
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',8,'synthetic')", (day+timedelta(days=1),))
            rows = _load_investment_trade_account_rows(cur,start=day,end=day,base_currency='CNY')
            assert len(rows)==1 and rows[0]['amount']==100 and rows[0]['amount_cny'] is None
            assert _serialize_row(rows[0])['amount_cny'] is None
            summary = _account_summary(rows, {'ibkr'})[0]
            assert summary['fx_status']=='incomplete' and summary['investment_inflow'] is None and summary['net'] is None
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'synthetic')", (day,))
            rows = _load_investment_trade_account_rows(cur,start=day,end=day,base_currency='CNY')
            assert rows[0]['amount_cny']==700 and _account_summary(rows, {'ibkr'})[0]['investment_inflow']==700


def test_transfer_summary_never_uses_a_future_fx_rate(empty_source_db, monkeypatch):
    from psycopg2.extras import RealDictCursor
    from domain.ledger.ledger_view import _load_transfer_matches, _load_pending_transfer_cases, _transfer_summary, summarize_period
    day=date(2026,10,22)
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            from domain.ledger import confirmed_transfers
            common = {'from_account_code':'bank_a', 'to_account_code':'ibkr', 'amount':Decimal('100'),
                      'currency':'USD', 'from_evidence':{'kind':'cashflow','id':1,'day':str(day)},
                      'to_evidence':{'kind':'trade','id':2,'day':str(day)}}
            records = [{**common, 'transfer_id':'valid','evidence_status':'confirmed'},
                       {**common, 'transfer_id':'invalid','evidence_status':'invalidated'}]
            monkeypatch.setattr(confirmed_transfers, 'read', lambda cur, limit=None: records)
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',8,'synthetic')",(day+timedelta(days=1),))
            rows=_load_transfer_matches(cur,start=day,end=day,base_currency='CNY')
            assert len(rows)==1 and rows[0]['amount_cny'] is None
            pending=_load_pending_transfer_cases(cur,start=day,end=day)
            assert len(pending)==1 and pending[0]['status']=='invalidated' and pending[0]['amount'] is None
            rows[0]['match_date_obj']=day
            summary=summarize_period([],rows,day,day)
            assert summary['internal_transfer'] is None and summary['investment_transfer'] is None
            assert _transfer_summary(rows)[0]['amount'] is None
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'synthetic')",(day,))
            rows=_load_transfer_matches(cur,start=day,end=day,base_currency='CNY')
            assert rows[0]['amount_cny']==700 and _transfer_summary(rows)[0]['amount']==700


def test_only_verified_fact_link_deduplicates_investment_funding(empty_source_db):
    from psycopg2.extras import RealDictCursor
    from domain.ledger.ledger_view import _load_investment_account_rows
    day=date(2026,10,22)
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("INSERT INTO pams_accounts(account_code,display_name) VALUES ('test_bank','Synthetic bank') ON CONFLICT DO NOTHING")
            cur.execute("""INSERT INTO pams_cashflow_fact(event_date,account_code,counter_account_code,amount,currency,
                fx_rate_to_cny,amount_cny,cashflow_type,source_table,source_id,source_hash)
                VALUES (%s,'test_bank','ibkr',-100,'USD',1,-100,'investment_funding','synthetic','cash','cash-hash')
                RETURNING cashflow_id""",(day,))
            cashflow_id=cur.fetchone()['cashflow_id']
            for index, linked_id in enumerate((cashflow_id,None), start=1):
                cur.execute("""INSERT INTO pams_investment_trade_fact
                    (trade_date,account_code,currency,trade_type,side,gross_amount,net_cash,cashflow_id,
                     source_table,source_id,source_hash)
                    VALUES (%s,'ibkr','USD','deposit','deposit',100,100,%s,'synthetic',%s,%s)""",
                    (day,linked_id,str(index),'trade-hash-'+str(index)))
            rows=_load_investment_account_rows(cur,start=day,end=day,base_currency='CNY')
            assert len(rows)==2
            assert {row['id'].split('-')[1] for row in rows} == {'inflow','trade'}
            assert all(row['amount_cny'] is None for row in rows)


def test_investment_funding_requires_published_matching_source(empty_source_db):
    from uuid import uuid4
    from psycopg2.extras import RealDictCursor
    from domain.ledger.ledger_view import _load_investment_trade_account_rows
    day=date(2026,10,22)
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            for index, state in enumerate(('published','draft','excluded','cross','ignored'), start=1):
                batch,item=str(uuid4()),str(uuid4())
                account='tonghuashun' if state=='cross' else 'ibkr'
                cur.execute("""INSERT INTO source_batches(batch_id,account_code,business_type,institution,acquisition_method)
                               VALUES (%s,%s,'trade','synthetic','manual')""",(batch,account))
                cur.execute("""INSERT INTO source_items(source_item_id,batch_id,source_uid,parse_method,record_hash,review_status)
                               VALUES (%s,%s,%s,'manual',%s,%s)""",(item,batch,str(index),'a'*64,'excluded' if state=='excluded' else 'confirmed'))
                cur.execute("""INSERT INTO pams_investment_trade_fact(trade_date,account_code,currency,trade_type,side,
                  gross_amount,net_cash,source_table,source_id,source_hash,source_item_id,review_status)
                  VALUES (%s,'ibkr','CNY','deposit','deposit',100,100,'source_items',%s,%s,%s,%s)""",
                  (day,str(index),'hash-'+str(index),item,'ignored' if state=='ignored' else 'confirmed'))
                if state!='draft':
                    cur.execute("UPDATE source_batches SET status='published',published_source_hash=%s WHERE batch_id=%s",('f'*64,batch))
            cur.execute("""INSERT INTO pams_investment_trade_fact(trade_date,account_code,currency,trade_type,side,
              gross_amount,net_cash,source_table,source_id,source_hash) VALUES
              (%s,'ibkr','CNY','deposit','deposit',50,50,'legacy','legacy','legacy-hash')""",(day,))
            rows=_load_investment_trade_account_rows(cur,start=day,end=day,base_currency='CNY')
            assert {row['source_id'] for row in rows} == {'1','2','4','legacy'}
            for row in rows:
                if row['source_id'] != 'legacy':
                    assert row['source_evidence_status'] in {'unknown','inconsistent'}
                    assert row['amount_cny'] is None


def test_investment_funding_direction_conflict_does_not_reverse_cash(empty_source_db):
    from psycopg2.extras import RealDictCursor
    from domain.ledger.ledger_view import _load_investment_trade_account_rows, _account_summary, _serialize_row
    day=date(2026,10,22)
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""INSERT INTO pams_investment_trade_fact(trade_date,account_code,currency,trade_type,side,
                gross_amount,net_cash,source_table,source_id,source_hash)
                VALUES (%s,'ibkr','CNY','deposit','deposit',100,-100,'legacy','conflict','conflict-hash')""",(day,))
            rows=_load_investment_trade_account_rows(cur,start=day,end=day,base_currency='CNY')
            assert len(rows)==1 and rows[0]['amount']==-100 and rows[0]['amount_cny'] is None
            assert _serialize_row(rows[0])['source_issue']=='direction_conflict'
            summary=_account_summary(rows,{'ibkr'})[0]
            assert summary['investment_inflow'] is None and summary['source_status']=='inconsistent'
            assert summary['direction_conflict_count']==1 and summary['pending_count']==1


def test_ledger_foreign_expense_keeps_native_value_and_blocks_period_totals(empty_source_db, monkeypatch):
    from psycopg2.extras import RealDictCursor
    from domain.ledger import ledger_view
    day=date(2026,10,22)
    monkeypatch.setattr(ledger_view, '_load_accounts_config', lambda: {'accounts': {'test_bank': {'account_type':'funding'}}})
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("INSERT INTO pams_accounts(account_code,display_name) VALUES ('test_bank','Synthetic bank') ON CONFLICT DO NOTHING")
            cur.execute("""INSERT INTO pams_cashflow_fact(event_date,account_code,amount,currency,fx_rate_to_cny,amount_cny,
                           cashflow_type,source_table,source_id,source_hash)
                           VALUES (%s,'test_bank',-100,'USD',1,-100,'operating_expense','synthetic','expense','expense-hash')""",(day,))
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',8,'synthetic')",(day+timedelta(days=1),))
            payload=ledger_view.build_ledger_workbench_payload(cur,month='2026-10')
            assert payload['periods']['month']['expense'] is None
            assert payload['periods']['month']['fx_status']=='incomplete'
            row=next(row for row in payload['recent_rows'] if row['account_code']=='test_bank')
            assert row['amount']==-100 and row['amount_cny'] is None
            assert row['recorded_amount_cny']==-100 and row['recorded_fx_status']=='unverified' and row['fx_basis']=='unavailable'
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'synthetic')",(day,))
            payload=ledger_view.build_ledger_workbench_payload(cur,month='2026-10')
            assert payload['periods']['month']['expense']==700
            assert payload['periods']['month']['fx_status']=='estimated'


def test_empty_and_fact_rows_keep_amounts_and_require_trade_day_fx(empty_source_db, monkeypatch):
    day = date(2026, 10, 22)
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
    monkeypatch.setattr(endpoint, 'get_conn', lambda: psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(endpoint, '_load_accounts_config', lambda: {})
    app = FastAPI()
    app.include_router(endpoint.router, prefix='/api')
    with TestClient(app) as web:
        empty = web.get('/api/investment-transactions')
        assert empty.status_code == 200, empty.text
        assert empty.json()['rows'] == []
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO pams_investment_trade_fact
                (trade_date,account_code,symbol,currency,trade_type,side,quantity,price,gross_amount,
                 fee_amount,tax_amount,net_cash,fx_rate_to_cny,net_cash_cny,source_table,source_id,source_hash)
                VALUES (%s,'ibkr','USD1','USD','buy','buy',10,10,100,2,0,-102,1,-102,'synthetic','1','hash1'),
                       (%s,'ibkr','CNY1','CNY','sell','sell',1,50,50,1,0,49,1,49,'synthetic','2','hash2')""", (day, day))
            cur.execute("""INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate)
                           VALUES (%s,'CNY','USD',6),(%s,'CNY','USD',8)""", (day-timedelta(days=1), day+timedelta(days=1)))
        response = web.get('/api/investment-transactions')
        assert response.status_code == 200, response.text
        data = response.json()
        rows = {row['symbol']: row for row in data['rows']}
        usd = rows['USD1']
        assert float(usd['gross_amount']) == 100 and float(usd['net_cash']) == -102
        assert usd['fx_rate'] is None and usd['gross_amount_cny'] is None and usd['net_cash_cny'] is None and usd['fees_cny'] is None
        assert data['summary']['buy_amount_cny'] is None and data['summary']['buy_amount_personal_cny'] is None
        assert float(data['summary']['sell_amount_cny']) == 50 and data['summary']['fees_cny'] is None
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'')",(day,))
        data = web.get('/api/investment-transactions').json()
        usd = next(row for row in data['rows'] if row['symbol'] == 'USD1')
        assert usd['fx_rate'] is None and usd['gross_amount_cny'] is None
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
            cur.execute("UPDATE pams_fx_rates_daily SET source='synthetic' WHERE quote_currency='USD' AND as_of=%s",(day,))
        data = web.get('/api/investment-transactions').json()
        usd = next(row for row in data['rows'] if row['symbol'] == 'USD1')
        assert float(usd['gross_amount_cny']) == 700 and float(usd['net_cash_cny']) == -714 and float(usd['fees_cny']) == 14
        assert float(data['summary']['buy_amount_cny']) == 700 and float(data['summary']['fees_cny']) == 15


def test_zero_trade_amounts_remain_zero_without_fx(empty_source_db,monkeypatch):
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO pams_investment_trade_fact
                (trade_date,account_code,symbol,currency,trade_type,side,quantity,price,gross_amount,
                 fee_amount,tax_amount,net_cash,source_table,source_id,source_hash)
                VALUES ('2026-10-22','ibkr','ZERO','JPY','buy','buy',0,0,0,0,0,0,'synthetic','zero','zero-hash')""")
    monkeypatch.setattr(endpoint,'get_conn',lambda:psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(endpoint,'_load_accounts_config',lambda:{})
    app=FastAPI(); app.include_router(endpoint.router,prefix='/api')
    with TestClient(app) as web:
        response=web.get('/api/investment-transactions')
        assert response.status_code==200,response.text
        row=response.json()['rows'][0]
        assert row['fx_rate'] is None
        assert Decimal(str(row['gross_amount_cny']))==0
        assert Decimal(str(row['net_cash_cny']))==0
        assert Decimal(str(row['fees_cny']))==0
