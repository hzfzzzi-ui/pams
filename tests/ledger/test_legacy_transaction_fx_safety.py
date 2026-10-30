from __future__ import annotations

from decimal import Decimal
import os
from pathlib import Path
import sys
from uuid import uuid4

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import parse_dsn
from fastapi import HTTPException
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend" / "api"))
from endpoints import transactions


@pytest.fixture
def isolated_db(monkeypatch):
    dsn = os.environ.get("PAMS_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("explicit disposable PostgreSQL DSN required")
    params = parse_dsn(dsn)
    assert params.get("host") in {"127.0.0.1", "localhost", "::1"}
    conn = psycopg2.connect(dsn, connect_timeout=5)
    assert conn.server_version // 10000 == 16
    conn.autocommit = True
    schema = "legacy_fx_api_" + uuid4().hex
    with conn.cursor() as cur:
        cur.execute("SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname=current_database()")
        assert cur.fetchone()[0] == "pams-disposable-schema-test"
        cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        cur.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        cur.execute("""CREATE TABLE pams_accounts(account_code text, display_name text);
            CREATE TABLE pams_fx_rates_daily(as_of date, base_currency text, quote_currency text, rate numeric);
            CREATE TABLE pams_transactions(id int, date date, amount numeric, currency text,
                category text, subcategory text, account_code text, description text, location text,
                auto_classified boolean, source text, created_at timestamptz);
            INSERT INTO pams_accounts VALUES ('a','Synthetic');
            INSERT INTO pams_fx_rates_daily VALUES
                ('2026-01-01','CNY','USD',7.2),('2026-01-02','CNY','MXN',0.35),
                ('2026-01-05','CNY','USD',9.9),('2026-01-01','USD','CNY',0.1);
            INSERT INTO pams_transactions VALUES
                (1,'2026-01-03',10,'CNY','收入',NULL,'a',NULL,NULL,false,'test',now()),
                (2,'2026-01-03',-10,'USD','生活',NULL,'a',NULL,NULL,false,'test',now()),
                (3,'2026-01-03',-1,'CAD','内部划转',NULL,'a',NULL,NULL,false,'test',now()),
                (4,'2026-01-03',-2,'MXN','内部划转',NULL,'a',NULL,NULL,false,'test',now());""")
    def connect():
        return psycopg2.connect(dsn, options="-c search_path=" + schema)
    monkeypatch.setattr(transactions, "get_conn", connect)
    yield conn
    with conn.cursor() as cur:
        cur.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
    conn.close()


def test_recent_reports_missing_rate_instead_of_fabricating(isolated_db):
    records = {row["id"]: row for row in transactions.get_recent_transactions(limit=10)["transactions"]}
    assert Decimal(records[1]["amount_cny"]) == 10
    assert Decimal(records[2]["amount_cny"]) == -72
    assert records[3]["amount_cny"] is None
    assert Decimal(records[4]["amount_cny"]) == Decimal("-0.70")


def test_unknown_amount_blocks_aggregates_and_known_rows_keep_contract(isolated_db):
    with pytest.raises(HTTPException) as exc:
        transactions.get_monthly_stats("2026-01")
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException):
        transactions.get_daily_stats("2026-01")
    with pytest.raises(HTTPException):
        transactions.get_categories_by_range("2026-01-01", "2026-01-31")
    with isolated_db.cursor() as cur:
        cur.execute("DELETE FROM pams_transactions WHERE id = 3")
    summary = transactions.get_monthly_stats("2026-01")
    assert Decimal(summary["total_income"]) == Decimal("10")
    assert Decimal(summary["total_expense"]) == Decimal("72.70")
    assert Decimal(summary["net"]) == Decimal("-62.70")
    daily = transactions.get_daily_stats("2026-01")
    assert len(daily) == 1 and Decimal(daily[0]["expense"]) == Decimal("72.70")
    categories = transactions.get_categories_by_range("2026-01-01", "2026-01-31")
    assert sum(Decimal(row["amount"]) for row in categories) == Decimal("72.70")


def test_zero_rate_is_unknown_and_never_used(isolated_db):
    with isolated_db.cursor() as cur:
        cur.execute("UPDATE pams_fx_rates_daily SET rate=0 WHERE base_currency='CNY' AND quote_currency='USD' AND as_of='2026-01-01'")
    records = {row["id"]: row for row in transactions.get_recent_transactions(limit=10)["transactions"]}
    assert records[2]["amount_cny"] is None
    with pytest.raises(HTTPException) as exc:
        transactions.get_monthly_stats("2026-01")
    assert exc.value.status_code == 422


@pytest.mark.parametrize("endpoint", ["monthly", "daily", "categories"])
def test_aggregates_use_one_snapshot_when_fx_and_rows_change(isolated_db, monkeypatch, endpoint):
    # Leave only known-rate transactions at the start of the first statement.
    with isolated_db.cursor() as cur:
        cur.execute("DELETE FROM pams_transactions WHERE id = 3")
    original = transactions._require_cny_amounts
    calls = []
    def concurrent_change(cur, start, end):
        original(cur, start, end)
        with isolated_db.cursor() as other:
            other.execute("UPDATE pams_fx_rates_daily SET rate=0 WHERE base_currency='CNY' AND quote_currency='USD' AND as_of='2026-01-01'")
            other.execute("""INSERT INTO pams_transactions VALUES
                (5,'2026-01-03',-20,'EUR','生活',NULL,'a',NULL,NULL,false,'test',now())""")
        calls.append(True)
    monkeypatch.setattr(transactions, "_require_cny_amounts", concurrent_change)
    if endpoint == "monthly":
        result = transactions.get_monthly_stats("2026-01")
        assert Decimal(result["total_expense"]) == Decimal("72.70")
    elif endpoint == "daily":
        result = transactions.get_daily_stats("2026-01")
        assert len(result) == 1 and Decimal(result[0]["expense"]) == Decimal("72.70")
    else:
        result = transactions.get_categories_by_range("2026-01-01", "2026-01-31")
        assert sum(Decimal(row["amount"]) for row in result) == Decimal("72.70")
    assert calls == [True]


def test_http_api_serializes_missing_and_blocks_incomplete_totals(isolated_db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(transactions.router, prefix="/api")
    client = TestClient(app)
    response = client.get("/api/transactions/recent")
    assert response.status_code == 200
    rows = {row["id"]: row for row in response.json()["transactions"]}
    assert rows[3]["amount_cny"] is None
    assert Decimal(rows[2]["amount_cny"]) == Decimal("-72")
    params = {}
    assert client.get("/api/transactions/monthly/2026-01", params=params).status_code == 422
    assert client.get("/api/transactions/daily/2026-01", params=params).status_code == 422
    assert client.get("/api/transactions/categories-by-range", params={**params, "start": "2026-01-01", "end": "2026-01-31"}).status_code == 422
