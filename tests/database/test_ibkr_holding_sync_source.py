"""Actual Flex HTTP sync with reviewed zero source in a disposable PG database."""
# ruff: noqa: F811
from datetime import date
import os
from pathlib import Path
import re
from uuid import uuid4

import pytest
from psycopg2 import sql
from psycopg2.extras import RealDictCursor
from psycopg2.extensions import parse_dsn

from tests.brokers.ibkr.test_ibkr_sync_transaction import XML
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from scripts.bootstrap_isolated_sources import initialize_isolated_sources
import psycopg2
from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.testclient import TestClient
from collectors import ibkr_flex as collector
from endpoints import ibkr
from domain.brokers.ibkr import ibkr_report_state
from tests.database.test_ibkr_zero_holding_source import stage_report, confirm, ZERO
from domain.imports import isolation
from domain.investments import holding_evidence_writer, holding_write_evidence

ROOT = Path(__file__).resolve().parents[2]
DAY = date(2026, 10, 22)


@pytest.fixture
def sync_case(empty_source_db, monkeypatch, tmp_path):
    conn = psycopg2.connect(**empty_source_db)
    initialize_isolated_sources(conn)
    monkeypatch.setenv('PAMS_HOLDING_EVIDENCE_ISOLATED', 'true')
    monkeypatch.delenv('PAMS_IBKR_CASH_EVENTS_AFTER', raising=False)
    monkeypatch.setattr(ibkr_report_state, 'utc_now', lambda: datetime(2026, 10, 22, 22, tzinfo=timezone.utc))
    monkeypatch.setattr(ibkr, '_credentials', lambda: ('test-token', 'test-query'))
    monkeypatch.setattr(ibkr, 'get_conn', lambda: conn)
    monkeypatch.setattr(collector, 'request_flex_reference', lambda *a, **k: {'reference_code': 'TEST'})
    report = {'xml': XML}
    monkeypatch.setattr(collector, 'fetch_flex_statement', lambda *a, **k: report['xml'])
    with conn.cursor() as cur:
        cur.execute("INSERT INTO pams_accounts(account_code,display_name,base_currency,is_asset,active) VALUES ('ibkr','IBKR','USD',true,true) ON CONFLICT DO NOTHING")
        cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES ('2026-10-22','CNY','USD',7,'synthetic') ON CONFLICT DO NOTHING")
    conn.commit()
    app = FastAPI()
    app.include_router(ibkr.router, prefix='/api')
    with TestClient(app) as web:
        yield web, conn, report
    conn.close()


@pytest.mark.parametrize('old_day',[DAY,date(2026,10,21)],ids=['same_day','previous_day'])
def test_http_same_contract_reviewed_exit_and_replay(sync_case, monkeypatch, old_day):
    from domain.brokers.ibkr.ibkr_holding_identity import review_holding_identity
    from domain.imports.unified_source import lock_source_review, review_item
    from uuid import UUID
    web,conn,report=sync_case
    params=conn.get_dsn_parameters()
    with conn.cursor() as cur:
        cur.execute(sql.SQL("COMMENT ON DATABASE {} IS 'pams-isolated-account-import'").format(sql.Identifier(params['dbname'])))
        cur.execute("INSERT INTO pams_holdings_snapshot(account_code,name,symbol,currency,major_category,as_of,market_value,cost,quantity,asset_liability,minor_category) VALUES ('ibkr','Retired','RETIRE','USD','美股',%s,123,80,10,'资产','stock') RETURNING id",(old_day,))
        holding=cur.fetchone()[0]
    conn.commit()
    def isolated(cur):
        details=cur.connection.get_dsn_parameters()
        assert details['host'] in {'127.0.0.1','localhost','::1'} and re.fullmatch(r'pams_import_test_[0-9a-f]{32}',details['dbname'])
        cur.execute("SELECT shobj_description(oid,'pg_database') AS marker FROM pg_database WHERE datname=current_database()")
        assert cur.fetchone()['marker']=='pams-isolated-account-import'
        return True
    monkeypatch.setattr(isolation,'source_enabled',isolated)
    monkeypatch.setattr(holding_evidence_writer,'source_enabled',isolated)
    monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED','true')
    monkeypatch.setenv('PAMS_HOLDING_EVIDENCE_ISOLATED','true')
    base=re.search(r'<OpenPosition\b[^>]*/>',XML).group()
    zero=ZERO.replace('symbol="TEST"','symbol="RETIRE" conid="1001"').replace('marketValue="0"','marketValue="0" positionValue="0"')
    report['xml']=XML.replace(base,'<OpenPositions>'+base+zero+'</OpenPositions>').replace('ibCommission="-1.25"','ibCommission="-1.25" ibCommissionCurrency="USD" taxAmount="0"')
    old_position=zero.replace('position="0"','position="10"').replace('costBasisMoney="0"','costBasisMoney="80"').replace('marketValue="0" positionValue="0"','marketValue="123" positionValue="123"')
    old_xml=report['xml'].replace(zero,old_position).replace('20261022',old_day.strftime('%Y%m%d'))
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        old=stage_report(cur,batch_id=uuid4(),account_code='ibkr',source_account_id='U_TEST',business_date=old_day,content=old_xml.encode())
        cur.execute('SELECT revision FROM source_batches WHERE batch_id=%s',(old['batch_id'],)); rev=cur.fetchone()['revision']
        for item in [old['scope_item_id'],old['position_item_ids'][0]]:
            rev=review_item(cur,batch_id=UUID(old['batch_id']),item_id=UUID(item),expected_revision=rev,parsed_fields={},review_status='confirmed',review_note='synthetic review')
        rev=review_holding_identity(cur,batch_id=old['batch_id'],item_id=old['position_item_ids'][1],holding_id=holding,expected_revision=rev,review_note='synthetic contract review')
        snap=lock_source_review(cur,batch_id=UUID(old['batch_id']),expected_revision=rev)
        cur.execute("UPDATE source_batches SET status='published',published_source_hash=%s WHERE batch_id=%s",(snap['source_snapshot_sha256'],old['batch_id']))
        staged=stage_report(cur,batch_id=uuid4(),account_code='ibkr',source_account_id='U_TEST',business_date=DAY,content=report['xml'].encode())
    conn.commit()
    confirm({**parse_dsn(os.environ['PAMS_TEST_POSTGRES_DSN']),'dbname':params['dbname']},staged)
    ref={'batch_id':staged['batch_id'],'item_id':staged['position_item_ids'][1],
         'identity_batch_id':old['batch_id'],'identity_item_id':old['position_item_ids'][1]}
    result=web.post('/api/ibkr/flex/sync',json={'holding_retirement_sources':{'Retired':ref}})
    assert result.status_code==200,result.text
    with conn.cursor() as cur:
        cur.execute('SELECT quantity,cost,market_value FROM pams_holdings_snapshot WHERE account_code=%s AND name=%s AND as_of=%s',('ibkr','Retired',DAY))
        assert cur.fetchone()==(0,0,0)
        if old_day!=DAY:
            cur.execute('SELECT quantity,cost,market_value,as_of FROM pams_holdings_snapshot WHERE id=%s',(holding,))
            assert cur.fetchone()==(10,80,123,old_day)
    conn.rollback()
    replay=web.post('/api/ibkr/flex/sync',json={'holding_retirement_sources':{'Retired':ref}})
    assert replay.status_code==200,replay.text
    from domain.investments.ibkr_closed_evidence import verified_ibkr_closed_positions
    monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED','false')
    connection_params={**parse_dsn(os.environ['PAMS_TEST_POSTGRES_DSN']), 'dbname':params['dbname']}
    with psycopg2.connect(**connection_params) as read_conn:
        read_conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with read_conn.cursor(cursor_factory=RealDictCursor) as cur:
            closed=verified_ibkr_closed_positions(cur)
            retired=[row for row in closed if row['name']=='Retired']
            assert len(retired)==1 and retired[0]['is_closed'] and retired[0]['quantity']==0
            assert retired[0]['closed_evidence']=='confirmed_zero_and_immutable_identity'
            assert retired[0]['holding_pnl'] is None and retired[0]['cost'] is None
    import main
    monkeypatch.setattr(main,'get_conn',lambda:psycopg2.connect(**connection_params))
    monkeypatch.setattr(main,'_holdings_market_view_as_of',lambda cur:DAY)
    monkeypatch.setattr(main,'_daily_pnl_view_as_of',lambda:DAY)
    monkeypatch.setattr(main,'_excluded_holding_accounts',lambda:set())
    monkeypatch.setattr(main,'_account_owner_ratios',lambda:{})
    monkeypatch.setattr(main,'_default_owner_ratio',lambda:1)
    with TestClient(main.app) as holdings_web:
        response=holdings_web.get('/holdings/latest?includeClosed=true')
        assert response.status_code==200,response.text
        shown=[row for row in response.json()['rows'] if row['name']=='Retired']
        assert len(shown)==1 and shown[0]['is_closed'] is True
        assert shown[0]['closed_evidence']=='confirmed_zero_and_immutable_identity'
    with psycopg2.connect(**connection_params) as read_conn:
        read_conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with read_conn.cursor(cursor_factory=RealDictCursor) as reader:
            assert [row for row in verified_ibkr_closed_positions(reader) if row['name']=='Retired']
            with psycopg2.connect(**connection_params) as writer,writer.cursor() as cur:
                cur.execute('ALTER TABLE source_items DISABLE TRIGGER USER')
                cur.execute("UPDATE source_items SET review_note='tampered' WHERE source_item_id=%s",(ref['item_id'],))
                cur.execute('ALTER TABLE source_items ENABLE TRIGGER USER')
            # One request sees one database snapshot even when evidence changes concurrently.
            assert [row for row in verified_ibkr_closed_positions(reader) if row['name']=='Retired']
        read_conn.rollback()
        with read_conn.cursor(cursor_factory=RealDictCursor) as reader:
            assert not [row for row in verified_ibkr_closed_positions(reader) if row['name']=='Retired']
    with psycopg2.connect(**connection_params) as writer,writer.cursor() as cur:
        cur.execute('ALTER TABLE source_items DISABLE TRIGGER USER')
        cur.execute("UPDATE source_items SET review_note='synthetic position checked' WHERE source_item_id=%s",(ref['item_id'],))
        cur.execute('ALTER TABLE source_items ENABLE TRIGGER USER')
    with psycopg2.connect(**connection_params) as read_conn:
        read_conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with read_conn.cursor(cursor_factory=RealDictCursor) as cur:
            assert [row for row in verified_ibkr_closed_positions(cur) if row['name']=='Retired']
    with psycopg2.connect(**connection_params) as writer,writer.cursor() as cur:
        cur.execute('ALTER TABLE source_items DISABLE TRIGGER USER')
        cur.execute("UPDATE source_items SET review_note='identity tampered' WHERE source_item_id=%s",(ref['identity_item_id'],))
        cur.execute('ALTER TABLE source_items ENABLE TRIGGER USER')
    with psycopg2.connect(**connection_params) as read_conn:
        read_conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with read_conn.cursor(cursor_factory=RealDictCursor) as cur:
            assert not [row for row in verified_ibkr_closed_positions(cur) if row['name']=='Retired']
    with psycopg2.connect(**connection_params) as writer,writer.cursor() as cur:
        cur.execute('ALTER TABLE source_items DISABLE TRIGGER USER')
        cur.execute("UPDATE source_items SET review_note='synthetic contract review' WHERE source_item_id=%s",(ref['identity_item_id'],))
        cur.execute('ALTER TABLE source_items ENABLE TRIGGER USER')
    with psycopg2.connect(**connection_params) as read_conn:
        read_conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with read_conn.cursor(cursor_factory=RealDictCursor) as cur:
            assert [row for row in verified_ibkr_closed_positions(cur) if row['name']=='Retired']
    with psycopg2.connect(**connection_params) as writer,writer.cursor() as cur:
        cur.execute('ALTER TABLE source_batches DISABLE TRIGGER USER')
        cur.execute("UPDATE source_batches SET status='draft' WHERE batch_id=%s",(ref['batch_id'],))
        cur.execute('ALTER TABLE source_batches ENABLE TRIGGER USER')
    with psycopg2.connect(**connection_params) as read_conn:
        read_conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with read_conn.cursor(cursor_factory=RealDictCursor) as cur:
            assert not [row for row in verified_ibkr_closed_positions(cur) if row['name']=='Retired']


def test_real_flex_sync_retirement_replay_and_unknown_gap(sync_case, monkeypatch):
    web, conn, report = sync_case
    params = conn.get_dsn_parameters()
    assert params['host'] in {'127.0.0.1', 'localhost', '::1'}
    assert re.fullmatch(r'pams_import_test_[0-9a-f]{32}', params['dbname'])
    with conn.cursor() as cur:
        cur.execute(sql.SQL("COMMENT ON DATABASE {} IS 'pams-isolated-account-import'").format(sql.Identifier(params['dbname'])))
        cur.execute("INSERT INTO pams_holdings_snapshot(account_code,name,symbol,currency,major_category,as_of,market_value,cost,quantity,asset_liability,minor_category) VALUES ('ibkr','Retired','RETIRE','USD','美股',%s,123,80,10,'资产','stock')", (DAY,))
        cur.execute("INSERT INTO pams_accounts(account_code,display_name,base_currency,is_asset,active) VALUES ('other','Synthetic other','USD',true,true)")
        cur.execute("INSERT INTO pams_holdings_snapshot(account_code,name,symbol,currency,major_category,as_of,market_value,cost,quantity,asset_liability,minor_category) VALUES ('other','Other','OTHER','USD','美股',%s,55,40,1,'资产','stock')", (DAY,))
        cur.execute("SELECT row_to_json(t)::text FROM pams_holdings_snapshot t WHERE account_code='other'")
        protected = cur.fetchall()
    conn.commit()

    def test_database_only(cur):
        details = cur.connection.get_dsn_parameters()
        assert details['host'] in {'127.0.0.1', 'localhost', '::1'}
        assert re.fullmatch(r'pams_import_test_[0-9a-f]{32}', details['dbname'])
        cur.execute("SELECT shobj_description(oid, 'pg_database') AS marker FROM pg_database WHERE datname=current_database()")
        assert cur.fetchone()['marker'] == 'pams-isolated-account-import'
        return True

    monkeypatch.setattr(isolation, 'source_enabled', test_database_only)
    monkeypatch.setattr(holding_evidence_writer, 'source_enabled', test_database_only)
    monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED', 'true')
    monkeypatch.setenv('PAMS_HOLDING_EVIDENCE_ISOLATED', 'true')
    position = re.search(r'<OpenPosition\b[^>]*/>', XML).group()
    zero = ZERO.replace('symbol="TEST"', 'symbol="RETIRE"').replace('marketValue="0"', 'marketValue="0" positionValue="0"')
    report['xml'] = XML.replace(position, '<OpenPositions>' + position + zero + '</OpenPositions>').replace('ibCommission="-1.25"', 'ibCommission="-1.25" ibCommissionCurrency="USD" taxAmount="0"')
    old_trade = re.search(r'<Trade\b[^>]*/>', XML).group().replace('tradeID="T1"', 'tradeID="H_IN_REPORT"').replace('tradeDate="20261022"', 'tradeDate="20261001"')
    report['xml'] = report['xml'].replace('</FlexStatement>', old_trade + '</FlexStatement>')
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        staged = stage_report(cur, batch_id=uuid4(), account_code='ibkr', source_account_id='U_TEST',
                              business_date=DAY, content=report['xml'].encode())
    conn.commit()
    # Reuse the test helper's review logic against this disposable database only.
    confirm({**parse_dsn(os.environ['PAMS_TEST_POSTGRES_DSN']), 'dbname': params['dbname']}, staged)
    item = staged['position_item_ids'][1]
    payload = {'holding_retirement_sources': {'Retired': {'batch_id': staged['batch_id'], 'item_id': item}}}

    unknown = web.post('/api/ibkr/flex/sync', json={})
    assert unknown.status_code == 500, unknown.text
    with conn.cursor() as cur:
        cur.execute("SELECT market_value FROM pams_holdings_snapshot WHERE account_code='ibkr' AND name='Retired'")
        assert cur.fetchone()[0] == 123
    conn.rollback()

    original = report['xml']
    report['xml'] = original.replace('TEST STOCK', 'TEST STOCK CHANGED')
    wrong_report = web.post('/api/ibkr/flex/sync', json=payload)
    assert wrong_report.status_code == 500
    with conn.cursor() as cur:
        cur.execute("SELECT market_value FROM pams_holdings_snapshot WHERE account_code='ibkr' AND name='Retired'")
        assert cur.fetchone()[0] == 123
    conn.rollback()
    report['xml'] = original.replace(' ibCommissionCurrency="USD"', '')
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        missing_unit_source = stage_report(cur, batch_id=uuid4(), account_code='ibkr', source_account_id='U_TEST',
                                           business_date=DAY, content=report['xml'].encode())
    conn.commit()
    confirm({**parse_dsn(os.environ['PAMS_TEST_POSTGRES_DSN']), 'dbname': params['dbname']}, missing_unit_source)
    missing_unit_payload = {'holding_retirement_sources': {'Retired': {'batch_id': missing_unit_source['batch_id'],
                                                                       'item_id': missing_unit_source['position_item_ids'][1]}}}
    missing_unit = web.post('/api/ibkr/flex/sync', json=missing_unit_payload)
    assert missing_unit.status_code == 500
    with conn.cursor() as cur:
        cur.execute("SELECT market_value FROM pams_holdings_snapshot WHERE account_code='ibkr' AND name='Retired'")
        assert cur.fetchone()[0] == 123
    conn.rollback()
    report['xml'] = original
    report['xml'] = original.replace(' taxAmount="0"', '')
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        missing_tax_source = stage_report(cur, batch_id=uuid4(), account_code='ibkr', source_account_id='U_TEST',
                                          business_date=DAY, content=report['xml'].encode())
    conn.commit()
    confirm({**parse_dsn(os.environ['PAMS_TEST_POSTGRES_DSN']), 'dbname': params['dbname']}, missing_tax_source)
    missing_tax_payload = {'holding_retirement_sources': {'Retired': {'batch_id': missing_tax_source['batch_id'],
                                                                      'item_id': missing_tax_source['position_item_ids'][1]}}}
    missing_tax = web.post('/api/ibkr/flex/sync', json=missing_tax_payload)
    assert missing_tax.status_code == 500
    with conn.cursor() as cur:
        cur.execute("SELECT market_value FROM pams_holdings_snapshot WHERE account_code='ibkr' AND name='Retired'")
        assert cur.fetchone()[0] == 123
    conn.rollback()
    report['xml'] = original
    with conn.cursor() as cur:
        cur.execute("DELETE FROM pams_fx_rates_daily WHERE as_of=%s AND base_currency='CNY' AND quote_currency='USD'", (DAY,))
    conn.commit()
    missing_fx = web.post('/api/ibkr/flex/sync', json=payload)
    assert missing_fx.status_code == 500
    with conn.cursor() as cur:
        cur.execute("SELECT market_value FROM pams_holdings_snapshot WHERE account_code='ibkr' AND name='Retired'")
        assert cur.fetchone()[0] == 123
        cur.execute("SELECT count(*) FROM pams_investment_trade_fact WHERE source_table='ibkr_flex_trade'")
        assert cur.fetchone()[0] == 0
        cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'synthetic')", (DAY,))
    conn.commit()
    # Matching symbol and currency cannot establish the old holding's conid.
    # A reviewed zero report must leave both facts and existing amounts intact.
    blocked = web.post('/api/ibkr/flex/sync', json=payload)
    assert blocked.status_code == 500, blocked.text
    assert 'verified existing conid identity' in blocked.text
    with conn.cursor() as cur:
        cur.execute("SELECT quantity,cost,market_value FROM pams_holdings_snapshot WHERE account_code='ibkr' AND name='Retired'")
        assert cur.fetchone() == (10,80,123)
        cur.execute("SELECT count(*) FROM pams_investment_trade_fact WHERE source_table='ibkr_flex_trade'")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT row_to_json(t)::text FROM pams_holdings_snapshot t WHERE account_code='other'")
        assert cur.fetchall() == protected
    conn.rollback()



@pytest.mark.parametrize('known_old_source', [False, True], ids=['unknown_old_identity', 'different_conid'])
def test_zero_row_does_not_retire_an_unidentified_or_different_contract(sync_case, monkeypatch, known_old_source):
    web, conn, report = sync_case
    params = conn.get_dsn_parameters()
    assert params['host'] in {'127.0.0.1', 'localhost', '::1'}
    assert re.fullmatch(r'pams_import_test_[0-9a-f]{32}', params['dbname'])
    with conn.cursor() as cur:
        cur.execute(sql.SQL("COMMENT ON DATABASE {} IS 'pams-isolated-account-import'").format(sql.Identifier(params['dbname'])))
        cur.execute("INSERT INTO pams_holdings_snapshot(account_code,name,symbol,currency,major_category,as_of,market_value,cost,quantity,asset_liability,minor_category) VALUES ('ibkr','Retired','RETIRE','USD','美股',%s,123,80,10,'资产','stock') RETURNING id", (DAY,))
        old_id = cur.fetchone()[0]
    conn.commit()

    def test_database_only(cur):
        details = cur.connection.get_dsn_parameters()
        assert details['host'] in {'127.0.0.1', 'localhost', '::1'}
        assert re.fullmatch(r'pams_import_test_[0-9a-f]{32}', details['dbname'])
        cur.execute("SELECT shobj_description(oid, 'pg_database') AS marker FROM pg_database WHERE datname=current_database()")
        assert cur.fetchone()['marker'] == 'pams-isolated-account-import'
        return True

    monkeypatch.setattr(isolation, 'source_enabled', test_database_only)
    monkeypatch.setattr(holding_evidence_writer, 'source_enabled', test_database_only)
    monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED', 'true')
    monkeypatch.setenv('PAMS_HOLDING_EVIDENCE_ISOLATED', 'true')
    source_params = {**parse_dsn(os.environ['PAMS_TEST_POSTGRES_DSN']), 'dbname': params['dbname']}
    position = re.search(r'<OpenPosition\b[^>]*/>', XML).group()
    zero = ZERO.replace('symbol="TEST"', 'symbol="RETIRE" conid="2002"').replace('marketValue="0"', 'marketValue="0" positionValue="0"')
    report['xml'] = XML.replace(position, '<OpenPositions>' + position + zero + '</OpenPositions>').replace('ibCommission="-1.25"', 'ibCommission="-1.25" ibCommissionCurrency="USD" taxAmount="0"')
    if known_old_source:
        old_position = zero.replace('conid="2002"', 'conid="1001"').replace('position="0"', 'position="10"').replace('costBasisMoney="0"', 'costBasisMoney="80"').replace('marketValue="0" positionValue="0"', 'marketValue="123" positionValue="123"')
        old_report = report['xml'].replace(zero, old_position)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            old_staged = stage_report(cur, batch_id=uuid4(), account_code='ibkr', source_account_id='U_TEST', business_date=DAY, content=old_report.encode())
        conn.commit()
        confirm(source_params, old_staged)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            with pytest.raises(ValueError, match='conflicts with holding'):
                holding_evidence_writer.finish_holding_change(cur, holding_id=old_id, source_item_id=old_staged['position_item_ids'][1])
        conn.rollback()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        staged = stage_report(cur, batch_id=uuid4(), account_code='ibkr', source_account_id='U_TEST', business_date=DAY, content=report['xml'].encode())
    conn.commit()
    confirm(source_params, staged)
    payload = {'holding_retirement_sources': {'Retired': {'batch_id': staged['batch_id'], 'item_id': staged['position_item_ids'][1]}}}
    with pytest.raises(ValueError, match='verified existing conid'):
        holding_write_evidence.apply_ibkr_retirements(conn, [{'id': old_id}])
    with pytest.raises(ValueError, match='verified existing conid'):
        holding_write_evidence.finish_ibkr_retirements(conn, [{'id': old_id}])
    result = web.post('/api/ibkr/flex/sync', json=payload)
    assert result.status_code == 500 and 'verified existing conid identity' in result.text
    with conn.cursor() as cur:
        cur.execute("SELECT quantity,cost,market_value FROM pams_holdings_snapshot WHERE id=%s", (old_id,))
        assert cur.fetchone() == (10,80,123)
        cur.execute("SELECT count(*) FROM pams_investment_trade_fact WHERE source_table='ibkr_flex_trade'")
        assert cur.fetchone()[0] == 0
    conn.rollback()
    from domain.investments.ibkr_closed_evidence import verified_ibkr_closed_positions
    with psycopg2.connect(**source_params) as read_conn:
        read_conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with read_conn.cursor(cursor_factory=RealDictCursor) as cur:
            assert not [row for row in verified_ibkr_closed_positions(cur) if row['name']=='Retired']
    import main
    monkeypatch.setattr(main,'get_conn',lambda:psycopg2.connect(**source_params))
    monkeypatch.setattr(main,'_holdings_market_view_as_of',lambda cur:DAY)
    monkeypatch.setattr(main,'_daily_pnl_view_as_of',lambda:DAY)
    monkeypatch.setattr(main,'_excluded_holding_accounts',lambda:set())
    monkeypatch.setattr(main,'_account_owner_ratios',lambda:{})
    monkeypatch.setattr(main,'_default_owner_ratio',lambda:1)
    with TestClient(main.app) as holdings_web:
        response=holdings_web.get('/holdings/latest?includeClosed=true')
        assert response.status_code==200,response.text
        assert not [row for row in response.json()['rows'] if row['name']=='Retired' and row.get('is_closed')]


def test_new_schema_defaults_to_source_sync_and_replay(sync_case, monkeypatch):
    web, conn, report = sync_case
    params = conn.get_dsn_parameters()
    assert params['host'] in {'127.0.0.1', 'localhost', '::1'}
    assert re.fullmatch(r'pams_import_test_[0-9a-f]{32}', params['dbname'])
    with conn.cursor() as cur:
        cur.execute(sql.SQL("COMMENT ON DATABASE {} IS 'pams-isolated-account-import'").format(sql.Identifier(params['dbname'])))
        cur.execute('DELETE FROM pams_holdings_snapshot WHERE account_code=%s', ('ibkr',))
    conn.commit()

    def test_database_only(cur):
        details = cur.connection.get_dsn_parameters()
        assert details['host'] in {'127.0.0.1', 'localhost', '::1'}
        assert re.fullmatch(r'pams_import_test_[0-9a-f]{32}', details['dbname'])
        cur.execute("SELECT shobj_description(oid, 'pg_database') AS marker FROM pg_database WHERE datname=current_database()")
        assert cur.fetchone()['marker'] == 'pams-isolated-account-import'
        return True

    monkeypatch.setattr(isolation, 'source_enabled', test_database_only)
    monkeypatch.setattr(holding_evidence_writer, 'source_enabled', test_database_only)
    monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED', 'true')
    monkeypatch.setenv('PAMS_HOLDING_EVIDENCE_ISOLATED', 'true')
    report['xml'] = XML.replace('ibCommission="-1.25"', 'ibCommission="-1.25" ibCommissionCurrency="USD" taxAmount="0"')
    first = web.post('/api/ibkr/flex/sync', json={})
    assert first.status_code == 200, first.text
    assert first.json()['source_mode'] == 'unified'
    assert first.json()['trade_fact_backfill']['status'] == 'not_run'
    assert first.json()['sync_run']['publication']['report_trade_sources']['written'] == 1
    again = web.post('/api/ibkr/flex/sync', json={})
    assert again.status_code == 200, again.text
    assert again.json()['sync_run']['publication']['report_trade_sources']['replayed_verified'] == 1
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('pams_ibkr_trades'),to_regclass('pams_ibkr_position_history')")
        assert cur.fetchone() == (None, None)
        cur.execute("SELECT count(*) FROM pams_investment_trade_fact WHERE source_table='ibkr_flex_trade'")
        assert cur.fetchone()[0] == 1
    conn.rollback()
