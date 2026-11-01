"""One isolated report keeps one raw XML source and many trade rows."""
# ruff: noqa: F811
from datetime import date
from hashlib import sha256
import psycopg2
from psycopg2.extras import RealDictCursor
import pytest
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from scripts.bootstrap_isolated_sources import initialize_isolated_sources
from tests.brokers.ibkr.test_ibkr_sync_transaction import XML
from domain.brokers.ibkr.ibkr_report_batch import publish_report_batch

DAY=date(2026,10,22)

def report():
    return XML.replace('ibCommission="-1.25"','ibCommission="-1.25" ibCommissionCurrency="USD" taxAmount="0"').encode()


def execute(cur, content, *, replay=False):
    return publish_report_batch(cur,report_bytes=content,source_account_id='U_TEST',day=DAY,
        report_sha256=sha256(content).hexdigest(),replay=replay)


def test_one_report_one_file_replay_and_atomic_rollback(empty_source_db):
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate) VALUES (%s,'CNY','USD',7)",(DAY,))
        conn.commit()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            result=execute(cur,report()); assert result['written']==1
            cur.execute('SELECT count(*) AS n FROM source_files'); assert cur.fetchone()['n']==1
            cur.execute('SELECT count(*) AS n FROM source_batches'); assert cur.fetchone()['n']==1
            cur.execute('SELECT count(*) AS n FROM source_items'); assert cur.fetchone()['n']==2
            cur.execute('SELECT content FROM source_files'); assert bytes(cur.fetchone()['content'])==report()
            conn.rollback()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT count(*) AS n FROM source_batches'); assert cur.fetchone()['n']==0
            cur.execute('SELECT count(*) AS n FROM pams_investment_trade_fact'); assert cur.fetchone()['n']==0
            result=execute(cur,report()); assert result['written']==1
        conn.commit()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            result=execute(cur,report(),replay=True)
            assert result['replayed'] and result['existing_verified']==1


def test_cross_report_same_official_trade_reuses_fact_without_false_new_link(empty_source_db):
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate) VALUES (%s,'CNY','USD',7)",(DAY,))
            first=execute(cur,report()); assert first['written']==1
        conn.commit()
        second_xml=report()+b'\n'
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            second=execute(cur,second_xml)
            assert second['written']==0 and second['existing_verified']==1
            assert second['source_contributions']=='not_recorded'
            cur.execute('SELECT count(*) AS n FROM pams_investment_trade_fact'); assert cur.fetchone()['n']==1
            cur.execute('SELECT count(*) AS n FROM source_files'); assert cur.fetchone()['n']==2
            cur.execute('SELECT published_result FROM source_batches WHERE batch_id=%s',(second['batch_id'],))
            saved=cur.fetchone()['published_result']
            assert saved['records']==[] and saved['observations'][0]['role']=='matched_existing_unlinked'
        conn.commit()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            assert execute(cur,second_xml,replay=True)['replayed']
            cur.execute('SAVEPOINT altered')
            cur.execute("UPDATE pams_investment_trade_fact SET net_cash=-1 WHERE source_table='ibkr_flex_trade'")
            with pytest.raises(ValueError,match='replay fact mismatch'): execute(cur,second_xml,replay=True)
            cur.execute('ROLLBACK TO SAVEPOINT altered')
        bad=report().replace(b'quantity="2"',b'quantity="1"').replace(b'tradeMoney="340"',b'tradeMoney="170"').replace(b'netCash="-341.25"',b'netCash="-171.25"')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            with pytest.raises(ValueError,match='matching original source'): execute(cur,bad)
        conn.rollback()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT count(*) AS n FROM source_batches'); assert cur.fetchone()['n']==2
            cur.execute('SELECT count(*) AS n FROM pams_investment_trade_fact'); assert cur.fetchone()['n']==1


def test_bad_xml_and_oversize_are_rejected_before_any_parse_or_write(empty_source_db):
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            unsafe=b'<!DOCTYPE r [<!ENTITY x "bad">]><r>&x;</r>'
            with pytest.raises(ValueError,match='unsafe IBKR report'): execute(cur,unsafe)
            with pytest.raises(ValueError,match='byte limit'): execute(cur,b'x'*(32*1024*1024+1))
            cur.execute('SELECT count(*) AS n FROM source_batches'); assert cur.fetchone()['n']==0


def test_parallel_same_report_is_one_batch_and_one_fact(empty_source_db):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate) VALUES (%s,'CNY','USD',7)",(DAY,))
    barrier=Barrier(2)
    def run(_):
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
            barrier.wait(timeout=10)
            return execute(cur,report())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(run,range(2)))
    assert sorted(r['written'] for r in results)==[0,1]
    assert sum(bool(r['replayed']) for r in results)==1
    with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
        cur.execute('SELECT count(*) FROM source_batches'); assert cur.fetchone()[0]==1
        cur.execute('SELECT count(*) FROM pams_investment_trade_fact'); assert cur.fetchone()[0]==1


def test_report_above_item_limit_is_rejected_without_truncation(empty_source_db):
    import re
    content=report()
    trade=re.search(rb'<Trade\b[^>]*/>',content).group()
    content=content.replace(trade,trade*2001)
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            with pytest.raises(ValueError,match='source item limit'): execute(cur,content)
            cur.execute('SELECT count(*) AS n FROM source_batches'); assert cur.fetchone()['n']==0


def test_report_batch_does_not_consult_obsolete_trade_table(empty_source_db):
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate) VALUES (%s,'CNY','USD',7)",(DAY,))
            cur.execute('CREATE TABLE pams_ibkr_trades (ignored boolean)')
            result=execute(cur,report())
            assert result['written']==1
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM pams_investment_trade_fact WHERE source_table='ibkr_flex_trade'")
            assert cur.fetchone()[0]==1


def test_report_batch_failure_rolls_back_source_file_and_facts(empty_source_db):
    import re
    original=report(); trade=re.search(rb'<Trade\b[^>]*/>',original).group()
    content=original.replace(trade,trade+trade.replace(b'tradeID="T1"',b'tradeID="T2"'))
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate) VALUES (%s,'CNY','USD',7)",(DAY,))
            cur.execute("""CREATE FUNCTION reject_report_item() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN IF NEW.raw_fields->'official_attributes'->>'tradeID'='T2' THEN
                RAISE EXCEPTION 'synthetic source failure'; END IF; RETURN NEW; END $$""")
            cur.execute('CREATE TRIGGER reject_report_item BEFORE INSERT ON source_items FOR EACH ROW EXECUTE FUNCTION reject_report_item()')
        conn.commit()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            with pytest.raises(psycopg2.Error): execute(cur,content)
        conn.rollback()
        with conn.cursor() as cur:
            for table in ('source_batches','source_files','source_items','pams_investment_trade_fact'):
                cur.execute('SELECT count(*) FROM '+table); assert cur.fetchone()[0]==0


def test_multiple_trades_share_one_report_file_and_batch(empty_source_db):
    import re
    original=report(); trade=re.search(rb'<Trade\b[^>]*/>',original).group()
    content=original.replace(trade,trade+trade.replace(b'tradeID="T1"',b'tradeID="T2"'))
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate) VALUES (%s,'CNY','USD',7)",(DAY,))
            result=execute(cur,content)
            assert result['written']==2
            cur.execute('SELECT count(*) AS n FROM source_batches'); assert cur.fetchone()['n']==1
            cur.execute('SELECT count(*) AS n FROM source_files'); assert cur.fetchone()['n']==1
            cur.execute('SELECT count(*) AS n, count(distinct file_id) AS files FROM source_items')
            row=cur.fetchone(); assert row['n']==3 and row['files']==1
        conn.commit()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            assert execute(cur,content,replay=True)['existing_verified']==2


def test_ibkr_fact_cannot_commit_without_published_source(empty_source_db):
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO pams_investment_trade_fact(trade_date,account_code,currency,source_table,source_id,source_hash)
                VALUES (%s,'ibkr','USD','ibkr_flex_cash_transaction','orphan',%s)""",(DAY,'a'*64))
        with pytest.raises(psycopg2.errors.CheckViolation,match='published confirmed source'):
            conn.commit()
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM pams_investment_trade_fact WHERE source_id='orphan'")
            assert cur.fetchone()[0]==0
