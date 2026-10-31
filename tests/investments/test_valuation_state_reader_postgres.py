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
from domain.investments.valuation_state_reader import latest_published, valuation_series_evidence, valuation_series_rows
from domain.investments import portfolio_state


def test_published_valuation_keeps_currency_status_order_and_source_evidence(monkeypatch):
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
            cur.execute("""INSERT INTO pams_portfolio_valuation_state VALUES
                ('2026-01-02', 'newer-draft', 'CNY', 'draft', 'partial', 999, 'draft-new', '{}', NULL, '2026-01-08Z')""")
            cur.execute("""ALTER TABLE pams_portfolio_valuation_state
                ADD COLUMN assets_cny numeric, ADD COLUMN liabilities_cny numeric,
                ADD COLUMN investment_value_cny numeric, ADD COLUMN total_pnl_cny numeric,
                ADD COLUMN source_run_id bigint""")
            row = latest_published(cur, base_currency="cny")
            assert row["valuation_date"] == date(2026, 1, 2)
            assert row["cycle"] == "manual" and row["status"] == "published"
            assert row["net_liquidation_value"] == Decimal("98.76")
            assert row["data_completeness"] == "complete" and row["source_hash"] == "hash-2"
            assert row["raw_json"] == {"quality": "confirmed"}
            assert latest_published(cur, base_currency="USD")["source_hash"] == "usd"
            assert latest_published(cur, base_currency="EUR") is None
            series = valuation_series_rows(cur, base_currency="CNY", limit=3)
            assert [row["valuation_date"] for row in series] == [date(2026, 1, 2), date(2026, 1, 3), date(2026, 1, 4)]
            assert [row["net_liquidation_value"] for row in series] == [Decimal("98.76"), Decimal("100"), Decimal("200")]
            assert series[0]["assets_cny"] is None
            evidence = [valuation_series_evidence(row, source_table="pams_portfolio_valuation_state") for row in series]
            assert [item["publication_status"] for item in evidence] == ["published", "draft", "failed"]
            assert [item["daily_accurate_eligible"] for item in evidence] == [None, False, False]
            assert evidence[0]["source_hash"] == "hash-2"
            assert evidence[0]["business_date"] == date(2026, 1, 2)
            assert evidence[0]["data_completeness"] == "complete"
            assert evidence[0]["input_lineage_status"] == "unknown"
            assert evidence[0]["valuation_period"] == "unknown"
            monkeypatch.setattr(portfolio_state, "_table_exists", lambda cur, table: True)
            projected = portfolio_state.load_valuation_series(cur, limit=3, base_currency="CNY")
            assert [row["net_worth_cny"] for row in projected] == [Decimal("98.76"), Decimal("100"), Decimal("200")]
            assert [row["valuation_meta"]["publication_status"] for row in projected] == ["published", "draft", "failed"]
            assert len(valuation_series_rows(cur, base_currency="CNY", limit=2)) == 2
            assert valuation_series_rows(cur, base_currency="cny", limit=3) == []
    finally:
        conn.rollback()
        conn.close()


def test_legacy_nav_evidence_does_not_invent_quality_or_publication():
    evidence = valuation_series_evidence({"as_of": date(2025, 1, 1), "status": "published"}, source_table="pams_nav_daily")
    assert evidence["business_date"] == date(2025, 1, 1)
    assert evidence["publication_status"] == "unknown"
    assert evidence["daily_accurate_eligible"] is None
    assert evidence["data_completeness"] is None
    assert evidence["source_hash"] is None
    assert evidence["input_lineage_status"] == "unknown"
