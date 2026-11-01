"""Holdings FX requires dated source evidence and preserves unknowns."""
# ruff: noqa: F811
from datetime import date, timedelta
from decimal import Decimal
from fastapi.testclient import TestClient
import psycopg2
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from scripts.bootstrap_isolated_sources import initialize_isolated_sources


def test_current_and_historical_holdings_require_fx_source(empty_source_db,monkeypatch):
    import main
    from domain.brokers.ibkr import ibkr_daily_close_read
    day=date(2026,10,22); previous=day-timedelta(days=1)
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO pams_holdings_snapshot
                (name,symbol,account_code,asset_liability,currency,major_category,minor_category,
                 market_value,cost,quantity,current_price,as_of) VALUES
                ('stock','SYN','ibkr','资产','USD','美股','股票',90,80,10,9,%s),
                ('stock','SYN','ibkr','资产','USD','美股','股票',100,80,10,10,%s),
                ('old','OLD','ibkr','资产','USD','美股','股票',20,10,2,10,%s),
                ('zero','ZERO','ibkr','资产','JPY','债券','债券',0,0,0,0,%s)""",(previous,day,previous,day))
            cur.execute("""INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES
                (%s,'CNY','USD',7,''),(%s,'CNY','USD',7.2,''),(%s,'CNY','USD',8,'future')""",
                (previous,day,day+timedelta(days=1)))
    monkeypatch.setattr(main,'get_conn',lambda:psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(main,'_holdings_market_view_as_of',lambda cur:day)
    monkeypatch.setattr(main,'_view_as_of',lambda:day)
    monkeypatch.setattr(main,'_daily_pnl_view_as_of',lambda:day)
    monkeypatch.setattr(main,'_excluded_holding_accounts',lambda:set())
    monkeypatch.setattr(main,'_account_owner_ratios',lambda:{})
    monkeypatch.setattr(main,'_default_owner_ratio',lambda:Decimal(1))
    monkeypatch.setattr(main,'_owner_ratio_case_sql',lambda alias:('%s',[Decimal(1)]))
    monkeypatch.setattr(main,'_account_owner_ratio',lambda account:Decimal(1))
    monkeypatch.setattr(ibkr_daily_close_read,'configured',lambda:False)
    with TestClient(main.app) as web:
        def current(path='/holdings/latest'):
            response=web.get(path)
            assert response.status_code==200,response.text
            return {row['symbol']:row for row in response.json()['rows']}
        def history(category):
            response=web.get('/holdings/history/'+category,params={'days':10,'interval':'daily'})
            assert response.status_code==200,response.text
            return {row['date']:row for row in response.json()['rows']}
        for path in ('/holdings/latest','/holdings/by-category/美股'):
            row=current(path)['SYN']
            assert Decimal(row['market_value'])==100 and row['market_value_cny'] is None,row
            assert row['fx_precision']=='unavailable' and row['fx_rate_as_of'] is None
            assert Decimal(row['daily_pnl'])==10 and row['daily_pnl_cny'] is None,row
        assert history('美股')[day.isoformat()]['value'] is None
        zero = history('其他')[day.isoformat()]
        assert zero['value'] is None and zero['known_value'] == 0
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            cur.execute("UPDATE pams_fx_rates_daily SET source='synthetic' WHERE quote_currency='USD' AND as_of=%s",(previous,))
        row=current()['SYN']
        assert Decimal(row['market_value_cny'])==700 and row['fx_precision']=='estimated',row
        assert row['fx_rate_as_of']==previous.isoformat()
        assert history('美股')[day.isoformat()]['value'] is None
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            cur.execute("UPDATE pams_fx_rates_daily SET source='synthetic' WHERE quote_currency='USD' AND as_of=%s",(day,))
        rows=current()
        assert Decimal(rows['SYN']['market_value_cny'])==720 and rows['SYN']['fx_precision']=='daily_exact'
        assert Decimal(rows['OLD']['market_value_cny'])==140 and rows['OLD']['fx_rate_as_of']==previous.isoformat()
        partial = history('美股')[day.isoformat()]
        assert partial['value'] is None and Decimal(str(partial['known_value'])) == Decimal('864')
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            cur.execute("UPDATE pams_fx_rates_daily SET source='' WHERE quote_currency='USD' AND as_of=%s",(previous,))
        row=current()['SYN']
        assert Decimal(row['daily_pnl_cny'])==72 and row['daily_fx_pnl_cny'] is None,row
        assert row['daily_total_pnl_cny'] is None,row
