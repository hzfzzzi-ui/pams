"""Published isolated Flex runs read the single database original, not disk."""
# ruff: noqa: F811
from datetime import date,datetime,timezone
from hashlib import sha256
import re

import psycopg2
import pytest
from fastapi.testclient import TestClient
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from tests.brokers.ibkr.test_ibkr_sync_transaction import XML
from scripts.bootstrap_isolated_sources import initialize_isolated_sources


@pytest.mark.parametrize('include_trade',[True,False],ids=['with_trade','holdings_only'])
def test_source_report_selected_by_run_and_verified_in_real_http(empty_source_db,monkeypatch,tmp_path,include_trade):
    import main
    from collectors import ibkr_flex
    from domain.brokers.ibkr import ibkr_report_state, ibkr_performance, ibkr_daily_close_read
    from endpoints import ibkr
    day=date(2026,10,22)
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'synthetic')",(day,))
    monkeypatch.setenv('PAMS_HOLDING_EVIDENCE_ISOLATED','true')
    monkeypatch.delenv('PAMS_IBKR_CASH_EVENTS_AFTER',raising=False)
    monkeypatch.setenv('PAMS_DATA_DIR',str(tmp_path))
    for module in (main,ibkr,ibkr_performance): monkeypatch.setattr(module,'get_conn',lambda:psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(ibkr,'_credentials',lambda:('token','query'))
    monkeypatch.setattr(ibkr_flex,'request_flex_reference',lambda *a,**k:{'reference_code':'TEST'})
    monkeypatch.setattr(ibkr_report_state,'utc_now',lambda:datetime(2026,10,22,22,tzinfo=timezone.utc))
    monkeypatch.setattr(ibkr_daily_close_read,'configured',lambda:False)
    raw=XML.replace('ibCommission="-1.25"','ibCommission="-1.25" ibCommissionCurrency="USD" taxAmount="0"')
    raw=raw.replace('<FlexStatement accountId="U_TEST" toDate="20261022">',
        '<FlexStatement accountId="U_TEST" toDate="20261022"><AccountInformation accountId="U_TEST" currency="USD" />')
    if not include_trade:
        raw=re.sub(r'<Trade\b[^>]*/>','',raw)
    monkeypatch.setattr(ibkr_flex,'fetch_flex_statement',lambda *a,**k:raw)
    with TestClient(main.app) as web:
        first=web.post('/api/ibkr/flex/sync',json={})
        assert first.status_code==200,first.text
        batch=first.json()['sync_run']['publication']['report_trade_sources']['batch_id']
        assert first.json()['raw_file']=='source:'+batch
        assert not list(tmp_path.rglob('*.xml'))
        with psycopg2.connect(**empty_source_db) as conn:
            conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
            report,_=ibkr_report_state.select_report(conn,tmp_path)
            assert report and report.path.read_bytes()==raw.encode() and report.sha256==sha256(raw.encode()).hexdigest()
            from domain.brokers.ibkr.ibkr_daily_close_read import read_bounded
            assert read_bounded(report.path,32*1024*1024)==raw.encode()
        with psycopg2.connect(**empty_source_db) as conn:
            conn.set_session(readonly=True,isolation_level='READ COMMITTED')
            assert ibkr_report_state.select_report(conn,tmp_path)[0] is None
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            with pytest.raises(psycopg2.errors.RaiseException):
                cur.execute("UPDATE broker_processing_state SET source_sha256=%s WHERE state_type='publication'", ('0'*64,))
            conn.rollback()
            # A privileged adversarial change is tested only in this disposable
            # transaction; a mismatched immutable publication must not resolve.
            cur.execute('ALTER TABLE broker_processing_state DISABLE TRIGGER USER')
            cur.execute("UPDATE broker_processing_state SET source_sha256=%s WHERE state_type='publication'", ('0'*64,))
            cur.execute('ALTER TABLE broker_processing_state ENABLE TRIGGER USER')
            assert ibkr_report_state.select_report(conn,tmp_path)[0] is None
            conn.rollback()
        performance=web.get('/api/ibkr/performance')
        assert performance.status_code==200,performance.text
        assert performance.json()['status']=='ok'
        from domain.brokers.ibkr import ibkr_daily_close_sync, ibkr_account_audit_read, ibkr_reconciliation
        subject,_,scope=ibkr_daily_close_sync._production_context()
        assert subject and scope==('U_TEST','USD') and subject.path.read_bytes()==raw.encode()
        audit=ibkr_account_audit_read.read_ibkr_account_audit(connection_factory=lambda:psycopg2.connect(**empty_source_db),raw_dir=tmp_path)
        assert audit['adapter']['original_sha256']==sha256(raw.encode()).hexdigest()
        assert audit['adapter']['history_dates_source']=='unavailable_without_official_daily_coverage'
        assert audit['adapter']['history_date_count']==0
        reconciliation=ibkr_reconciliation.read_ibkr_reconciliation(connection_factory=lambda:psycopg2.connect(**empty_source_db),raw_dir=tmp_path)
        assert reconciliation['report']['sha256']==sha256(raw.encode()).hexdigest()
        with monkeypatch.context() as disabled:
            disabled.setenv('PAMS_UNIFIED_SOURCE_ISOLATED','false')
            assert web.get('/api/ibkr/performance').json()['status']=='ok'
            blocked=web.post('/api/ibkr/flex/sync',json={})
            assert blocked.status_code>=400
            with psycopg2.connect(**empty_source_db) as conn:
                conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
                selected,_=ibkr_report_state.select_report(conn,tmp_path)
                assert selected and selected.path.read_bytes()==raw.encode()
        replay=web.post('/api/ibkr/flex/sync',json={})
        assert replay.status_code==200,replay.text
        assert replay.json()['raw_file']=='source:'+batch and not list(tmp_path.rglob('*.xml'))
    with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
        cur.execute('ALTER TABLE source_files DISABLE TRIGGER USER')
        cur.execute("UPDATE source_files SET content=decode('00','hex')")
        cur.execute('ALTER TABLE source_files ENABLE TRIGGER USER')
    with psycopg2.connect(**empty_source_db) as conn:
        conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
        report,_=ibkr_report_state.select_report(conn,tmp_path)
        assert report is None
        assert ibkr_report_state.publication_floor(conn)==day


def test_publication_date_order_corruption_fallback_and_watermark(empty_source_db, monkeypatch, tmp_path):
    """A later publication revision cannot reorder dates or lower the business watermark."""
    import main
    from collectors import ibkr_flex
    from domain.brokers.ibkr import ibkr_report_state
    from endpoints import ibkr
    old_day, new_day = date(2026, 10, 21), date(2026, 10, 22)
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'synthetic')", [(old_day,), (new_day,)])
    monkeypatch.setenv('PAMS_HOLDING_EVIDENCE_ISOLATED', 'true')
    monkeypatch.setenv('PAMS_DATA_DIR', str(tmp_path))
    monkeypatch.delenv('PAMS_IBKR_CASH_EVENTS_AFTER', raising=False)
    for module in (main, ibkr):
        monkeypatch.setattr(module, 'get_conn', lambda: psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(ibkr, '_credentials', lambda: ('token', 'query'))
    monkeypatch.setattr(ibkr_flex, 'request_flex_reference', lambda *a, **k: {'reference_code':'TEST'})
    raw = re.sub(r'<Trade\b[^>]*/>', '', XML)
    reports = {day: raw.replace('20261022', day.strftime('%Y%m%d')) for day in (old_day, new_day)}
    current = {'day': old_day}
    monkeypatch.setattr(ibkr_flex, 'fetch_flex_statement', lambda *a, **k: reports[current['day']])
    monkeypatch.setattr(ibkr_report_state, 'utc_now', lambda: datetime.combine(current['day'], datetime.min.time(), tzinfo=timezone.utc).replace(hour=22))
    with TestClient(main.app) as web:
        batch_ids = {}
        for day in (old_day, new_day):
            current['day'] = day
            response = web.post('/api/ibkr/flex/sync', json={})
            assert response.status_code == 200, response.text
            batch_ids[day] = response.json()['sync_run']['publication']['report_trade_sources']['batch_id']
    with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
        insert_revision = """INSERT INTO broker_processing_state
            (state_type,account_code,source_batch_id,statement_date,source_sha256,replay_fingerprint,
             published_result_sha256,holding_count,cash_state,cash_state_sha256)
            SELECT state_type,account_code,source_batch_id,statement_date,source_sha256,replay_fingerprint,
                   published_result_sha256,holding_count,cash_state,cash_state_sha256
            FROM broker_processing_state WHERE state_type='publication' AND source_batch_id=%s LIMIT 1 RETURNING publication_id"""
        cur.execute(insert_revision, (batch_ids[new_day],))
        new_revision_id = cur.fetchone()[0]
        cur.execute(insert_revision, (batch_ids[old_day],))
    with psycopg2.connect(**empty_source_db) as conn:
        conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
        selected, _ = ibkr_report_state.select_report(conn, tmp_path)
        assert selected and selected.as_of == new_day and selected.run_id == new_revision_id
        assert selected.path.read_bytes() == reports[new_day].encode()
        assert ibkr_report_state.publication_floor(conn) == new_day
    with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
        cur.execute('ALTER TABLE source_files DISABLE TRIGGER USER')
        cur.execute("UPDATE source_files SET content=content || decode('00','hex') WHERE batch_id=%s", (batch_ids[new_day],))
        cur.execute('ALTER TABLE source_files ENABLE TRIGGER USER')
    with psycopg2.connect(**empty_source_db) as conn:
        conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
        selected, latest = ibkr_report_state.select_report(conn, tmp_path)
        assert selected and selected.as_of == old_day and selected.path.read_bytes() == reports[old_day].encode()
        assert ibkr_report_state.report_quality(selected, latest, expected=old_day)['status'] == 'review'
        assert ibkr_report_state.publication_floor(conn) == new_day
        with pytest.raises(ibkr_report_state.ReportRejected, match='regression'):
            ibkr_report_state.validate_candidate(old_day, old_day, selected, minimum=new_day,
                now=datetime(2026, 10, 22, 22, tzinfo=timezone.utc))
