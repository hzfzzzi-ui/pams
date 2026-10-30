"""Disposable PG16 database only; never connect through production settings."""
from contextlib import contextmanager
import os
from pathlib import Path
from uuid import uuid4

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import parse_dsn
import pytest

ROOT = Path(__file__).resolve().parents[2]


@contextmanager
def scratch_database():
    dsn = os.environ.get("PAMS_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Explicit isolated PostgreSQL DSN required")
    params = parse_dsn(dsn)
    assert params.get("host") in {"127.0.0.1", "localhost", "::1"}
    admin = psycopg2.connect(dsn, connect_timeout=5)
    assert admin.server_version // 10000 == 16
    admin.autocommit = True
    name = "pams_import_test_" + uuid4().hex
    with admin.cursor() as cur:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        cur.execute(sql.SQL("COMMENT ON DATABASE {} IS 'pams-isolated-account-import'").format(sql.Identifier(name)))
    target = {**params, "dbname": name}
    try:
        with psycopg2.connect(**target) as conn, conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE pams_accounts(account_code varchar(50) PRIMARY KEY,display_name text,
                    base_currency text DEFAULT 'CNY',active boolean DEFAULT true);
                INSERT INTO pams_accounts(account_code,display_name) VALUES
                    ('tonghuashun','Test broker'),('tencent_licaitong','Test fund'),('bank_test','Test bank');
                CREATE TABLE pams_asset_master(asset_id varchar(120) PRIMARY KEY,primary_symbol text,
                    canonical_name text,asset_type text,currency text,status text DEFAULT 'active');
                INSERT INTO pams_asset_master VALUES
                    ('TEST_STOCK','000001','Test stock','stock','CNY','active'),
                    ('TEST_FUND','011613','Test fund C','fund','CNY','active');
                CREATE TABLE pams_asset_alias(asset_id text,alias_value text);
            """)
            cur.execute((ROOT / "database/postgres/schema/072_account_import.sql").read_text(encoding="utf-8"))
            cur.execute((ROOT / "database/postgres/schema/024_cashflow_and_trade_fact.sql").read_text(encoding="utf-8"))
            cur.execute("""CREATE TABLE pams_holdings_snapshot(
                id bigserial PRIMARY KEY,account_code varchar(50),symbol text,name text,currency text,
                asset_liability text,major_category text,minor_category text,as_of date,
                market_value numeric(20,4),cost numeric(20,4),
                holding_pnl numeric(20,4) GENERATED ALWAYS AS (market_value-cost) STORED,
                quantity numeric(20,6),buy_price numeric(20,6),current_price numeric(20,6),asset_id text,
                created_at timestamptz DEFAULT now(),updated_at timestamptz DEFAULT now(),
                UNIQUE(account_code,name,as_of));""")
        yield target
    finally:
        assert name.startswith("pams_import_test_") and len(name) == len("pams_import_test_") + 32
        with admin.cursor() as cur:
            cur.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()
