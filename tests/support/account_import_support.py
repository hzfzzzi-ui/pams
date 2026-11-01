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
def scratch_database(*, development=False):
    dsn = os.environ.get("PAMS_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Explicit isolated PostgreSQL DSN required")
    params = parse_dsn(dsn)
    assert params.get("host") in {"127.0.0.1", "localhost", "::1"}
    admin = psycopg2.connect(dsn, connect_timeout=5)
    assert admin.server_version // 10000 == 16
    admin.autocommit = True
    prefix = "pams_dev_" if development else "pams_import_test_"
    marker = "pams-development-sandbox" if development else "pams-isolated-account-import"
    name = prefix + uuid4().hex
    with admin.cursor() as cur:
        cur.execute("SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname=current_database()")
        assert cur.fetchone()[0] == 'pams-disposable-schema-test'
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        cur.execute(sql.SQL("COMMENT ON DATABASE {} IS {}").format(sql.Identifier(name), sql.Literal(marker)))
    target = {**params, "dbname": name}
    try:
        with psycopg2.connect(**target) as conn, conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE pams_accounts(account_code varchar(50) PRIMARY KEY,display_name text,
                    base_currency text DEFAULT 'CNY',active boolean DEFAULT true);
                INSERT INTO pams_accounts(account_code,display_name) VALUES
                    ('tonghuashun','Test broker'),('tencent_licaitong','Test fund'),('bank_test','Test bank');
                CREATE TABLE pams_asset_master(asset_id varchar(120) PRIMARY KEY,primary_symbol text,
                    canonical_name text,asset_type text,currency text,status text DEFAULT 'active',
                    display_name text,exchange text,market text,source text,confidence text,
                    raw_json jsonb DEFAULT '{}'::jsonb,updated_at timestamptz DEFAULT now());
                INSERT INTO pams_asset_master(asset_id,primary_symbol,canonical_name,asset_type,currency,status) VALUES
                    ('TEST_STOCK','000001','Test stock','stock','CNY','active'),
                    ('TEST_FUND','011613','Test fund C','fund','CNY','active');
                UPDATE pams_asset_master SET exchange='SZSE' WHERE asset_id='TEST_STOCK';
                CREATE TABLE pams_asset_alias(asset_id text,alias_value text,alias_type text NOT NULL DEFAULT 'alias');
            """)
            cur.execute((ROOT / "database/postgres/schema/024_cashflow_and_trade_fact.sql").read_text(encoding="utf-8"))
            cur.execute("""CREATE TABLE pams_holdings_snapshot(
                id bigserial PRIMARY KEY,account_code varchar(50),symbol text,name text,currency text,
                asset_liability text,major_category text,minor_category text,as_of date,
                market_value numeric(20,4),cost numeric(20,4),
                holding_pnl numeric(20,4) GENERATED ALWAYS AS (market_value-cost) STORED,
                quantity numeric(20,6),buy_price numeric(20,6),current_price numeric(20,6),asset_id text,
                created_at timestamptz DEFAULT now(),updated_at timestamptz DEFAULT now(),
                UNIQUE(account_code,name,as_of));""")
            for schema_name in ('074_unified_source.sql','075_source_fact_links.sql','076_valuation_input_evidence.sql','077_confirmed_transfers.sql'):
                cur.execute((ROOT / 'database/postgres/schema' / schema_name).read_text(encoding='utf-8'))
        yield target
    finally:
        assert name.startswith(prefix) and len(name) == len(prefix) + 32
        with admin.cursor() as cur:
            cur.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()
