"""Exercise valuation ordering and evidence using an explicitly marked test DB."""

import os
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import psycopg2
import pytest
from psycopg2.extensions import parse_dsn
from psycopg2.extras import RealDictCursor

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend/api"))
from domain.investments.valuation_state_reader import latest_published


def test_published_valuation_keeps_currency_status_order_and_source_evidence():
    dsn = os.environ.get("PAMS_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("explicit disposable PostgreSQL DSN required")
    assert parse_dsn(dsn).get("host") in {"127.0.0.1", "localhost", "::1"}
    conn = psycopg2.connect(dsn, connect_timeout=5)
    try:
        assert conn.server_version // 10000 == 16
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT shobj_description(oid, 'pg_database') AS marker FROM pg_database WHERE datname=current_database()")
            assert cur.fetchone()["marker"] == "pams-disposable-schema-test"
            cur.execute("""CREATE TEMP TABLE pams_portfolio_valuation_state (
                valuation_date date, cycle text, base_currency text, status text,
                data_completeness text, net_liquidation_value numeric,
                source_hash text, raw_json jsonb, published_at timestamptz,
                updated_at timestamptz)""")
            cur.execute("""INSERT INTO pams_portfolio_valuation_state VALUES
                ('2026-01-02', 'close', 'CNY', 'published', 'partial', 12.34, 'hash-1', '{"quality":"estimated"}', '2026-01-02Z', '2026-01-02Z'),
                ('2026-01-02', 'manual', 'CNY', 'published', 'complete', 98.76, 'hash-2', '{"quality":"confirmed"}', '2026-01-03Z', '2026-01-03Z'),
                ('2026-01-03', 'close', 'CNY', 'draft', 'complete', 100, 'draft', '{}', NULL, '2026-01-04Z'),
                ('2026-01-04', 'close', 'CNY', 'failed', 'complete', 200, 'failed', '{}', NULL, '2026-01-04Z'),
                ('2026-01-05', 'close', 'USD', 'published', 'partial', 5, 'usd', '{}', '2026-01-05Z', '2026-01-05Z')""")
            row = latest_published(cur, base_currency="cny")
            assert row["valuation_date"] == date(2026, 1, 2)
            assert row["cycle"] == "manual" and row["status"] == "published"
            assert row["net_liquidation_value"] == Decimal("98.76")
            assert row["data_completeness"] == "complete" and row["source_hash"] == "hash-2"
            assert row["raw_json"] == {"quality": "confirmed"}
            assert latest_published(cur, base_currency="USD")["source_hash"] == "usd"
            assert latest_published(cur, base_currency="EUR") is None
    finally:
        conn.rollback()
        conn.close()
