"""Published TradingView evidence is verified in an actual read-only transaction."""
# ruff: noqa: F811
from uuid import uuid4
from datetime import date
from decimal import Decimal
import psycopg2
from psycopg2.extras import RealDictCursor
from scripts.bootstrap_isolated_sources import initialize_isolated_sources
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from tests.database.test_native_source_http import isolated  # noqa: F401
from tests.imports.test_account_import_publish import client  # noqa: F401


def test_verified_csv_fact_and_tamper(empty_source_db, monkeypatch):
    from domain.imports.tradingview_source import stage_csvs
    from domain.imports.tradingview_source_publish import review_row, publish
    from domain.imports.tradingview_fact_verification import TradingViewFactVerifier
    monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED','true')
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            batch=stage_csvs(cur,batch_id=uuid4(),account_code='tonghuashun',files=[('test.csv',b'Symbol,Side,Qty,Fill Price,Commission,Tax,Closing Time,Note\nSSE:600000,Buy,10,8,1,0,2026-10-01 12:00:00,first\n')])['batch_id']
            cur.execute('SELECT revision FROM source_batches WHERE batch_id=%s',(batch,)); rev=cur.fetchone()['revision']
            cur.execute('SELECT source_item_id FROM source_items WHERE batch_id=%s',(batch,)); item=cur.fetchone()['source_item_id']
            rev=review_row(cur,batch_id=batch,item_id=item,expected_revision=rev,note='reviewed')['revision']
            publish(cur,batch_id=batch,confirm=True)
            cur.execute('SELECT trade_id FROM pams_investment_trade_fact WHERE source_item_id=%s',(item,)); ident=cur.fetchone()['trade_id']
            cur.execute("INSERT INTO pams_ohlcv_daily(symbol,market,trade_date,close) VALUES ('sh600000','CN_A','2026-09-30',9)")
        conn.commit()
    def read():
        with psycopg2.connect(**empty_source_db) as conn:
            conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                verified=TradingViewFactVerifier(cur).verify_many([ident])[ident]
                from domain.investments.valuation_nav import _tonghuashun_series_from_trade_fact
                day=date(2026,10,1)
                values=_tonghuashun_series_from_trade_fact(cur,dates=[day],account_owner_ratio=lambda _:Decimal(1))
                return verified,values
    verified,values=read()
    assert verified is True and not values.unknown_dates and values[date(2026,10,1)]==Decimal('9')
    assert date(2026,10,1) in values.estimated_dates
    from domain.investments.valuation_nav import build_weekly_nav_rows
    output=build_weekly_nav_rows(dates=[date(2026,10,1)],bank_series={},snapshot_series={},a_share_series=values,ibkr_values={})
    assert output[0]['valuation_quality']=='estimated' and output[0]['estimated_accounts']==['tonghuashun']
    with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as writer:
        writer.execute("INSERT INTO pams_ohlcv_daily(symbol,market,trade_date,close) VALUES ('sh600000','CN_A','2026-10-01',9)")
    assert not read()[1].estimated_dates
    with psycopg2.connect(**empty_source_db) as read_conn:
        read_conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with read_conn.cursor(cursor_factory=RealDictCursor) as cur:
            verifier=TradingViewFactVerifier(cur)
            assert verifier.verify_many([ident])[ident] is True
            with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as writer:
                writer.execute('UPDATE pams_investment_trade_fact SET fee_amount=fee_amount+1 WHERE trade_id=%s',(ident,))
            assert verifier.verify_many([ident])[ident] is True  # Same repeatable-read snapshot.
            read_conn.commit()
            assert verifier.verify_many([ident])[ident] is False
            read_conn.rollback()
            assert verifier.verify_many([ident])[ident] is False
    verified,values=read()
    assert verified is False and date(2026,10,1) in values.unknown_dates


def test_native_published_trade_is_verified_in_history(isolated):
    from tests.imports.test_account_import_publish import DAY, draft, preview, publish, trade, sql
    from domain.investments.valuation_nav import _tonghuashun_series_from_trade_fact
    web,params=isolated
    batch=draft(web,[trade()]); response=publish(web,batch,preview(web,batch))
    assert response.status_code==200,response.text
    ident=sql(params,'SELECT trade_id FROM pams_investment_trade_fact')[0]['trade_id']
    sql(params,"CREATE TABLE pams_ohlcv_daily(symbol text,market text,trade_date date,close numeric)")
    sql(params,"CREATE TABLE pams_fx_rates_daily(as_of date,base_currency text,quote_currency text,rate numeric,source text)")
    sql(params,"INSERT INTO pams_ohlcv_daily(symbol,market,trade_date,close) VALUES ('sz000001','CN_A',%s,12)",(DAY,))
    day=date.fromisoformat(DAY)
    def read():
        with psycopg2.connect(**params) as conn:
            conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                return _tonghuashun_series_from_trade_fact(cur,dates=[day],account_owner_ratio=lambda _:Decimal(1))
    values=read()
    assert not values.unknown_dates and values[day]==Decimal('195')
    sql(params,'UPDATE pams_investment_trade_fact SET net_cash=-1004 WHERE trade_id=%s',(ident,))
    assert day in read().unknown_dates


def test_batch_verification_reads_original_a_constant_number_and_parses_once(empty_source_db, monkeypatch):
    from domain.imports.tradingview_source import stage_csvs
    from domain.imports.tradingview_source_publish import review_row, publish
    from domain.imports.tradingview_fact_verification import TradingViewFactVerifier
    from unittest.mock import patch
    import domain.imports.tradingview_fact_verification as verification
    monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED','true')
    header='Symbol,Side,Qty,Fill Price,Commission,Tax,Closing Time,Note\n'
    rows=''.join(f'SSE:60000{i},Buy,10,8,1,0,2026-10-01 12:00:00,row{i}\n' for i in range(5))
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            batch=stage_csvs(cur,batch_id=uuid4(),account_code='tonghuashun',files=[('test.csv',(header+rows).encode())])['batch_id']
            cur.execute('SELECT revision FROM source_batches WHERE batch_id=%s',(batch,)); rev=cur.fetchone()['revision']
            cur.execute('SELECT source_item_id FROM source_items WHERE batch_id=%s ORDER BY source_order',(batch,))
            for item in cur.fetchall():
                rev=review_row(cur,batch_id=batch,item_id=item['source_item_id'],expected_revision=rev,note='checked')['revision']
            publish(cur,batch_id=batch,confirm=True)
            cur.execute('SELECT trade_id FROM pams_investment_trade_fact ORDER BY trade_id')
            ids=[row['trade_id'] for row in cur.fetchall()]
        conn.commit()
    class CountingCursor(RealDictCursor):
        def execute(self,query,params=None):
            if 'SELECT content FROM source_files' in query or 'SELECT file_id,filename,file_format,content_type,sha256,content' in query:
                self.connection.file_reads += 1
            return super().execute(query,params)
    from psycopg2.extensions import connection as BaseConnection
    class CountingConnection(BaseConnection):
        file_reads=0
    with psycopg2.connect(**empty_source_db,connection_factory=CountingConnection) as conn:
        conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=CountingCursor) as cur,patch.object(verification,'_prepare',wraps=verification._prepare) as parse:
            assert all(TradingViewFactVerifier(cur).verify_many(ids).values())
            assert conn.file_reads==2 and parse.call_count==1
