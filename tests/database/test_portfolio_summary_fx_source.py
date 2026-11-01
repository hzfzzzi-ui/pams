"""Holdings summaries never convert using FX rows without a source."""
# ruff: noqa: F811
from datetime import date, timedelta
from decimal import Decimal

import psycopg2
from fastapi.testclient import TestClient

from scripts.bootstrap_isolated_sources import initialize_isolated_sources
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401


def test_summary_http_requires_fx_source_and_preserves_estimate(empty_source_db, monkeypatch):
    import main
    from domain.investments import portfolio_summary

    day = date(2026, 10, 30)
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO pams_holdings_snapshot
                (name, account_code, asset_liability, currency, major_category, minor_category,
                 market_value, cost, as_of)
                VALUES ('foreign','tonghuashun','资产','USD','A股','A股',100,80,%s)""", (day,))
            cur.execute("""INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source)
                VALUES (%s,'CNY','USD',7,'   ')""", (day,))
    monkeypatch.setattr(main, 'get_conn', lambda: psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(main, '_holdings_market_view_as_of', lambda cur: day)
    monkeypatch.setattr(main, '_excluded_holding_accounts', lambda: set())
    monkeypatch.setattr(main, '_account_owner_ratios', lambda: {})
    monkeypatch.setattr(main, '_default_owner_ratio', lambda: Decimal('1'))
    # Scope evidence is tested independently; this test isolates the FX contract.
    monkeypatch.setattr(portfolio_summary, '_incomplete_snapshot_accounts', lambda *_: set())
    with TestClient(main.app) as web:
        def read():
            total = web.get('/api/holdings/summary?base_currency=CNY')
            accounts = web.get('/api/holdings/by-account?base_currency=CNY')
            assert total.status_code == accounts.status_code == 200, (total.text, accounts.text)
            return total.json(), next(r for r in accounts.json()['by_account'] if r['account_code'] == 'tonghuashun')
        total, account = read()
        assert total['total']['value'] is None and total['fx_status'] == 'incomplete'
        assert account['total_value_base'] is None and account['fx_precision'] == 'unavailable'
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',6,'synthetic')", (day - timedelta(days=1),))
        total, account = read()
        assert Decimal(total['total']['value']) == Decimal(account['total_value_base']) == 600
        assert account['fx_precision'] == 'estimated'
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
            cur.execute("UPDATE pams_fx_rates_daily SET source='synthetic' WHERE as_of=%s AND quote_currency='USD'", (day,))
        total, account = read()
        assert Decimal(total['total']['value']) == Decimal(account['total_value_base']) == 700
        assert account['fx_precision'] == 'daily_exact'


def test_summary_cross_rate_requires_both_sources(empty_source_db, monkeypatch):
    from psycopg2.extras import RealDictCursor
    from domain.investments import portfolio_summary as summary
    day = date(2026, 10, 30)
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""INSERT INTO pams_holdings_snapshot
                (name,account_code,asset_liability,currency,major_category,minor_category,market_value,cost,as_of)
                VALUES ('eur','tonghuashun','资产','EUR','A股','A股',100,80,%s)""", (day,))
            cur.execute("""INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source)
                VALUES (%s,'CNY','EUR',8,'synthetic'),(%s,'CNY','USD',7,NULL)""", (day,day))
            monkeypatch.setattr(summary,'_incomplete_snapshot_accounts',lambda *_:set())
            kw=dict(base_currency='USD',view_as_of=day,owner_ratio_sql='1',owner_ratio_params=())
            assert summary.build_holdings_summary_payload(cur,**kw)['total']['value'] is None
            assert summary.build_holdings_by_account_payload(cur,**kw)['by_account'][0]['total_value_base'] is None
            cur.execute("UPDATE pams_fx_rates_daily SET source='synthetic' WHERE quote_currency='USD'")
            assert Decimal(summary.build_holdings_summary_payload(cur,**kw)['total']['value']) == Decimal('114.29')
            cur.execute("UPDATE pams_fx_rates_daily SET source=NULL WHERE quote_currency='EUR'")
            assert summary.build_holdings_summary_payload(cur,**kw)['total']['value'] is None


def test_nav_fx_timelines_require_finite_positive_source(empty_source_db):
    from psycopg2.extras import RealDictCursor
    from domain.investments.valuation_nav import fx_rate_at, load_fx_timelines
    day = date(2026, 10, 30)
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source)
                VALUES (%s,'CNY','USD',7,' '),(%s,'CNY','EUR',8,'synthetic'),
                       (%s,'CNY','JPY','NaN','synthetic')""",(day,day,day))
            fx = load_fx_timelines(cur,'CNY')
            assert fx_rate_at(fx,'USD',day,'CNY') is None
            assert fx_rate_at(fx,'JPY',day,'CNY') is None
            assert fx_rate_at(fx,'EUR',day,'CNY') == Decimal('8')
            assert fx_rate_at(fx,'CNY',day,'CNY') == Decimal('1')
            # USD/EUR is derived only from same-day sourced CNY legs.
            assert fx_rate_at(load_fx_timelines(cur,'USD'),'EUR',day,'USD') is None
            cur.execute("UPDATE pams_fx_rates_daily SET source='synthetic' WHERE quote_currency='USD'")
            fx_usd=load_fx_timelines(cur,'USD')
            assert fx_rate_at(fx_usd,'EUR',day,'USD')==Decimal('8')/Decimal('7')
            assert fx_rate_at(fx_usd,'CNY',day,'USD')==Decimal('1')/Decimal('7')
            assert fx_rate_at(fx_usd,'EUR',day-timedelta(days=1),'USD') is None


def test_tonghuashun_history_rejects_future_mark_and_converts_base_currency(empty_source_db):
    from psycopg2.extras import RealDictCursor
    from domain.investments.valuation_nav import _tonghuashun_series
    day = date(2026, 10, 30)
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""INSERT INTO pams_investment_trade_fact
                (trade_date,account_code,symbol,currency,trade_type,side,quantity,price,gross_amount,
                 fee_amount,tax_amount,net_cash,source_table,source_id,source_hash,review_status)
                VALUES (%s,'tonghuashun','600001','CNY','buy','buy',10,8,80,0,0,-80,
                        'synthetic','price-test',%s,'reviewed')""",(day,'a'*64))
            cur.execute("""INSERT INTO pams_holdings_snapshot
                (name,symbol,account_code,asset_liability,currency,major_category,minor_category,
                 market_value,cost,quantity,current_price,as_of)
                VALUES ('future','600001','tonghuashun','资产','CNY','A股','A股',1000,80,10,100,%s)""",
                        (day+timedelta(days=1),))
            cur.execute("""INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source)
                VALUES (%s,'CNY','USD',7,'synthetic')""",(day,))
            values=_tonghuashun_series(cur,dates=[day],base_currency='USD',account_owner_ratio=lambda _:Decimal(1))
            assert day in values.unknown_dates
            cur.execute("INSERT INTO pams_ohlcv_daily(symbol,market,trade_date,close) VALUES ('sh600001','CN_A',%s,12)",(day,))
            values=_tonghuashun_series(cur,dates=[day],base_currency='USD',account_owner_ratio=lambda _:Decimal(1))
            assert day in values.unknown_dates  # The synthetic trade has no reviewed source.
            assert abs(values[day] - Decimal(40)/Decimal(7)) < Decimal('1e-25')
            cur.execute("UPDATE pams_fx_rates_daily SET source='' WHERE quote_currency='USD'")
            values=_tonghuashun_series(cur,dates=[day],base_currency='USD',account_owner_ratio=lambda _:Decimal(1))
            assert day in values.unknown_dates
