"""Full empty bootstrap to actual Flex and asset HTTP APIs in isolated PG."""
# ruff: noqa: F811
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import re

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor
from fastapi.testclient import TestClient

from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from tests.brokers.ibkr.test_ibkr_sync_transaction import XML
from scripts import bootstrap_isolated_sources as bootstrap


def test_empty_bootstrap_default_ibkr_sync_asset_apis_replay_and_rollback(empty_source_db, monkeypatch, tmp_path):
    import main
    from collectors import ibkr_flex
    from domain.brokers.ibkr import ibkr_daily_close_read, ibkr_report_state
    from endpoints import ibkr, investment_transactions

    day = date(2026, 10, 22)
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('pams_ibkr_trades'), to_regclass('pams_ibkr_position_history')")
            assert cur.fetchone() == (None, None)
            cur.execute("INSERT INTO pams_fx_rates_daily (as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'synthetic')", (day,))
    monkeypatch.setenv('PAMS_HOLDING_EVIDENCE_ISOLATED', 'true')
    monkeypatch.delenv('PAMS_IBKR_CASH_EVENTS_AFTER', raising=False)
    monkeypatch.setenv('PAMS_DATA_DIR', str(tmp_path))
    for module in (main, ibkr, investment_transactions):
        monkeypatch.setattr(module, 'get_conn', lambda: psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(ibkr, '_credentials', lambda: ('synthetic-token','synthetic-query'))
    monkeypatch.setattr(ibkr_flex, 'request_flex_reference', lambda *a, **k: {'reference_code':'TEST'})
    monkeypatch.setattr(ibkr_report_state, 'utc_now', lambda: datetime(2026, 10, 22, 22, tzinfo=timezone.utc))
    report = {'xml': XML.replace('ibCommission="-1.25"', 'ibCommission="-1.25" ibCommissionCurrency="USD" taxAmount="0"')}
    monkeypatch.setattr(ibkr_flex, 'fetch_flex_statement', lambda *a, **k: report['xml'])
    monkeypatch.setattr(main, '_holdings_market_view_as_of', lambda cur: day)
    monkeypatch.setattr(main, '_view_as_of', lambda: day)
    monkeypatch.setattr(main, '_daily_pnl_view_as_of', lambda: day)
    monkeypatch.setattr(main, '_excluded_holding_accounts', lambda: set())
    monkeypatch.setattr(main, '_account_owner_ratios', lambda: {})
    monkeypatch.setattr(main, '_default_owner_ratio', lambda: Decimal('1'))
    monkeypatch.setattr(ibkr_daily_close_read, 'configured', lambda: False)

    with TestClient(main.app) as web:
        first = web.post('/api/ibkr/flex/sync', json={})
        assert first.status_code == 200, first.text
        assert first.json()['source_mode'] == 'unified'
        assert first.json()['sync_run']['publication']['report_trade_sources']['written'] == 1
        published_status = web.get('/api/ibkr/flex/status')
        assert published_status.status_code == 200, published_status.text
        published_latest = published_status.json()['latest']
        assert published_latest['source'] == 'ibkr_flex' and published_latest['asset_scope'] == 'ibkr'
        assert published_latest['status'] == 'unknown' and published_latest['error'] is None
        published_run_id = published_latest['id']
        broker_trades = web.get('/api/ibkr/trades')
        assert broker_trades.status_code == 200, broker_trades.text
        assert any(row['symbol'] == 'TEST' for row in broker_trades.json()['rows'])
        broker_history = web.get('/api/ibkr/position-history?symbol=TEST')
        assert broker_history.status_code == 200, broker_history.text
        assert broker_history.json()['data_status'] == 'recorded_only'
        assert any(row['symbol'] == 'TEST' for row in broker_history.json()['rows'])
        broker_reconcile = web.get('/api/ibkr/reconcile')
        assert broker_reconcile.status_code == 200, broker_reconcile.text
        assert 'TEST' in broker_reconcile.json()['matched_symbols']
        assert broker_reconcile.json()['position_history_status'] == 'unavailable'
        positions = web.get('/api/holdings/latest')
        assert positions.status_code == 200, positions.text
        assert float(next(row for row in positions.json()['rows'] if row['symbol'] == 'TEST')['market_value']) == 381
        trades = web.get('/api/investment-transactions?account_code=ibkr')
        assert trades.status_code == 200, trades.text
        assert any(row['symbol'] == 'TEST' for row in trades.json()['rows'])
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
            cur.execute('SELECT version_key FROM valuation_evidence ORDER BY version_key')
            versions_before=cur.fetchall()
            cur.execute('SELECT version_key,input_kind,evidence::text FROM valuation_evidence v CROSS JOIN LATERAL jsonb_to_recordset(v.inputs) AS x(input_kind text,input_hash text,evidence jsonb) ORDER BY version_key,input_kind,evidence::text')
            evidence_before=cur.fetchall()
        replay = web.post('/api/ibkr/flex/sync', json={})
        assert replay.status_code == 200, replay.text
        assert replay.json()['sync_run']['publication']['report_trade_sources']['replayed_verified'] == 1
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
            cur.execute('SELECT version_key FROM valuation_evidence ORDER BY version_key')
            assert cur.fetchall()==versions_before
            cur.execute('SELECT version_key,input_kind,evidence::text FROM valuation_evidence v CROSS JOIN LATERAL jsonb_to_recordset(v.inputs) AS x(input_kind text,input_hash text,evidence jsonb) ORDER BY version_key,input_kind,evidence::text')
            assert cur.fetchall()==evidence_before
        # The independent daily cash importer consumes exactly the report's
        # verified, published trade rather than a missing legacy raw table.
        from domain.brokers.ibkr import ibkr_daily_close_read as daily
        trade = re.search(r'<Trade\b[^>]*/>', report['xml']).group()
        daily_xml = report['xml'].replace(trade, '<Trades>' + trade + '</Trades>')
        cash = ('<CashTransactions><CashTransaction accountId="U_TEST" transactionID="CASH1" '
                'currency="USD" dateTime="20261022;120000" amount="10" type="Dividends" />'
                '</CashTransactions>')
        daily_xml = daily_xml.replace('</FlexStatement>', cash + '</FlexStatement>')
        digest = hashlib.sha256(daily_xml.encode()).hexdigest()
        monkeypatch.setenv('PAMS_IBKR_CASH_EVENTS_AFTER', '2026-10-21')
        monkeypatch.setattr(daily, 'load_evidence', lambda: {'status':'ok', 'account_id':'U_TEST',
            'as_of':day, 'sha256':digest, 'xml_text':daily_xml, 'source_file':'synthetic'})
        monkeypatch.setattr(daily, 'read_series', lambda **kwargs: {'source_sha256':digest, 'daily_closes':{
            day.isoformat(): {'status':'ok', 'source_sha256':digest, 'account_id':'U_TEST', 'as_of':day.isoformat()}}})
        cash_status = web.get('/api/ibkr/cash-events/status')
        assert cash_status.status_code == 200, cash_status.text
        cash_sync = web.post('/api/ibkr/cash-events/sync', json={})
        assert cash_sync.status_code == 200, cash_sync.text
        assert cash_sync.json()['facts_written'] == 1
        assert cash_sync.json()['days'][0]['complete'] is True
        cash_replay = web.post('/api/ibkr/cash-events/sync', json={})
        assert cash_replay.status_code == 200, cash_replay.text
        assert cash_replay.json()['facts_written'] == 0
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            with pytest.raises(psycopg2.errors.RaiseException):
                cur.execute("UPDATE broker_processing_state SET retryable_count=0 WHERE state_type='cash_progress'")
            conn.rollback()
            # A forged newest state must not suppress a real retry.
            cur.execute("""INSERT INTO broker_processing_state
                (state_type,account_hash,business_date,source_batch_id,report_sha256,statement_sha256,
                 trade_version,retryable_count,pending,state_sha256)
                SELECT state_type,account_hash,business_date,source_batch_id,report_sha256,statement_sha256,
                       trade_version,0,'[]'::jsonb,%s FROM broker_processing_state WHERE state_type='cash_progress'
                ORDER BY progress_id DESC LIMIT 1""",('0'*64,))
        pending_status=web.get('/api/ibkr/cash-events/status')
        assert pending_status.status_code==200 and pending_status.json()['queued_day_count']==1,pending_status.text
        repaired=web.post('/api/ibkr/cash-events/sync',json={})
        assert repaired.status_code==200 and repaired.json()['facts_written']==0,repaired.text
        assert web.get('/api/ibkr/cash-events/status').json()['queued_day_count']==0
        good_daily_xml = daily_xml
        daily_xml = good_daily_xml.replace('<Trades>', '').replace('</Trades>', '')
        digest = hashlib.sha256(daily_xml.encode()).hexdigest()
        missing_section = web.post('/api/ibkr/cash-events/sync', json={})
        assert missing_section.status_code == 200, missing_section.text
        assert missing_section.json()['days'][0]['complete'] is False
        assert any(item['reason'] == 'missing_or_ambiguous_trade_section'
                   for item in missing_section.json()['days'][0]['pending'])
        daily_xml = good_daily_xml.replace('tradeID="T1"', 'tradeID="NOT_PUBLISHED"')
        digest = hashlib.sha256(daily_xml.encode()).hexdigest()
        missing_fact = web.post('/api/ibkr/cash-events/sync', json={})
        assert missing_fact.status_code == 200, missing_fact.text
        assert missing_fact.json()['days'][0]['complete'] is False
        assert any(item['reason'] == 'unverified_trade_source'
                   for item in missing_fact.json()['days'][0]['pending'])
        daily_xml = good_daily_xml.replace(trade, '')
        digest = hashlib.sha256(daily_xml.encode()).hexdigest()
        missing_member = web.post('/api/ibkr/cash-events/sync', json={})
        assert missing_member.status_code == 200, missing_member.text
        assert missing_member.json()['days'][0]['complete'] is False
        assert any(item['reason'] == 'unverified_trade_source'
                   for item in missing_member.json()['days'][0]['pending'])
        daily_xml = good_daily_xml.replace(trade, trade.replace('accountId="U_TEST"', 'accountId="OTHER"'))
        digest = hashlib.sha256(daily_xml.encode()).hexdigest()
        wrong_account = web.post('/api/ibkr/cash-events/sync', json={})
        assert wrong_account.status_code == 200, wrong_account.text
        assert wrong_account.json()['days'][0]['complete'] is False
        assert any(item['reason'] == 'missing_or_ambiguous_trade_section'
                   for item in wrong_account.json()['days'][0]['pending'])
        monkeypatch.delenv('PAMS_IBKR_CASH_EVENTS_AFTER', raising=False)
        report['xml'] = report['xml'].replace('tradeID="T1"', 'tradeID="T2"').replace('tradeMoney="340"', 'tradeMoney="999"')
        failed = web.post('/api/ibkr/flex/sync', json={})
        assert failed.status_code >= 400
    # A new client/connection models a restart: business publication survives,
    # while the most recent technical attempt cannot be inferred from it.
    with TestClient(main.app) as restarted:
        status=restarted.get('/api/ibkr/flex/status')
        assert status.status_code==200,status.text
        body=status.json()
        assert body['latest']['status']=='unknown' and body['latest']['error'] is None
        assert body['latest']['source']=='ibkr_flex' and body['latest']['asset_scope']=='ibkr'
        assert body['latest']['id']==published_run_id
        assert body['report_quality']['statement_as_of']==day.isoformat()
        assert body['report_quality']['last_attempt_status'] is None
        assert body['report_quality']['active_run_id'] > 0
    with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.pams_data_sync_runs'),count(*) FROM broker_processing_state WHERE state_type='publication'")
        assert cur.fetchone()==(None,1)
        with pytest.raises(psycopg2.errors.RaiseException):
            cur.execute("UPDATE broker_processing_state SET statement_date=statement_date+1 WHERE state_type='publication'")
        conn.rollback()
        with pytest.raises(psycopg2.errors.RaiseException):
            cur.execute("DELETE FROM broker_processing_state WHERE state_type='publication'")
        conn.rollback()
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='broker_processing_state'")
        assert {'status','started_at','finished_at','error_text','summary_json'}.isdisjoint({row[0] for row in cur.fetchall()})
        cur.execute("SELECT count(*) FROM pams_investment_trade_fact WHERE source_table='ibkr_flex_trade'")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT market_value FROM pams_holdings_snapshot WHERE account_code='ibkr' AND symbol='TEST'")
        assert cur.fetchone()[0] == 381
        cur.execute("SELECT to_regclass('pams_ibkr_trades'), to_regclass('pams_ibkr_position_history')")
        assert cur.fetchone() == (None, None)


def test_flex_http_trade_and_cash_share_one_report_source(empty_source_db,monkeypatch,tmp_path):
    import main
    from collectors import ibkr_flex
    from domain.brokers.ibkr import ibkr_report_state
    from endpoints import ibkr
    day=date(2026,10,22)
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'synthetic')",(day,))
    monkeypatch.setenv('PAMS_HOLDING_EVIDENCE_ISOLATED','true')
    monkeypatch.setenv('PAMS_IBKR_CASH_EVENTS_AFTER','2026-10-21')
    monkeypatch.setenv('PAMS_DATA_DIR',str(tmp_path))
    for module in (main,ibkr): monkeypatch.setattr(module,'get_conn',lambda:psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(ibkr,'_credentials',lambda:('token','query'))
    monkeypatch.setattr(ibkr_flex,'request_flex_reference',lambda *a,**k:{'reference_code':'TEST'})
    monkeypatch.setattr(ibkr_report_state,'utc_now',lambda:datetime(2026,10,22,22,tzinfo=timezone.utc))
    raw=XML.replace('ibCommission="-1.25"','ibCommission="-1.25" ibCommissionCurrency="USD" taxAmount="0"')
    cash='<CashTransactions><CashTransaction accountId="U_TEST" transactionID="CASH1" currency="USD" dateTime="20261022;120000" amount="10" type="Dividends" /></CashTransactions>'
    raw=raw.replace('</FlexStatement>',cash+'</FlexStatement>')
    monkeypatch.setattr(ibkr_flex,'fetch_flex_statement',lambda *a,**k:raw)
    with TestClient(main.app) as web:
        first=web.post('/api/ibkr/flex/sync',json={})
        assert first.status_code==200,first.text
        assert first.json()['sync_run']['publication']['cash_events']['inserted_count']==1
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            cur.execute('SELECT count(*) FROM source_batches'); assert cur.fetchone()[0]==1
            cur.execute('SELECT count(*) FROM source_files'); assert cur.fetchone()[0]==1
            cur.execute('SELECT count(*),count(distinct file_id) FROM source_items'); assert cur.fetchone()==(3,1)
            cur.execute("SELECT count(*),count(source_item_id) FROM pams_investment_trade_fact WHERE source_table IN ('ibkr_flex_trade','ibkr_flex_cash_transaction')")
            assert cur.fetchone()==(2,2)
        replay=web.post('/api/ibkr/flex/sync',json={})
        assert replay.status_code==200,replay.text
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            cur.execute('SELECT count(*) FROM source_batches'); assert cur.fetchone()[0]==1
            cur.execute('SELECT count(*) FROM pams_investment_trade_fact'); assert cur.fetchone()[0]==2


def test_late_cash_fx_publishes_supplement_without_changing_frozen_source(empty_source_db,monkeypatch,tmp_path):
    import main
    from collectors import ibkr_flex
    from domain.brokers.ibkr import ibkr_report_state
    from endpoints import ibkr
    day=date(2026,10,22)
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'synthetic')",(day,))
    monkeypatch.setenv('PAMS_HOLDING_EVIDENCE_ISOLATED','true')
    monkeypatch.setenv('PAMS_IBKR_CASH_EVENTS_AFTER','2026-10-21')
    monkeypatch.setenv('PAMS_DATA_DIR',str(tmp_path))
    for module in (main,ibkr): monkeypatch.setattr(module,'get_conn',lambda:psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(ibkr,'_credentials',lambda:('token','query'))
    monkeypatch.setattr(ibkr_flex,'request_flex_reference',lambda *a,**k:{'reference_code':'TEST'})
    monkeypatch.setattr(ibkr_report_state,'utc_now',lambda:datetime(2026,10,22,22,tzinfo=timezone.utc))
    raw=XML.replace('ibCommission="-1.25"','ibCommission="-1.25" ibCommissionCurrency="USD" taxAmount="0"')
    cash='<CashTransactions><CashTransaction accountId="U_TEST" transactionID="CASH1" currency="EUR" dateTime="20261022;120000" amount="10" type="Dividends" /></CashTransactions>'
    raw=raw.replace('</FlexStatement>',cash+'</FlexStatement>')
    monkeypatch.setattr(ibkr_flex,'fetch_flex_statement',lambda *a,**k:raw)
    with TestClient(main.app) as web:
        first=web.post('/api/ibkr/flex/sync',json={})
        assert first.status_code==200,first.text
        pending=first.json()['sync_run']['publication']['cash_events']['pending']
        assert any(item['reason']=='missing_or_invalid_dated_fx' for item in pending)
        status=web.get('/api/ibkr/flex/status')
        assert status.status_code==200,status.text
        latest=status.json()['latest']
        assert latest['cash_evidence_status']=='verified_publication_state'
        assert any(item['reason']=='missing_or_invalid_dated_fx' for item in latest['cash_events']['pending'])
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            with pytest.raises(psycopg2.errors.RaiseException):
                cur.execute("UPDATE broker_processing_state SET cash_state='{}'::jsonb WHERE state_type='publication'")
            conn.rollback()
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','EUR',8,'synthetic-market')",(day,))
        retry=web.post('/api/ibkr/flex/sync',json={})
        assert retry.status_code==200,retry.text
        status=web.get('/api/ibkr/flex/status')
        assert status.status_code==200,status.text
        assert status.json()['latest']['cash_evidence_status']=='verified_publication_state'
        assert status.json()['latest']['cash_events']['pending']==[]
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM broker_processing_state WHERE state_type='publication'")
            assert cur.fetchone()[0]==2  # Business pending state changed, not a technical retry log.
            cur.execute("SELECT count(*) FROM pams_investment_trade_fact WHERE source_table='ibkr_flex_cash_transaction'")
            assert cur.fetchone()[0]==1
            cur.execute("SELECT count(*) FROM pams_investment_trade_fact WHERE source_table='ibkr_flex_trade'")
            assert cur.fetchone()[0]==1
            cur.execute('SELECT count(*) FROM source_files'); assert cur.fetchone()[0]==1
            cur.execute("""SELECT i.origin_file_id, f.file_id, i.raw_fields->>'original_batch_id', f.batch_id::text
                FROM source_items i JOIN source_files f ON f.file_id=i.origin_file_id
                JOIN source_batches b ON b.batch_id=i.batch_id WHERE b.workflow='ibkr_cash_supplement'""")
            item=cur.fetchone(); assert item and item[0]==item[1] and item[2]==item[3]
        replay=web.post('/api/ibkr/flex/sync',json={})
        assert replay.status_code==200,replay.text
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            cur.execute('SELECT count(*) FROM source_files'); assert cur.fetchone()[0]==1
            cur.execute("SELECT count(*) FROM pams_investment_trade_fact WHERE source_table='ibkr_flex_cash_transaction'")
            assert cur.fetchone()[0]==1
        from domain.brokers.ibkr.ibkr_report_batch import _cash_supplement
        from domain.brokers.ibkr.ibkr_cash_events import normalize_cash_event
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM pams_investment_trade_fact WHERE source_table='ibkr_flex_cash_transaction'")
            fact=cur.fetchone()
            cur.execute("SELECT * FROM source_items WHERE origin_file_id IS NOT NULL")
            source=cur.fetchone(); proof=source['raw_fields']
            fx=source['parsed_fields']['fx_evidence']
            assert fx=={'as_of':day.isoformat(),'base_currency':'CNY','quote_currency':'EUR',
                        'rate':'8.00000000','source':'synthetic-market','kind':'daily_reference_not_broker_settlement'}
            original=ibkr_flex.parse_flex_report(raw,account_code='ibkr').cash_events[0]
            event=normalize_cash_event(original,account_code='ibkr',cutoff=date.min,statement_as_of=day)
            event['source_evidence']={'source_sha256':hashlib.sha256(raw.encode()).hexdigest()}
            args=dict(original_batch=proof['original_batch_id'],original_file=proof['original_file_id'],
                      original_scope=proof['original_item_id'],report_sha256=proof['report_sha256'],
                      source_account_id='U_TEST',event=event,ordinal=1,fact=fact,created=False)
            assert _cash_supplement(cur,**args)==str(source['batch_id'])
            for changes in ({'ordinal':2},{'source_account_id':'OTHER'},
                            {'report_sha256':'0'*64},{'event':dict(event,net_cash=Decimal('999'))}):
                with pytest.raises(ValueError): _cash_supplement(cur,**(args | changes))
            cur.execute('SAVEPOINT original_file')
            with pytest.raises(psycopg2.Error):
                cur.execute('DELETE FROM source_files WHERE file_id=%s',(proof['original_file_id'],))
            cur.execute('ROLLBACK TO SAVEPOINT original_file')
        # A corrupt summary never becomes a verified statement of no pending events.
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            cur.execute("""INSERT INTO broker_processing_state
                (state_type,account_code,source_batch_id,statement_date,source_sha256,replay_fingerprint,
                 published_result_sha256,holding_count,cash_state,cash_state_sha256)
                SELECT state_type,account_code,source_batch_id,statement_date,source_sha256,replay_fingerprint,
                       published_result_sha256,holding_count,'{"pending":[]}'::jsonb,%s
                FROM broker_processing_state WHERE state_type='publication' ORDER BY publication_id DESC LIMIT 1""",('0'*64,))
        status=web.get('/api/ibkr/flex/status')
        assert status.status_code==200,status.text
        assert status.json()['latest']['cash_evidence_status']=='unknown'
        assert status.json()['latest']['cash_events'] is None
        # Simulate privileged corruption only in this disposable database: the
        # independently stored result hash must make the report unavailable.
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            cur.execute('ALTER TABLE source_batches DISABLE TRIGGER source_batch_published_immutable')
            cur.execute("UPDATE source_batches SET published_result=published_result || '{\"tampered\":true}'::jsonb WHERE workflow='ibkr_report'")
            cur.execute('ALTER TABLE source_batches ENABLE TRIGGER source_batch_published_immutable')
        status=web.get('/api/ibkr/flex/status')
        assert status.status_code==200,status.text
        assert status.json()['latest']['publication'] is None
        assert status.json()['report_quality']['status']=='missing'
        with psycopg2.connect(**empty_source_db) as conn:
            assert ibkr_report_state.publication_floor(conn)==day


def test_cash_only_report_with_missing_fx_preserves_original_before_retry(empty_source_db,monkeypatch,tmp_path):
    import main
    from collectors import ibkr_flex
    from domain.brokers.ibkr import ibkr_report_state
    from endpoints import ibkr
    day=date(2026,10,22)
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
    monkeypatch.setenv('PAMS_HOLDING_EVIDENCE_ISOLATED','true')
    monkeypatch.setenv('PAMS_IBKR_CASH_EVENTS_AFTER','2026-10-21')
    monkeypatch.setenv('PAMS_DATA_DIR',str(tmp_path))
    for module in (main,ibkr): monkeypatch.setattr(module,'get_conn',lambda:psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(ibkr,'_credentials',lambda:('token','query'))
    monkeypatch.setattr(ibkr_flex,'request_flex_reference',lambda *a,**k:{'reference_code':'TEST'})
    monkeypatch.setattr(ibkr_report_state,'utc_now',lambda:datetime(2026,10,22,22,tzinfo=timezone.utc))
    raw=re.sub(r'<Trade\b[^>]*/>','',XML)
    cash='<CashTransactions><CashTransaction accountId="U_TEST" transactionID="CASH1" currency="EUR" dateTime="20261022;120000" amount="10" type="Dividends" /></CashTransactions>'
    raw=raw.replace('</FlexStatement>',cash+'</FlexStatement>')
    monkeypatch.setattr(ibkr_flex,'fetch_flex_statement',lambda *a,**k:raw)
    with TestClient(main.app) as web:
        first=web.post('/api/ibkr/flex/sync',json={})
        assert first.status_code==200,first.text
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            cur.execute('SELECT content FROM source_files'); assert bytes(cur.fetchone()[0])==raw.encode()
            cur.execute('SELECT count(*) FROM pams_investment_trade_fact'); assert cur.fetchone()[0]==0
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','EUR',8,'synthetic')",(day,))
        retry=web.post('/api/ibkr/flex/sync',json={})
        assert retry.status_code==200,retry.text
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM source_batches WHERE workflow='ibkr_cash_supplement'")
            assert cur.fetchone()[0]==1
            cur.execute('SELECT count(*) FROM source_files'); assert cur.fetchone()[0]==1


def test_flex_missing_tax_publishes_known_trade_and_holding_without_inventing_tax(empty_source_db,monkeypatch,tmp_path):
    import main
    from collectors import ibkr_flex
    from domain.brokers.ibkr import ibkr_report_state
    from domain.brokers.ibkr.ibkr_fact_source_verification import verify_ibkr_fact
    from endpoints import ibkr, investment_transactions
    day=date(2026,10,22)
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'synthetic')",(day,))
    monkeypatch.setenv('PAMS_HOLDING_EVIDENCE_ISOLATED','true')
    monkeypatch.delenv('PAMS_IBKR_CASH_EVENTS_AFTER',raising=False)
    monkeypatch.setenv('PAMS_DATA_DIR',str(tmp_path))
    for module in (main,ibkr,investment_transactions):
        monkeypatch.setattr(module,'get_conn',lambda:psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(ibkr,'_credentials',lambda:('token','query'))
    monkeypatch.setattr(ibkr_flex,'request_flex_reference',lambda *a,**k:{'reference_code':'TEST'})
    monkeypatch.setattr(ibkr_report_state,'utc_now',lambda:datetime(2026,10,22,22,tzinfo=timezone.utc))
    raw=XML.replace('ibCommission="-1.25"','ibCommission="-1.25" ibCommissionCurrency="USD"')
    monkeypatch.setattr(ibkr_flex,'fetch_flex_statement',lambda *a,**k:raw)
    with TestClient(main.app) as web:
        result=web.post('/api/ibkr/flex/sync',json={})
        assert result.status_code==200,result.text
        replay=web.post('/api/ibkr/flex/sync',json={})
        assert replay.status_code==200,replay.text
        assert replay.json()['sync_run']['publication']['report_trade_sources']['replayed_verified']==1
        rows=web.get('/api/investment-transactions?account_code=ibkr')
        assert rows.status_code==200,rows.text
        trade=next(row for row in rows.json()['rows'] if row['symbol']=='TEST')
        assert trade['tax_amount'] is None and Decimal(str(trade['net_cash']))==Decimal('-341.25')
    with psycopg2.connect(**empty_source_db) as conn:
        conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT trade_id,tax_amount,net_cash FROM pams_investment_trade_fact WHERE source_table='ibkr_flex_trade'")
            fact=cur.fetchone()
            assert fact['tax_amount'] is None and fact['net_cash']==Decimal('-341.25')
            evidence=verify_ibkr_fact(cur,trade_id=fact['trade_id'])
            assert evidence['status']=='partial' and {'tax_amount','net_reconciliation'}<=set(evidence['unknown_fields'])
            cur.execute("SELECT quantity,market_value FROM pams_holdings_snapshot WHERE account_code='ibkr' AND symbol='TEST'")
            holding=cur.fetchone()
            assert holding['quantity']==2 and holding['market_value']==381


@pytest.mark.parametrize('fee_currency',[None,'EUR'],ids=['unknown_fee_currency','cross_currency_fee'])
def test_flex_unconverted_fee_does_not_block_known_trade_and_holding(empty_source_db,monkeypatch,tmp_path,fee_currency):
    import main
    from collectors import ibkr_flex
    from domain.brokers.ibkr import ibkr_report_state
    from domain.brokers.ibkr.ibkr_fact_source_verification import verify_ibkr_fact
    from endpoints import ibkr, investment_transactions
    day=date(2026,10,22)
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'synthetic')",(day,))
    monkeypatch.setenv('PAMS_HOLDING_EVIDENCE_ISOLATED','true')
    monkeypatch.delenv('PAMS_IBKR_CASH_EVENTS_AFTER',raising=False)
    monkeypatch.setenv('PAMS_DATA_DIR',str(tmp_path))
    for module in (main,ibkr,investment_transactions): monkeypatch.setattr(module,'get_conn',lambda:psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(ibkr,'_credentials',lambda:('token','query'))
    monkeypatch.setattr(ibkr_flex,'request_flex_reference',lambda *a,**k:{'reference_code':'TEST'})
    monkeypatch.setattr(ibkr_report_state,'utc_now',lambda:datetime(2026,10,22,22,tzinfo=timezone.utc))
    attribute=f' ibCommissionCurrency="{fee_currency}"' if fee_currency else ''
    raw=XML.replace('ibCommission="-1.25"',f'ibCommission="-1.25"{attribute} taxAmount="0"')
    monkeypatch.setattr(ibkr_flex,'fetch_flex_statement',lambda *a,**k:raw)
    with TestClient(main.app) as web:
        response=web.post('/api/ibkr/flex/sync',json={})
        assert response.status_code==200,response.text
        replay=web.post('/api/ibkr/flex/sync',json={})
        assert replay.status_code==200,replay.text
        transactions=web.get('/api/investment-transactions?account_code=ibkr')
        assert transactions.status_code==200,transactions.text
        result=transactions.json()
        trade=next(row for row in result['rows'] if row['symbol']=='TEST')
        assert trade['fees'] is None and trade['fees_cny'] is None and trade['fees_status']=='unknown'
        assert Decimal(str(trade['net_cash']))==Decimal('-341.25')
        assert result['summary']['fees_cny'] is None
    with psycopg2.connect(**empty_source_db) as conn:
        conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT trade_id,fee_amount,fee_currency,net_cash FROM pams_investment_trade_fact WHERE source_table='ibkr_flex_trade'")
            fact=cur.fetchone()
            assert fact['fee_amount']==Decimal('1.25') and fact['fee_currency']==fee_currency
            assert fact['net_cash']==Decimal('-341.25')
            proof=verify_ibkr_fact(cur,trade_id=fact['trade_id'])
            assert proof['status']=='partial' and 'net_reconciliation' in proof['unknown_fields']
            if fee_currency is None: assert 'fee_currency' in proof['unknown_fields']
            cur.execute("SELECT quantity,market_value FROM pams_holdings_snapshot WHERE account_code='ibkr' AND symbol='TEST'")
            holding=cur.fetchone()
            assert holding['quantity']==2 and holding['market_value']==381
            from domain.investments.portfolio_state import load_investment_transactions
            transactions=load_investment_transactions(cur, 'CNY')
            item=next(row for row in transactions if row['symbol']=='TEST')
            assert item['fees'] is None and item['net_cash']==Decimal('-341.25')


def test_delayed_flex_report_keeps_native_trade_until_its_dated_fx_arrives(empty_source_db,monkeypatch,tmp_path):
    import main
    from collectors import ibkr_flex
    from domain.brokers.ibkr import ibkr_report_state
    from endpoints import ibkr, investment_transactions
    day=date(2026,10,23)
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES ('2026-10-22','CNY','USD',7,'synthetic')")
    monkeypatch.setenv('PAMS_HOLDING_EVIDENCE_ISOLATED','true')
    monkeypatch.delenv('PAMS_IBKR_CASH_EVENTS_AFTER',raising=False)
    monkeypatch.setenv('PAMS_DATA_DIR',str(tmp_path))
    for module in (main,ibkr,investment_transactions): monkeypatch.setattr(module,'get_conn',lambda:psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(ibkr,'_credentials',lambda:('token','query'))
    monkeypatch.setattr(ibkr_flex,'request_flex_reference',lambda *a,**k:{'reference_code':'TEST'})
    monkeypatch.setattr(ibkr_report_state,'utc_now',lambda:datetime(2026,10,23,22,tzinfo=timezone.utc))
    raw=XML.replace('20261022','20261023').replace('ibCommission="-1.25"','ibCommission="-1.25" ibCommissionCurrency="USD" taxAmount="0"')
    monkeypatch.setattr(ibkr_flex,'fetch_flex_statement',lambda *a,**k:raw)
    with TestClient(main.app) as web:
        first=web.post('/api/ibkr/flex/sync',json={})
        assert first.status_code==200,first.text
        def trade():
            response=web.get('/api/investment-transactions?account_code=ibkr')
            assert response.status_code==200,response.text
            return next(row for row in response.json()['rows'] if row['symbol']=='TEST')
        initial=trade()
        assert Decimal(str(initial['net_cash']))==Decimal('-341.25') and initial['net_cash_cny'] is None
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            cur.execute("SELECT fx_rate_to_cny,net_cash_cny FROM pams_investment_trade_fact WHERE source_table='ibkr_flex_trade'")
            assert cur.fetchone()==(None,None)
            cur.execute("SELECT quantity,market_value FROM pams_holdings_snapshot WHERE account_code='ibkr' AND symbol='TEST'")
            assert cur.fetchone()==(2,381)
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'synthetic')",(day,))
        replay=web.post('/api/ibkr/flex/sync',json={})
        assert replay.status_code==200,replay.text
        assert replay.json()['sync_run']['publication']['report_trade_sources']['replayed_verified']==1
        assert Decimal(str(trade()['net_cash_cny']))==Decimal('-2388.75')
    with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
        cur.execute("SELECT count(*),max(fx_rate_to_cny),max(net_cash_cny) FROM pams_investment_trade_fact WHERE source_table='ibkr_flex_trade'")
        assert cur.fetchone()==(1,None,None)


def test_ibkr_history_uses_only_verified_trade_facts_and_marks_coverage_unknown(empty_source_db,monkeypatch):
    from datetime import timedelta
    from domain.brokers.ibkr.ibkr_report_batch import publish_report_batch
    from domain.investments import investment_return_ibkr as service
    day=date(2026,10,22)
    raw=XML.replace('ibCommission="-1.25"','ibCommission="-1.25" ibCommissionCurrency="USD" taxAmount="0"').encode()
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'synthetic')",(day,))
            publish_report_batch(cur,report_bytes=raw,source_account_id='U_TEST',day=day,report_sha256=hashlib.sha256(raw).hexdigest())
        conn.commit()
    monkeypatch.setattr(service,'_fetch_yfinance_closes',lambda *a,**k:({'TEST':{day+timedelta(days=1):Decimal('200')}},[]))
    monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED','false')
    sample=day+timedelta(days=1)
    with psycopg2.connect(**empty_source_db) as conn:
        conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            values,costs,warnings=service._ibkr_full_trade_return_series(cur,dates=[sample],base_currency='USD',fx_timelines={})
            assert values[sample]==Decimal('400') and costs[sample]==Decimal('341.25')
            assert 'ibkr_history_coverage_unverified' in warnings
            absent,absent_cost,_=service._ibkr_full_trade_return_series(
                cur,dates=[sample],base_currency='CNY',fx_timelines={'USD':[(day,Decimal('7'))]})
            assert absent[sample] is None and absent_cost[sample] is None
            converted,converted_cost,_=service._ibkr_full_trade_return_series(
                cur,dates=[sample],base_currency='CNY',fx_timelines={'USD':[(sample,Decimal('7'))]})
            assert converted[sample]==Decimal('2800') and converted_cost[sample]==Decimal('2388.75')
    with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
        cur.execute("UPDATE pams_investment_trade_fact SET quantity=3 WHERE source_table='ibkr_flex_trade'")
    with psycopg2.connect(**empty_source_db) as conn:
        conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            _,_,warnings=service._ibkr_full_trade_return_series(cur,dates=[sample],base_currency='USD',fx_timelines={})
            assert 'ibkr_history_missing:'+sample.isoformat() in warnings


def test_development_ibkr_history_uses_exact_local_market_prices_without_network(empty_source_db, monkeypatch):
    from datetime import timedelta
    import main
    from domain.brokers.ibkr.ibkr_report_batch import publish_report_batch
    from domain.investments import investment_return_ibkr as service
    from endpoints import prices
    day = date(2026, 10, 22)
    first = day + timedelta(days=1)
    missing = day + timedelta(days=2)
    future = day + timedelta(days=3)
    raw = XML.replace('ibCommission="-1.25"', 'ibCommission="-1.25" ibCommissionCurrency="USD" taxAmount="0"').encode()
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            publish_report_batch(cur, report_bytes=raw, source_account_id='U_TEST', day=day,
                                 report_sha256=hashlib.sha256(raw).hexdigest())
            cur.execute("""INSERT INTO pams_ohlcv_daily(symbol,market,trade_date,close)
                VALUES ('TEST','YF',%s,200), ('TEST','CN_A',%s,999), ('TEST','YF',%s,300)""",
                (first, missing, future))
    monkeypatch.setenv('PAMS_SOURCE_MODE', 'development')
    monkeypatch.delenv('PAMS_UNIFIED_SOURCE_ISOLATED', raising=False)
    monkeypatch.setattr(service, '_fetch_yfinance_closes', lambda *a, **k: pytest.fail('unexpected external fetch'))
    with psycopg2.connect(**empty_source_db) as conn:
        conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            values, costs, warnings = service._ibkr_full_trade_return_series(
                cur, dates=[first, missing], base_currency='USD', fx_timelines={})
            assert values[first] == Decimal('400') and costs[first] == Decimal('341.25')
            assert values[missing] is None and costs[missing] == Decimal('341.25')
            assert 'ibkr_history_missing:' + missing.isoformat() in warnings
            assert not any(warning.startswith('ibkr_fx_missing:') for warning in warnings)
            fx_values, fx_costs, fx_warnings = service._ibkr_full_trade_return_series(
                cur, dates=[missing], base_currency='CNY', fx_timelines={})
            assert fx_values[missing] is None and fx_costs[missing] is None
            assert 'ibkr_fx_missing:' + missing.isoformat() in fx_warnings
            converted_values, converted_costs, _ = service._ibkr_full_trade_return_series(
                cur, dates=[missing], base_currency='CNY',
                fx_timelines={'USD': [(missing, Decimal('7'))]})
            assert converted_values[missing] is None and converted_costs[missing] == Decimal('2388.75')
    monkeypatch.setattr(prices, '_get_conn', lambda: psycopg2.connect(**empty_source_db))
    with TestClient(main.app) as web:
        response = web.get('/api/prices/series', params={'symbols': 'TEST', 'market': 'US',
            'start': first.isoformat(), 'end': missing.isoformat(), 'normalize': 'false'})
        assert response.status_code == 200, response.text
        curve = response.json()['series'][0]
        assert curve['market'] == 'YF' and curve['coverage'] == 'ok' and curve['points'] == 1
        assert curve['rows'][0]['date'] == first.isoformat()
        empty = web.get('/api/prices/series', params={'symbols': 'TEST', 'market': 'US',
            'start': missing.isoformat(), 'end': missing.isoformat()})
        assert empty.status_code == 200, empty.text
        assert empty.json()['series'][0]['coverage'] == 'missing'


def test_return_http_reads_verified_ibkr_facts_when_isolated_writes_are_disabled(empty_source_db,monkeypatch):
    from datetime import timedelta
    import main
    from domain.brokers.ibkr.ibkr_report_batch import publish_report_batch
    from domain.investments import investment_return_ibkr as service
    day=date(2026,10,22)
    sample=day+timedelta(days=1)
    raw=XML.replace('ibCommission="-1.25"','ibCommission="-1.25" ibCommissionCurrency="USD" taxAmount="0"').encode()
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'synthetic')",(day,))
            publish_report_batch(cur,report_bytes=raw,source_account_id='U_TEST',day=day,report_sha256=hashlib.sha256(raw).hexdigest())
        conn.commit()
    monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED','false')
    monkeypatch.setattr(main,'get_conn',lambda:psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(main.accounts_endpoint,'_overview_payload',lambda cur=None:{'accounts':[]})
    monkeypatch.setattr(main,'_investment_return_view_as_of',lambda cur:sample)
    monkeypatch.setattr(main,'_investment_today_label_date',lambda:sample)
    monkeypatch.setattr(main,'_build_section_return_series_payload_from_snapshot',lambda *a,**k:None)
    monkeypatch.setattr(main,'_build_investment_return_series_payload_from_snapshot',lambda *a,**k:None)
    monkeypatch.setattr(main,'_apply_section_holdings_pnl_overlay',lambda cur,section,payload:payload)
    monkeypatch.setattr(main,'_latest_published_portfolio_valuation_state',lambda *a,**k:None)
    monkeypatch.setattr(main,'_build_summary2_latest_payload_for_request',lambda cur:{})
    monkeypatch.setattr(main,'_apply_published_valuation_state_to_return_series_payload',lambda payload,state:payload)
    monkeypatch.setattr(main,'_apply_summary_pnl_to_return_series_payload',lambda payload,summary:payload)
    monkeypatch.setattr(service,'_fetch_yfinance_closes',lambda *a,**k:({'TEST':{sample:Decimal('200')}},[]))
    def build(cur,**kwargs):
        values,costs,warnings=service._ibkr_full_trade_return_series(cur,dates=[sample],base_currency='USD',fx_timelines={})
        cur.execute("SELECT current_setting('transaction_read_only') AS readonly,current_setting('transaction_isolation') AS isolation")
        mode=cur.fetchone()
        return {'value':str(values[sample]),'cost':str(costs[sample]),'warnings':warnings,'mode':mode}
    monkeypatch.setattr(main,'_build_section_return_series_payload',build)
    monkeypatch.setattr(main,'_build_investment_return_series_payload',build)
    with TestClient(main.app) as web:
        for route in ('/investment/section-return-series?section=us_stock&start=2026-10-23',
                      '/investment/return-series?start=2026-10-23'):
            response=web.get(route)
            assert response.status_code==200,response.text
            result=response.json()
            assert Decimal(result['value'])==Decimal('400') and Decimal(result['cost'])==Decimal('341.25')
            assert result['mode']=={'readonly':'on','isolation':'repeatable read'}
            assert 'ibkr_history_coverage_unverified' in result['warnings']
