"""Exercise the legacy transaction endpoint on an explicitly disposable PG16 server."""
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import time
from uuid import uuid4

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import parse_dsn
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from endpoints import transactions


ROOT = Path(__file__).resolve().parents[2]


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
        with psycopg2.connect(**test_params) as conn:
            with conn.cursor() as cur:
                for path in sorted((ROOT / "database/postgres/schema").glob("*.sql")):
                    cur.execute(path.read_text(encoding="utf-8"))
        monkeypatch.setattr(transactions, "get_conn", lambda: psycopg2.connect(**test_params))
        yield test_params
    finally:
        with admin.cursor() as cur:
            cur.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()


def _input(*, source="telegram", amount=-5, category="测试", description=""):
    return transactions.TransactionCreate(
        date="2025-12-01", amount=amount, currency="MXN", account_code="santander",
        category=category, source=source, description=description,
    )


def _count(cur, table):
    cur.execute(sql.SQL("SELECT count(*) FROM {} ").format(sql.Identifier(table)))
    return cur.fetchone()[0]


def test_missing_log_rolls_back_transaction_holding_and_classification(transaction_database):
    with psycopg2.connect(**transaction_database) as conn, conn.cursor() as cur:
        cur.execute("SELECT match_count FROM pams_category_rules WHERE keyword='午餐'")
        before = cur.fetchone()[0]
    with pytest.raises(psycopg2.errors.UndefinedTable):
        transactions.create_transaction(_input(category=None, description="午餐"))
    with psycopg2.connect(**transaction_database) as conn, conn.cursor() as cur:
        assert _count(cur, "pams_transactions") == 0
        assert _count(cur, "pams_holdings_snapshot") == 0
        cur.execute("SELECT match_count FROM pams_category_rules WHERE keyword='午餐'")
        assert cur.fetchone()[0] == before


def test_log_failure_rolls_back_and_success_keeps_existing_behavior(transaction_database):
    with psycopg2.connect(**transaction_database) as conn, conn.cursor() as cur:
        # A synthetic fixture only: this is not a proposed shared production schema.
        cur.execute("""CREATE TABLE public.pams_transaction_sync_log (
            transaction_id bigint, holding_id bigint, sync_type text CHECK (sync_type='income'), amount numeric)""")
        cur.execute("""INSERT INTO pams_holdings_snapshot
            (name,asset_liability,currency,account_code,major_category,minor_category,market_value,cost,as_of)
            VALUES ('Santander现金','资产','MXN','santander','现金','现金/活期',100,100,'2025-12-01')""")
    with pytest.raises(psycopg2.errors.CheckViolation):
        transactions.create_transaction(_input())
    with psycopg2.connect(**transaction_database) as conn, conn.cursor() as cur:
        assert _count(cur, "pams_transactions") == 0
        assert _count(cur, "pams_transaction_sync_log") == 0
        cur.execute("SELECT market_value,cost FROM pams_holdings_snapshot")
        assert tuple(map(str, cur.fetchone())) == ("100.0000", "100.0000")
    result = transactions.create_transaction(_input(amount=5))
    with psycopg2.connect(**transaction_database) as conn, conn.cursor() as cur:
        cur.execute("SELECT transaction_id,sync_type,amount FROM pams_transaction_sync_log")
        log = cur.fetchone()
        assert log[0] == result["transaction_id"] and log[1] == "income" and log[2] == 5
        cur.execute("SELECT market_value,cost FROM pams_holdings_snapshot")
        assert tuple(map(str, cur.fetchone())) == ("105.0000", "100.0000")
        assert _count(cur, "pams_transactions") == 1


def test_non_telegram_transaction_does_not_require_log(transaction_database):
    result = transactions.create_transaction(_input(source="manual"))
    with psycopg2.connect(**transaction_database) as conn, conn.cursor() as cur:
        cur.execute("SELECT id,source FROM pams_transactions")
        assert cur.fetchone() == (result["transaction_id"], "manual")
        assert _count(cur, "pams_holdings_snapshot") == 0


def test_new_holding_and_log_are_committed_together(transaction_database):
    with psycopg2.connect(**transaction_database) as conn, conn.cursor() as cur:
        cur.execute("""CREATE TABLE public.pams_transaction_sync_log (
            transaction_id bigint, holding_id bigint, sync_type text, amount numeric)""")
    result = transactions.create_transaction(_input(amount=5))
    with psycopg2.connect(**transaction_database) as conn, conn.cursor() as cur:
        cur.execute("SELECT id,market_value,cost FROM pams_holdings_snapshot")
        holding_id, value, cost = cur.fetchone()
        assert value == cost == 5
        cur.execute("SELECT transaction_id,holding_id,sync_type,amount FROM pams_transaction_sync_log")
        assert cur.fetchone() == (result["transaction_id"], holding_id, "income", 5)


def test_deferred_commit_failure_rolls_back_all_writes(transaction_database):
    with psycopg2.connect(**transaction_database) as conn, conn.cursor() as cur:
        cur.execute("CREATE TABLE public.fixture_expected_transaction (id bigint PRIMARY KEY)")
        cur.execute("""CREATE TABLE public.pams_transaction_sync_log (
            transaction_id bigint REFERENCES public.fixture_expected_transaction(id)
                DEFERRABLE INITIALLY DEFERRED,
            holding_id bigint, sync_type text, amount numeric)""")
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        transactions.create_transaction(_input(amount=5))
    with psycopg2.connect(**transaction_database) as conn, conn.cursor() as cur:
        assert _count(cur, "pams_transactions") == 0
        assert _count(cur, "pams_holdings_snapshot") == 0
        assert _count(cur, "pams_transaction_sync_log") == 0


def test_concurrent_existing_holding_updates_do_not_lose_a_transaction(transaction_database):
    with psycopg2.connect(**transaction_database) as conn, conn.cursor() as cur:
        cur.execute("""CREATE TABLE public.pams_transaction_sync_log (
            id bigserial PRIMARY KEY, transaction_id bigint, holding_id bigint,
            sync_type text, amount numeric, created_at timestamptz DEFAULT now())""")
        cur.execute("""INSERT INTO pams_holdings_snapshot
            (name,asset_liability,currency,account_code,major_category,minor_category,market_value,cost,as_of)
            VALUES ('Santander现金','资产','MXN','santander','现金','现金/活期',100,100,'2025-12-01')""")
    blocker = psycopg2.connect(**transaction_database)
    monitor = psycopg2.connect(**transaction_database)
    monitor.autocommit = True
    try:
        with blocker.cursor() as cur:
            cur.execute("SELECT id FROM pams_holdings_snapshot FOR UPDATE")
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(transactions.create_transaction, _input(amount=amount)) for amount in (5, 7)]
            try:
                deadline = time.monotonic() + 10
                waiting = 0
                while time.monotonic() < deadline:
                    with monitor.cursor() as cur:
                        cur.execute("""SELECT count(*) FROM pg_stat_activity
                            WHERE datname=current_database() AND wait_event_type='Lock'
                            AND query LIKE '%%FROM pams_holdings_snapshot%%'""")
                        waiting = cur.fetchone()[0]
                    if waiting == 2:
                        break
                    time.sleep(0.05)
                assert waiting == 2, "Both requests must contend on the same holding"
            finally:
                blocker.rollback()
            results = [future.result(timeout=10) for future in futures]
    finally:
        blocker.close()
        monitor.close()
    assert all(result["success"] for result in results)
    with psycopg2.connect(**transaction_database) as conn, conn.cursor() as cur:
        cur.execute("SELECT market_value FROM pams_holdings_snapshot")
        assert cur.fetchone()[0] == 112
        assert _count(cur, "pams_transactions") == 2
        assert _count(cur, "pams_transaction_sync_log") == 2


def test_http_contract_preserves_success_and_failed_sync_does_not_commit(transaction_database):
    app = FastAPI()
    app.include_router(transactions.router, prefix="/api")
    client = TestClient(app, raise_server_exceptions=False)
    body = _input(source="manual").model_dump()
    response = client.post("/api/transactions", json=body)
    assert response.status_code == 200
    assert response.json() == {
        "success": True, "transaction_id": response.json()["transaction_id"], "message": "交易记录已创建",
    }
    body["source"] = "telegram"
    response = client.post("/api/transactions", json=body)
    assert response.status_code == 500
    with psycopg2.connect(**transaction_database) as conn, conn.cursor() as cur:
        assert _count(cur, "pams_transactions") == 1
        assert _count(cur, "pams_holdings_snapshot") == 0
