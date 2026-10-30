"""Verify legacy transaction boundaries only against an explicit disposable database."""
import os
from pathlib import Path
import sys
from uuid import uuid4

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import parse_dsn
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from endpoints import transactions  # noqa: E402


@pytest.fixture
def transaction_database(monkeypatch):
    dsn = os.environ.get("PAMS_SCHEMA_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Explicit disposable schema-test PostgreSQL DSN required")
    params = parse_dsn(dsn)
    assert params.get("host") in {"127.0.0.1", "localhost", "::1"}
    admin = psycopg2.connect(dsn, connect_timeout=5)
    assert admin.server_version // 10000 == 16
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute("SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = current_database()")
        assert cur.fetchone()[0] == "pams-disposable-schema-test"
    name = "pams_transaction_test_" + uuid4().hex
    with admin.cursor() as cur:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    test_params = {**params, "dbname": name}
    try:
        with psycopg2.connect(**test_params) as conn, conn.cursor() as cur:
            for path in sorted((ROOT / "database/postgres/schema").glob("*.sql")):
                cur.execute(path.read_text(encoding="utf-8"))
        monkeypatch.setattr(transactions, "get_conn", lambda: psycopg2.connect(**test_params))
        yield test_params
    finally:
        with admin.cursor() as cur:
            cur.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()


def _client():
    app = FastAPI()
    app.include_router(transactions.router, prefix="/api")
    return TestClient(app, raise_server_exceptions=False)


def _payload():
    return {"date": "2025-12-01", "amount": -5, "currency": "MXN",
            "account_code": "santander", "category": "测试"}


def _seed_history(cur):
    cur.execute("""INSERT INTO pams_cashflow_fact
        (event_date,account_code,amount,currency,amount_cny,cashflow_type,source_table,
         source_id,source_hash,source_channel,review_status)
        VALUES ('2025-01-01','santander',-5,'MXN',-2,'operating_expense',
        'pams_transactions','old-1','synthetic1','telegram','confirmed')""")


def _history(cur):
    cur.execute("SELECT to_jsonb(cf) FROM pams_cashflow_fact cf ORDER BY cashflow_id")
    return cur.fetchall()


def test_telegram_and_default_source_stop_before_any_database_call(monkeypatch):
    def no_database():
        raise AssertionError("Telegram requests must not open a database connection")
    monkeypatch.setattr(transactions, "get_conn", no_database)
    client = _client()
    for body in (_payload(), {**_payload(), "source": "telegram"}):
        response = client.post("/api/transactions", json=body)
        assert response.status_code == 410
        assert "Telegram" in response.json()["detail"]


def test_manual_transaction_and_budget_continue_preserving_history(transaction_database):
    with psycopg2.connect(**transaction_database) as conn, conn.cursor() as cur:
        _seed_history(cur)
        before = _history(cur)
    client = _client()
    response = client.post("/api/transactions", json={**_payload(), "source": "manual"})
    assert response.status_code == 200
    assert response.json()["success"] is True and response.json()["message"] == "交易记录已创建"
    response = client.post("/api/transactions/budgets", json={"category": "测试", "month": "2025-12", "amount": 30})
    assert response.status_code == 200 and response.json()["success"] is True
    with psycopg2.connect(**transaction_database) as conn, conn.cursor() as cur:
        assert _history(cur) == before
        cur.execute("SELECT amount,source FROM pams_transactions")
        assert cur.fetchone() == (-5, "manual")
        cur.execute("SELECT count(*) FROM pams_holdings_snapshot")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT budget_amount FROM pams_budgets WHERE category='测试'")
        assert cur.fetchone()[0] == 30


def test_production_like_missing_legacy_table_keeps_history(transaction_database):
    with psycopg2.connect(**transaction_database) as conn, conn.cursor() as cur:
        _seed_history(cur)
        before = _history(cur)
        cur.execute("DROP VIEW public.pams_transactions")
    client = _client()
    assert client.post("/api/transactions", json=_payload()).status_code == 410
    assert client.post("/api/transactions", json={**_payload(), "source": "manual"}).status_code == 500
    response = client.post("/api/transactions/budgets", json={"category": "测试", "month": "2025-12", "amount": 30})
    assert response.status_code == 200
    with psycopg2.connect(**transaction_database) as conn, conn.cursor() as cur:
        assert _history(cur) == before
        cur.execute("SELECT count(*) FROM pams_holdings_snapshot")
        assert cur.fetchone()[0] == 0
