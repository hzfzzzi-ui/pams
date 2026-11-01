"""Empty isolated database, Flex cash report, and real read-only ledger API."""
# ruff: noqa: F811
from datetime import date,datetime,timezone
from hashlib import sha256

import psycopg2
from decimal import Decimal
from psycopg2.extras import RealDictCursor
import pytest
from fastapi.testclient import TestClient
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from tests.brokers.ibkr.test_ibkr_sync_transaction import XML
from scripts.bootstrap_isolated_sources import initialize_isolated_sources


@pytest.mark.parametrize('supplement',[False,True],ids=['report_cash','supplement_cash'])
@pytest.mark.parametrize('provider',[None,''],ids=['null','empty'])
def test_cash_fx_rate_without_provider_is_unknown_in_ledger_http(empty_source_db,monkeypatch,supplement,provider):
    import main
    from endpoints import ledger
    from collectors.ibkr_flex import parse_flex_report
    from domain.brokers.ibkr.ibkr_cash_events import import_cash_events
    from domain.brokers.ibkr.ibkr_report_batch import publish_report_batch
    from tests.database.test_ibkr_fact_source_verification import cash_report
    day=date(2026,10,22)
    payload=cash_report().replace(b'type="Dividends"',b'type="Deposits &amp; Withdrawals"')
    digest=sha256(payload).hexdigest()
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'synthetic')",(day,))
            if not supplement:
                cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','EUR',8,'synthetic')",(day,))
        conn.commit()
        parsed=parse_flex_report(payload.decode(),account_code='ibkr')
        envelope=parsed.cash_events[0]
        envelope['source_evidence']={'broker_source':'ibkr_flex','source_sha256':digest,'confirmed_day':day.isoformat()}
        if supplement:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                publish_report_batch(cur,report_bytes=payload,source_account_id='U_TEST',day=day,report_sha256=digest,cash_pending_count=1)
            conn.commit()
            with conn.cursor() as cur:
                cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','EUR',8,'synthetic')",(day,))
        events=import_cash_events(conn,[envelope],account_code='ibkr',cutoff=date(2026,1,1),statement_as_of=day,defer_source_link=True)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            publish_report_batch(cur,report_bytes=payload,source_account_id='U_TEST',day=day,report_sha256=digest,cash_entries=events['_deferred_source_events'])
            # Simulate an existing fact whose previously available provider is later lost.
            cur.execute("UPDATE pams_fx_rates_daily SET source=%s WHERE quote_currency='EUR' AND as_of=%s",(provider,day))
    monkeypatch.setattr(ledger,'get_conn',lambda:psycopg2.connect(**empty_source_db))
    with TestClient(main.app) as web:
        response=web.get('/api/ledger/workbench',params={'month':'2026-10'})
        assert response.status_code==200,response.text
        rows=[r for r in response.json()['recent_rows_month'] if r['account_code']=='ibkr']
        assert len(rows)==1 and rows[0]['amount']==10 and rows[0]['amount_cny'] is None
        assert rows[0]['source_evidence_status']=='unknown' and rows[0]['needs_review']


def test_daily_cross_rates_require_each_leg_on_event_day(empty_source_db):
    from domain.ledger.ledger_view import _daily_fx_join
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES ('2026-10-22','CNY','USD',7,'test'),('2026-10-22','CNY','EUR',8,'test'),('2026-10-23','CNY','GBP',9,'test')")
            join=_daily_fx_join(cur,currency_sql='v.currency',date_sql='v.day')
            # Use named parameters for the join, and never borrow a future rate.
            def converted(currency,base):
                cur.execute("SELECT fx.rate FROM (SELECT %(currency)s::text AS currency, DATE '2026-10-22' AS day) v "+join,
                            {'currency':currency,'base':base})
                return cur.fetchone()['rate']
            assert converted('USD','CNY')==Decimal('7')
            assert converted('CNY','USD').quantize(Decimal('0.00000001'))==Decimal('0.14285714')
            assert converted('EUR','USD').quantize(Decimal('0.00000001'))==Decimal('1.14285714')
            assert converted('GBP','USD') is None
            assert converted('USD','GBP') is None
            assert converted('GBP','GBP')==1
            assert converted('','CNY') is None
            cur.execute("UPDATE pams_fx_rates_daily SET source='' WHERE as_of='2026-10-22' AND quote_currency='EUR'")
            assert converted('EUR','USD') is None
            assert converted('USD','EUR') is None


def test_ibkr_pending_fx_replay_tamper_and_base_currency_in_ledger(empty_source_db,monkeypatch,tmp_path):
    import main
    from collectors import ibkr_flex
    from domain.brokers.ibkr import ibkr_report_state
    from domain.ledger import ledger_view
    from endpoints import ibkr,ledger
    day=date(2026,10,22)
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'test')",(day,))
    monkeypatch.setenv('PAMS_HOLDING_EVIDENCE_ISOLATED','true')
    monkeypatch.setenv('PAMS_IBKR_CASH_EVENTS_AFTER','2026-10-21')
    monkeypatch.setenv('PAMS_DATA_DIR',str(tmp_path))
    for module in (main,ibkr,ledger): monkeypatch.setattr(module,'get_conn',lambda:psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(ibkr,'_credentials',lambda:('token','query'))
    monkeypatch.setattr(ibkr_flex,'request_flex_reference',lambda *a,**k:{'reference_code':'TEST'})
    monkeypatch.setattr(ibkr_report_state,'utc_now',lambda:datetime(2026,10,22,22,tzinfo=timezone.utc))
    monkeypatch.setattr(ledger_view,'_load_accounts_config',lambda:{'accounts':{
        'ibkr':{'account_type':'brokerage'},'ccb_7332':{'account_type':'funding'},
        'tonghuashun':{'account_type':'brokerage','personal_capital':250,'capital_base':400,'owner_ratio':0.625}}})
    raw=XML.replace('ibCommission="-1.25"','ibCommission="-1.25" ibCommissionCurrency="USD" taxAmount="0"')
    cash='<CashTransactions><CashTransaction accountId="U_TEST" transactionID="CASH1" currency="EUR" dateTime="20261022;120000" amount="10" type="Deposits &amp; Withdrawals" /></CashTransactions>'
    raw=raw.replace('</FlexStatement>',cash+'</FlexStatement>')
    monkeypatch.setattr(ibkr_flex,'fetch_flex_statement',lambda *a,**k:raw)
    def rows(web,base='CNY'):
        response=web.get('/api/ledger/workbench',params={'month':'2026-10','baseCurrency':base})
        assert response.status_code==200,response.text
        return [row for row in response.json()['recent_rows_month'] if row['account_code']=='ibkr']
    with TestClient(main.app) as web:
        assert web.post('/api/ibkr/flex/sync',json={}).status_code==200
        assert rows(web)==[]
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','EUR',8,'test')",(day,))
        result=web.post('/api/ibkr/flex/sync',json={}); assert result.status_code==200,result.text
        cash_rows=rows(web); assert len(cash_rows)==1
        assert cash_rows[0]['amount']==10 and cash_rows[0]['amount_cny']==80
        assert cash_rows[0]['source_evidence_status']=='verified'
        assert cash_rows[0]['source_authority_verified'] is False
        with monkeypatch.context() as disabled:
            disabled.setenv('PAMS_UNIFIED_SOURCE_ISOLATED','false')
            still_readable=rows(web)[0]
            assert still_readable['source_evidence_status']=='verified' and still_readable['amount_cny']==80
            assert web.post('/api/ibkr/flex/sync',json={}).status_code>=400
        usd=rows(web,'USD')[0]
        assert usd['amount_base']==11.43 and usd['amount_cny'] is None and usd['base_currency']=='USD'
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            cur.execute("DELETE FROM pams_fx_rates_daily WHERE base_currency='CNY' AND quote_currency='USD' AND as_of=%s",(day,))
        missing_base=rows(web,'USD')[0]
        assert missing_base['amount_base'] is None and missing_base['amount_cny'] is None and missing_base['needs_review']
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'test')",(day,))
        assert web.post('/api/ibkr/flex/sync',json={}).status_code==200
        assert len(rows(web))==1
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            cur.execute("INSERT INTO pams_accounts(account_code,display_name) VALUES ('ccb_7332','Synthetic bank') ON CONFLICT DO NOTHING")
            cur.execute("INSERT INTO pams_accounts(account_code,display_name) VALUES ('tonghuashun','Synthetic investment') ON CONFLICT DO NOTHING")
            cur.execute("""INSERT INTO pams_cashflow_fact(event_date,account_code,counter_account_code,amount,currency,
                fx_rate_to_cny,amount_cny,cashflow_type,source_table,source_id,source_hash)
                VALUES (%s,'ccb_7332','ibkr',-10,'EUR',8,-80,'investment_funding','synthetic_bank','one',%s)""",(day,'b'*64))
            cur.execute("""INSERT INTO pams_cashflow_fact(event_date,account_code,amount,currency,fx_rate_to_cny,amount_cny,
                cashflow_type,category,source_table,source_id,source_hash)
                VALUES (%s,'ccb_7332',-10,'EUR',8,-80,'operating_expense','food','synthetic_bank','expense',%s)""",(day,'c'*64))
            cur.execute("""INSERT INTO pams_cashflow_fact(event_date,account_code,counter_account_code,amount,currency,
                fx_rate_to_cny,amount_cny,cashflow_type,source_table,source_id,source_hash)
                VALUES (%s,'ccb_7332','tonghuashun',-10,'EUR',8,-80,'investment_funding','synthetic_bank','two',%s)""",(day,'d'*64))

        independent=rows(web)
        assert len(independent)==2 and sum(row.get('source_evidence_role')=='ibkr_report_cash' for row in independent)==1
        response=web.get('/api/ledger/workbench',params={'month':'2026-10','baseCurrency':'USD'}); assert response.status_code==200
        payload=response.json()
        assert payload['periods']['month']['expense']==11.43
        assert payload['expense_categories'][0]['amount']==11.43
        assert payload['monthly_trend'][-1]['expense']==11.43
        assert payload['transfer_summary']==[] and payload['pending_transfer_coverage']=='partial'
        assert payload['pending_transfer_count'] == 0 and payload['pending_transfer_count_status'] == 'known_subset'
        bank=next(item for item in payload['account_summary_ytd'] if item['account_code']=='ccb_7332')
        assert bank['expense']==11.43
        capital=next(item for item in payload['account_summary_ytd'] if item['account_code']=='tonghuashun')
        assert capital['personal_capital'] is None and capital['investment_inflow'] is None and capital['capital_status']=='unavailable'
        assert all(row['amount_cny'] is None for row in payload['recent_rows_month'])
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            cur.execute("DELETE FROM pams_cashflow_fact WHERE source_table='synthetic_bank'")
            cur.execute("SELECT source_item_id FROM pams_investment_trade_fact WHERE source_table='ibkr_flex_cash_transaction'")
            item=cur.fetchone()[0]
            cur.execute('ALTER TABLE pams_investment_trade_fact DISABLE TRIGGER USER')
            cur.execute("UPDATE pams_investment_trade_fact SET source_item_id=NULL WHERE source_table='ibkr_flex_cash_transaction'")
            cur.execute('ALTER TABLE pams_investment_trade_fact ENABLE TRIGGER USER')
        missing_source=rows(web)[0]
        assert missing_source['source_evidence_status']=='unknown' and missing_source['amount_cny'] is None
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            cur.execute('ALTER TABLE pams_investment_trade_fact DISABLE TRIGGER USER')
            cur.execute("UPDATE pams_investment_trade_fact SET source_item_id=%s WHERE source_table='ibkr_flex_cash_transaction'",(item,))
            cur.execute('ALTER TABLE pams_investment_trade_fact ENABLE TRIGGER USER')
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            cur.execute("UPDATE pams_investment_trade_fact SET net_cash=11 WHERE source_table='ibkr_flex_cash_transaction'")
        changed=rows(web)[0]
        assert changed['source_evidence_status']=='unknown' and changed['amount_cny'] is None and changed['needs_review']
