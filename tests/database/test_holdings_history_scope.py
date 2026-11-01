"""Real HTTP history preserves mixed-date positions and marks partial scope."""
# ruff: noqa: F811
from datetime import date, timedelta
from decimal import Decimal

import psycopg2
from fastapi.testclient import TestClient
from scripts.bootstrap_isolated_sources import initialize_isolated_sources
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401


def test_partial_history_preserves_each_position_and_fx_evidence(empty_source_db, monkeypatch):
    import main
    day = date(2026, 10, 30)
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO pams_holdings_snapshot
                (name,symbol,account_code,asset_liability,currency,major_category,minor_category,market_value,cost,as_of)
                VALUES ('A','A','ibkr','资产','USD','美股','美股',10,10,%s),
                       ('B','B','ibkr','资产','USD','美股','美股',20,20,%s),
                       ('C','C','ibkr','资产','CNY','美股','美股',70,70,%s),
                       ('A','A','ibkr','资产','USD','美股','美股',11,10,%s)""",
                        (day-timedelta(days=1),day-timedelta(days=1),day-timedelta(days=1),day))
            cur.execute("""INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source)
                VALUES (%s,'CNY','USD',7,'synthetic'),(%s,'CNY','EUR',14,'synthetic')""", (day,day))
    monkeypatch.setattr(main,'get_conn',lambda:psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(main,'_view_as_of',lambda:day)
    monkeypatch.setattr(main,'_owner_ratio_case_sql',lambda alias:('1',[]))
    monkeypatch.setattr(main,'_account_owner_ratio',lambda code:Decimal(1))
    monkeypatch.setattr(main,'_excluded_holding_accounts',lambda:set())
    with TestClient(main.app) as web:
        response=web.get('/holdings/history/美股',params={'days':3,'baseCurrency':'EUR'})
        assert response.status_code==200,response.text
        row=response.json()['rows'][-1]
        assert row['value'] is None and row['scope_status']=='incomplete'
        assert Decimal(str(row['known_value']))==Decimal('20.5'),response.text
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            cur.execute("UPDATE pams_fx_rates_daily SET source='' WHERE quote_currency='USD' AND as_of=%s",(day,))
        response=web.get('/holdings/history/美股',params={'days':3,'baseCurrency':'EUR'})
        assert response.status_code==200,response.text
        row=response.json()['rows'][-1]
        assert row['value'] is None and Decimal(str(row['known_value']))==Decimal('5')
