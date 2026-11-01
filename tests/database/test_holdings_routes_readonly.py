"""Actual holdings/summary APIs use one server-enforced stable snapshot."""
# ruff: noqa: F811
from datetime import date
import psycopg2
from psycopg2.extensions import connection, ISOLATION_LEVEL_REPEATABLE_READ
from fastapi.testclient import TestClient
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from scripts.bootstrap_isolated_sources import initialize_isolated_sources


def test_holdings_and_summary_http_are_readonly_repeatable_read(empty_source_db,monkeypatch):
    import main
    seen=[]
    class CheckedConnection(connection):
        def cursor(self,*args,**kwargs):
            assert self.readonly is True
            assert self.isolation_level==ISOLATION_LEVEL_REPEATABLE_READ
            seen.append(1)
            return super().cursor(*args,**kwargs)
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
    monkeypatch.setattr(main,'get_conn',lambda:psycopg2.connect(**empty_source_db,connection_factory=CheckedConnection))
    monkeypatch.setattr(main,'_holdings_market_view_as_of',lambda cur:date(2026,10,22))
    monkeypatch.setattr(main,'_view_as_of',lambda:date(2026,10,22))
    with TestClient(main.app) as web:
        for url in ('/api/holdings/latest','/api/holdings/summary','/api/holdings/by-account',
                    '/summary2/latest','/holdings/history/A股','/nav/series'):
            response=web.get(url)
            assert response.status_code==200,(url,response.text)
    assert len(seen)>=6


def test_holdings_http_does_not_mix_concurrent_commits(empty_source_db,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    import psycopg2.extras
    import main
    from scripts.bootstrap_isolated_sources import initialize_isolated_sources
    entered=Event(); release=Event(); paused=Event()
    class PausingCursor(psycopg2.extras.RealDictCursor):
        def execute(self,query,params=None):
            result=super().execute(query,params)
            if not paused.is_set():
                paused.set(); entered.set()
                assert release.wait(10)
            return result
    class PausingConnection(connection):
        def cursor(self,*args,**kwargs):
            kwargs['cursor_factory']=PausingCursor
            return super().cursor(*args,**kwargs)
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
    monkeypatch.setattr(main,'get_conn',lambda:psycopg2.connect(**empty_source_db,connection_factory=PausingConnection))
    monkeypatch.setattr(main,'_holdings_market_view_as_of',lambda cur:date(2026,10,22))
    with TestClient(main.app) as web,ThreadPoolExecutor(max_workers=1) as executor:
        reading=executor.submit(web.get,'/api/holdings/latest')
        assert entered.wait(8)
        with psycopg2.connect(**empty_source_db) as writer,writer.cursor() as cur:
            cur.execute("""INSERT INTO pams_holdings_snapshot(account_code,name,symbol,currency,asset_liability,
                major_category,minor_category,as_of,quantity,cost,market_value)
                VALUES ('ibkr','Concurrent synthetic','SYN','CNY','资产','其他','测试',%s,1,10,10)""",(date(2026,10,22),))
        release.set()
        before=reading.result(timeout=10)
        assert before.status_code==200 and before.json()['rows']==[],before.text
        after=web.get('/api/holdings/latest')
        assert after.status_code==200 and any(row['symbol']=='SYN' for row in after.json()['rows']),after.text


def test_snapshot_post_conflict_rolls_back_and_returns_retryable_response(empty_source_db,monkeypatch):
    import main
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
    monkeypatch.setattr(main,'get_conn',lambda:psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(main.accounts_endpoint,'_overview_payload',lambda cur=None:{'accounts':[]})
    monkeypatch.setattr(main,'_investment_return_view_as_of',lambda cur:date(2026,10,22))
    payload={'rows':[],'as_of':'2026-10-22'}
    monkeypatch.setattr(main,'_build_investment_return_series_payload',lambda *args,**kwargs:payload)
    monkeypatch.setattr(main,'_build_section_return_series_payload',lambda *args,**kwargs:payload)
    def fail_after_write(cur,**kwargs):
        cur.execute("INSERT INTO pams_fx_rates_daily(base_currency,quote_currency,as_of,rate,source) VALUES ('CNY','USD',%s,7,'test')",(date(2026,10,22),))
        raise psycopg2.errors.SerializationFailure('synthetic secret')
    monkeypatch.setattr(main,'_upsert_investment_return_series_snapshot',fail_after_write)
    with TestClient(main.app) as web:
        for url in ('/investment/return-series/snapshot?start=2026-10-22',
                    '/investment/section-return-series/snapshot?section=us_stock&start=2026-10-22'):
            response=web.post(url)
            assert response.status_code==409 and 'synthetic secret' not in response.text,response.text
            with psycopg2.connect(**empty_source_db) as check,check.cursor() as cur:
                cur.execute("SELECT count(*) FROM pams_fx_rates_daily WHERE source='test'")
                assert cur.fetchone()[0]==0
