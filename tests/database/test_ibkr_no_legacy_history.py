"""An obsolete position table cannot change new asset views."""
# ruff: noqa: F811
from datetime import date
import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi.testclient import TestClient
from scripts.bootstrap_isolated_sources import initialize_isolated_sources
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401


def test_old_position_table_does_not_change_holdings_or_history_http(empty_source_db,monkeypatch):
    import main
    day=date(2026,10,22)
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO pams_accounts(account_code,display_name,base_currency) VALUES ('ibkr','Synthetic','USD') ON CONFLICT DO NOTHING")
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'synthetic')",(day,))
            cur.execute("""INSERT INTO pams_holdings_snapshot(account_code,name,symbol,currency,asset_liability,
                major_category,minor_category,as_of,market_value,cost,quantity,current_price)
                VALUES ('ibkr','Synthetic','TEST','USD',%s,%s,%s,%s,100,80,2,50)""",
                ('\u8d44\u4ea7','\u7f8e\u80a1','\u7f8e\u80a1\u57fa\u91d1',day))
    monkeypatch.setattr(main,'get_conn',lambda:psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(main,'_holdings_market_view_as_of',lambda cur:day)
    monkeypatch.setattr(main,'_daily_pnl_view_as_of',lambda:day)
    monkeypatch.setattr(main,'_view_as_of',lambda:day)
    monkeypatch.setattr(main,'_excluded_holding_accounts',lambda:set())
    monkeypatch.setattr(main,'_account_owner_ratios',lambda:{})
    monkeypatch.setattr(main,'_default_owner_ratio',lambda:1)
    monkeypatch.setattr(main,'_account_owner_ratio',lambda account:1)
    monkeypatch.setattr(main,'_owner_ratio_case_sql',lambda alias:('1',[]))
    with TestClient(main.app) as web:
        paths=('/holdings/latest','/holdings/by-category/\u7f8e\u80a1','/holdings/history/\u7f8e\u80a1')
        def snapshot():
            values=[]
            for path in paths:
                response=web.get(path)
                assert response.status_code==200,response.text
                values.append(response.json())
            return values
        before=snapshot()
        assert before[2]['history_evidence']['coverage']=='unknown'
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            cur.execute('CREATE TABLE pams_ibkr_position_history (obsolete boolean)')
            cur.execute('INSERT INTO pams_ibkr_position_history VALUES (true)')
        assert snapshot()==before


def test_old_position_table_cannot_supply_nav_or_account_identity(empty_source_db,monkeypatch):
    from domain.brokers.ibkr import ibkr_daily_close_read
    from domain.investments.valuation_nav import _ibkr_nav_series, daily_close_account_identity
    day=date(2026,10,22)
    monkeypatch.setattr(ibkr_daily_close_read,'configured',lambda:False)
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            def evaluate():
                values=_ibkr_nav_series(cur,dates=[day],base_currency='CNY',owner_ratio_case_sql=lambda alias:('1',[]))
                identity=daily_close_account_identity(cur,source_account_id='U_TEST',as_of=day)
                return values,identity
            before=evaluate()
            assert before[0][day]['history_evidence']=='unavailable'
            assert before[1]['status']=='unconfirmed'
            cur.execute('CREATE TABLE pams_ibkr_position_history (obsolete boolean)')
            cur.execute('INSERT INTO pams_ibkr_position_history VALUES (true)')
            assert evaluate()==before
