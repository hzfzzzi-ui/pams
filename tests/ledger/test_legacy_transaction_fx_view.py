"""Exercise the legacy transaction FX view only against an isolated PostgreSQL server."""
from decimal import Decimal
import os
from pathlib import Path
from uuid import uuid4

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import parse_dsn
import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def isolated_cursor():
    dsn = os.environ.get("PAMS_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Explicit disposable PostgreSQL DSN required")
    assert parse_dsn(dsn).get("host") in {"127.0.0.1", "localhost", "::1"}
    conn = psycopg2.connect(dsn, connect_timeout=5)
    assert conn.server_version // 10000 == 16
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname=current_database()")
            assert cur.fetchone()[0] == "pams-disposable-schema-test"
            schema = "fx_view_test_" + uuid4().hex
            cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            cur.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(schema)))
            cur.execute("""CREATE TABLE pams_transactions (
                date date, amount numeric, currency varchar(3), account text
            ); CREATE TABLE pams_budgets (currency varchar(3));
            CREATE TABLE pams_fx_rates_daily (
                as_of date, base_currency text, quote_currency text, rate numeric
            );""")
            cur.execute((ROOT / "database/postgres/schema/006_update_accounts.sql").read_text(encoding="utf-8"))
            yield cur
        conn.rollback()
    finally:
        conn.close()


def test_view_uses_latest_non_future_direct_fx(isolated_cursor):
    cur = isolated_cursor
    cur.execute("""INSERT INTO pams_fx_rates_daily VALUES
        ('2026-01-01','USD','CNY',7.1), ('2026-01-02','USD','CNY',7.2),
        ('2026-01-05','USD','CNY',7.9), ('2026-01-02','CNY','USD',0.1)""")
    cur.execute("""INSERT INTO pams_transactions VALUES
        ('2026-01-03',10,'USD','Santander'), ('2026-01-03',10,'CNY','Santander')""")
    cur.execute("SELECT currency, amount_cny FROM v_transactions_cny ORDER BY currency")
    assert cur.fetchall() == [('CNY', Decimal('10')), ('USD', Decimal('72.0'))]


def test_view_does_not_guess_missing_or_future_rates(isolated_cursor):
    cur = isolated_cursor
    cur.execute("INSERT INTO pams_fx_rates_daily VALUES ('2026-01-05','USD','CNY',7.9)")
    cur.execute("""INSERT INTO pams_transactions VALUES
        ('2026-01-03',10,'USD','Santander'), ('2026-01-03',10,'MXN','Santander'),
        ('2026-01-03',10,NULL,'Santander')""")
    cur.execute("SELECT currency, amount_cny FROM v_transactions_cny ORDER BY currency NULLS LAST")
    assert cur.fetchall() == [('MXN', None), ('USD', None), (None, None)]
