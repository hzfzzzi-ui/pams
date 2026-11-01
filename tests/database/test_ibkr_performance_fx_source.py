"""Display FX must have an observed source and cannot come from the future."""
# ruff: noqa: F811
from datetime import date, timedelta
from decimal import Decimal
import psycopg2
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from scripts.bootstrap_isolated_sources import initialize_isolated_sources


def test_broker_display_fx_requires_source_and_never_uses_future(empty_source_db, monkeypatch):
    from domain.brokers.ibkr import ibkr_performance as performance
    today = date.today()
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source)
                VALUES (%s,'CNY','USD',7,''),(%s,'CNY','USD',8,'synthetic')""", (today, today+timedelta(days=1)))
    monkeypatch.setattr(performance, 'get_conn', lambda: psycopg2.connect(**empty_source_db))
    assert performance._latest_usd_cny_rate() is None
    with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
        cur.execute("""INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source)
            VALUES (%s,'CNY','USD',6,'synthetic')""", (today-timedelta(days=1),))
    assert performance._latest_usd_cny_rate() == Decimal('6')
