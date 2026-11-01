"""Daily cash and full reports share an original without fabricating links."""
# ruff: noqa: F811
from datetime import date,datetime,timezone
from hashlib import sha256
import re

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor
from psycopg2.extensions import make_dsn
from fastapi.testclient import TestClient
from scripts.bootstrap_isolated_sources import initialize_isolated_sources
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from tests.brokers.ibkr.test_ibkr_sync_transaction import XML


def test_distinct_reports_do_not_reassign_cash_and_conflicting_original_is_pending(empty_source_db):
    from collectors.ibkr_flex import parse_flex_report
    from domain.brokers.ibkr.ibkr_cash_events import import_cash_events
    from domain.brokers.ibkr.ibkr_report_batch import publish_report_batch
    day=date(2026,10,22)
    raw=('<FlexQueryResponse><FlexStatements><FlexStatement accountId="U_TEST" toDate="20261022">'
         '<Trades/><CashTransactions><CashTransaction accountId="U_TEST" transactionID="ONLY" '
         'currency="CNY" dateTime="20261022;120000" amount="10" type="Deposits &amp; Withdrawals"/>'
         '</CashTransactions></FlexStatement></FlexStatements></FlexQueryResponse>')
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        def run(text):
            digest=sha256(text.encode()).hexdigest()
            parsed=parse_flex_report(text,account_code='ibkr',allow_empty_holdings=True)
            event=parsed.cash_events[0]
            event['source_evidence']={'source_sha256':digest,'confirmed_day':day.isoformat()}
            outcome=import_cash_events(conn,[event],account_code='ibkr',cutoff=date(2026,1,1),statement_as_of=day,defer_source_link=True)
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                result=publish_report_batch(cur,report_bytes=text.encode(),source_account_id='U_TEST',day=day,
                    report_sha256=digest,cash_entries=outcome['_deferred_source_events'],cash_only=True)
            conn.commit()
            return outcome,result
        first,first_batch=run(raw)
        assert first['inserted_count']==1
        with conn.cursor() as cur:
            cur.execute("SELECT trade_id,source_item_id FROM pams_investment_trade_fact WHERE source_table='ibkr_flex_cash_transaction'")
            original=cur.fetchone()
        second,second_batch=run(raw+'\n')
        assert second['duplicate_count']==1 and second_batch['source_contributions']=='not_recorded'
        with conn.cursor() as cur:
            cur.execute('SELECT trade_id,source_item_id FROM pams_investment_trade_fact')
            assert cur.fetchone()==original
            cur.execute("SELECT count(*) FROM source_items WHERE parsed_fields->>'observation'='matched_existing_unlinked'")
            assert cur.fetchone()[0]==1
        conflicting,_=run(raw.replace('amount="10"','amount="11"'))
        assert conflicting['inserted_count']==0 and any(p['reason']=='conflicting_cash_transaction' for p in conflicting['pending'])
        with conn.cursor() as cur:
            cur.execute('SELECT count(*),max(net_cash) FROM pams_investment_trade_fact')
            assert cur.fetchone()==(1,10)
            cur.execute('SELECT count(*) FROM source_files'); assert cur.fetchone()[0]==3
        assert first_batch['batch_id']!=second_batch['batch_id']


@pytest.mark.parametrize('legacy_table_exists',[False,True],ids=['new_schema','legacy_table_ignored'])
@pytest.mark.parametrize('trade_section',[True,False],ids=['empty_trade_section','missing_trade_section'])
def test_daily_cash_report_without_holdings_has_its_own_valid_original(empty_source_db,monkeypatch,trade_section,legacy_table_exists):
    import main
    from domain.brokers.ibkr import ibkr_daily_close_read as daily
    from endpoints import ibkr,ledger
    day=date(2026,10,22)
    raw=('<FlexQueryResponse><FlexStatements><FlexStatement accountId="U_TEST" toDate="20261022">'
         + ('<Trades/>' if trade_section else '') + '<CashTransactions><CashTransaction accountId="U_TEST" transactionID="ONLY" '
         'currency="CNY" dateTime="20261022;120000" amount="10" type="Deposits &amp; Withdrawals"/>'
         '</CashTransactions></FlexStatement></FlexStatements></FlexQueryResponse>')
    digest=sha256(raw.encode()).hexdigest()
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        if legacy_table_exists:
            with conn.cursor() as cur:
                # The obsolete table must neither choose the old writer nor supply facts.
                cur.execute('CREATE TABLE pams_ibkr_trades (ignored boolean)')
    monkeypatch.setenv('PAMS_IBKR_CASH_EVENTS_AFTER','2026-10-21')
    for module in (ibkr,ledger): monkeypatch.setattr(module,'get_conn',lambda:psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(daily,'load_evidence',lambda:{'status':'ok','account_id':'U_TEST','as_of':day,'sha256':digest,'xml_text':raw,'source_file':'synthetic'})
    monkeypatch.setattr(daily,'read_series',lambda **kwargs:{'source_sha256':digest,'daily_closes':{day.isoformat():{'status':'ok','source_sha256':digest,'account_id':'U_TEST','as_of':day.isoformat()}}})
    with TestClient(main.app,base_url='http://pams.local') as web:
        response=web.post('/api/ibkr/cash-events/sync',json={})
        assert response.status_code==200,response.text
        assert response.json()['facts_written']==1 and response.json()['days'][0]['complete']==trade_section
        ledger_response=web.get('/api/ledger/workbench',params={'month':'2026-10'})
        rows=[r for r in ledger_response.json()['recent_rows_month'] if r['account_code']=='ibkr']
        assert len(rows)==1 and rows[0]['amount_cny']==10 and rows[0]['source_evidence_status']=='verified'
    with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
        cur.execute('SELECT content FROM source_files'); assert bytes(cur.fetchone()[0])==raw.encode()
        cur.execute('SELECT count(*) FROM pams_holdings_snapshot'); assert cur.fetchone()[0]==0


@pytest.mark.parametrize('daily_first',[True,False],ids=['cash_then_report','report_then_cash'])
@pytest.mark.parametrize('late_fx',[False,True],ids=['known_fx','late_fx'])
def test_daily_cash_and_full_report_share_original_and_keep_fact_source(empty_source_db,monkeypatch,tmp_path,daily_first,late_fx):
    import main
    from collectors import ibkr_flex
    from domain.brokers.ibkr import ibkr_daily_close_read as daily,ibkr_report_state
    from domain.brokers.ibkr.ibkr_fact_source_verification import verify_ibkr_fact
    from endpoints import ibkr,ledger
    day=date(2026,10,22)
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'synthetic')",(day,))
    monkeypatch.setenv('PAMS_HOLDING_EVIDENCE_ISOLATED','true')
    monkeypatch.setenv('PAMS_IBKR_CASH_EVENTS_AFTER','2026-10-21')
    monkeypatch.setenv('PAMS_DATA_DIR',str(tmp_path))
    for module in (main,ibkr,ledger): monkeypatch.setattr(module,'get_conn',lambda:psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(ibkr,'_credentials',lambda:('token','query'))
    monkeypatch.setattr(ibkr_flex,'request_flex_reference',lambda *a,**k:{'reference_code':'TEST'})
    monkeypatch.setattr(ibkr_report_state,'utc_now',lambda:datetime(2026,10,22,22,tzinfo=timezone.utc))
    raw=XML.replace('ibCommission="-1.25"','ibCommission="-1.25" ibCommissionCurrency="USD" taxAmount="0"')
    trade=re.search(r'<Trade\b[^>]*/>',raw).group()
    raw=raw.replace(trade,'<Trades>'+trade+'</Trades>')
    currency='EUR' if late_fx else 'USD'
    raw=raw.replace('</FlexStatement>',f'<CashTransactions><CashTransaction accountId="U_TEST" transactionID="CASH1" currency="{currency}" dateTime="20261022;120000" amount="10" type="Deposits &amp; Withdrawals" /></CashTransactions></FlexStatement>')
    digest=sha256(raw.encode()).hexdigest()
    monkeypatch.setattr(ibkr_flex,'fetch_flex_statement',lambda *a,**k:raw)
    monkeypatch.setattr(daily,'load_evidence',lambda:{'status':'ok','account_id':'U_TEST','as_of':day,'sha256':digest,'xml_text':raw,'source_file':'synthetic'})
    monkeypatch.setattr(daily,'read_series',lambda **kwargs:{'source_sha256':digest,'daily_closes':{day.isoformat():{'status':'ok','source_sha256':digest,'account_id':'U_TEST','as_of':day.isoformat()}}})
    with TestClient(main.app,base_url='http://pams.local') as web:
        def call(path):
            response=web.post(path,json={})
            assert response.status_code==200,response.text
            return response.json()
        if daily_first:
            if not late_fx:
                from domain.brokers.ibkr import ibkr_report_batch
                with monkeypatch.context() as failed:
                    def reject(*args,**kwargs): raise RuntimeError('synthetic source failure')
                    failed.setattr(ibkr_report_batch,'publish_report_batch',reject)
                    assert web.post('/api/ibkr/cash-events/sync',json={}).status_code==503
                with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
                    for table in ('source_batches','pams_investment_trade_fact'):
                        cur.execute('SELECT count(*) FROM '+table); assert cur.fetchone()[0]==0
                from concurrent.futures import ThreadPoolExecutor
                from threading import Barrier
                barrier=Barrier(2)
                def run(_):
                    with TestClient(main.app) as worker:
                        barrier.wait(timeout=10)
                        response=worker.post('/api/ibkr/cash-events/sync',json={})
                        assert response.status_code==200,response.text
                        return response.json()
                with ThreadPoolExecutor(max_workers=2) as pool:
                    outcomes=list(pool.map(run,range(2)))
                assert sum(item['facts_written'] for item in outcomes)==1
                first=next(item for item in outcomes if item.get('days'))
            else:
                first=call('/api/ibkr/cash-events/sync')
            assert '_source_xml' not in first and 'xml_text' not in str(first)
            assert first['facts_written']==(0 if late_fx else 1) and not first['days'][0]['complete']
            with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM pams_investment_trade_fact WHERE source_table='ibkr_flex_trade'")
                assert cur.fetchone()[0]==0
            call('/api/ibkr/flex/sync')
        else:
            call('/api/ibkr/flex/sync')
            assert call('/api/ibkr/cash-events/sync')['facts_written']==0
        if late_fx:
            with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
                cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','EUR',8,'synthetic')",(day,))
            assert call('/api/ibkr/cash-events/sync')['facts_written']==1
        else:
            assert call('/api/ibkr/cash-events/sync')['facts_written']==0
        with monkeypatch.context() as disabled:
            disabled.setenv('PAMS_UNIFIED_SOURCE_ISOLATED','false')
            assert web.post('/api/ibkr/cash-events/sync',json={}).status_code>=400
        assert call('/api/ibkr/flex/sync')['success']
        monkeypatch.setenv('PAMS_HOLDING_REVIEW_TEST_DSN',make_dsn(**empty_source_db))
        staged=web.post('/api/ibkr/holding-review/stage',headers={'origin':'http://pams.local','x-pams-isolated-review':'1'},
                        json={'source_account_id':'U_TEST','business_date':day.isoformat(),'xml_text':raw})
        assert staged.status_code==200,staged.text
        assert staged.json()['origin_reused']
        response=web.get('/api/ledger/workbench',params={'month':'2026-10'})
        assert response.status_code==200,response.text
        rows=[r for r in response.json()['recent_rows_month'] if r['account_code']=='ibkr']
        assert len(rows)==1 and rows[0]['source_evidence_status']=='verified' and rows[0]['amount_cny']==(80 if late_fx else 70)
        assert not list(tmp_path.rglob('*.xml'))
    from domain.brokers.ibkr.ibkr_report_batch import _existing_cash_proven
    with psycopg2.connect(**empty_source_db) as conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM pams_investment_trade_fact WHERE source_table='ibkr_flex_cash_transaction'")
        existing=cur.fetchone()
        cur.execute('SELECT raw_fields FROM source_items WHERE source_item_id=%s',(existing['source_item_id'],))
        original=cur.fetchone()['raw_fields']
        altered=dict(original['official_attributes'],description='different official row')
        with pytest.raises(ValueError,match='original row differs'):
            _existing_cash_proven(cur,existing,{'source_account_id':'U_TEST','raw_json':altered})
    with psycopg2.connect(**empty_source_db) as conn:
        conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT count(*) AS n FROM source_files'); assert cur.fetchone()['n']==1
            cur.execute("SELECT * FROM pams_investment_trade_fact WHERE source_table='ibkr_flex_cash_transaction'")
            fact=cur.fetchone(); assert fact and verify_ibkr_fact(cur,trade_id=fact['trade_id'])['status'] in {'partial','consistent'}
            cur.execute("SELECT count(*) AS n FROM source_items WHERE parsed_fields->>'observation'='matched_existing_unlinked'")
            assert cur.fetchone()['n']>=1


def test_cash_source_identifies_the_correct_statement_in_a_multi_day_report(empty_source_db):
    from collectors.ibkr_flex import parse_flex_report
    from domain.brokers.ibkr.ibkr_cash_events import import_cash_events
    from domain.brokers.ibkr.ibkr_report_batch import publish_report_batch
    from domain.brokers.ibkr.ibkr_fact_source_verification import verify_ibkr_fact
    day=date(2026,10,22)
    raw=(' <FlexQueryResponse><FlexStatements>'
         '<FlexStatement accountId="U_TEST" toDate="20261021"><CashTransactions>'
         '<CashTransaction accountId="U_TEST" transactionID="EARLIER" currency="CNY" '
         'dateTime="20261021;120000" amount="10" type="Deposits &amp; Withdrawals"/>'
         '</CashTransactions></FlexStatement>'
         '<FlexStatement accountId="U_TEST" toDate="20261022"><CashTransactions>'
         '<CashTransaction accountId="U_TEST" transactionID="CURRENT" currency="CNY" '
         'dateTime="20261022;120000" amount="20" type="Deposits &amp; Withdrawals"/>'
         '</CashTransactions></FlexStatement></FlexStatements></FlexQueryResponse>')
    digest=sha256(raw.encode()).hexdigest()
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        parsed=parse_flex_report(raw,account_code='ibkr',allow_empty_holdings=True)
        for event in parsed.cash_events:
            event['source_evidence']={'source_sha256':digest,'confirmed_day':day.isoformat()}
        outcome=import_cash_events(conn,parsed.cash_events,account_code='ibkr',cutoff=date(2026,1,1),statement_as_of=day,defer_source_link=True)
        assert outcome['inserted_count']==2
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            publish_report_batch(cur,report_bytes=raw.encode(),source_account_id='U_TEST',day=day,
                report_sha256=digest,cash_entries=outcome['_deferred_source_events'],cash_only=True)
        conn.commit()
        conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT trade_id FROM pams_investment_trade_fact WHERE source_table='ibkr_flex_cash_transaction'")
            ids=[row['trade_id'] for row in cur.fetchall()]
            assert len(ids)==2
            for trade_id in ids:
                result=verify_ibkr_fact(cur,trade_id=trade_id)
                assert result['status'] in {'partial','consistent'},result
