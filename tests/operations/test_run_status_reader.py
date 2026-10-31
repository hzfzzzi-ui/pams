"""Verify the legacy status provider using a disposable PostgreSQL connection."""
import os
from datetime import date
from pathlib import Path
import sys

import psycopg2
from psycopg2.extensions import parse_dsn
from psycopg2.extras import RealDictCursor
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend/api'))
from domain.operations.run_status import latest_sync_run


def test_latest_run_preserves_scope_and_status_contract():
    dsn = os.environ.get('PAMS_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('explicit disposable PostgreSQL DSN required')
    assert parse_dsn(dsn).get('host') in {'127.0.0.1', 'localhost', '::1'}
    conn = psycopg2.connect(dsn, connect_timeout=5)
    try:
        assert conn.server_version // 10000 == 16
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT shobj_description(oid, 'pg_database') AS marker FROM pg_database WHERE datname=current_database()")
            assert cur.fetchone()['marker'] == 'pams-disposable-schema-test'
            cur.execute("""CREATE TEMP TABLE pams_data_job_run (
                job_key text, asset_scope text, status text, started_at timestamptz,
                finished_at timestamptz, target_date date, data_as_of date,
                rows_affected integer, summary_json jsonb, error_text text)""")
            cur.execute("""INSERT INTO pams_data_job_run VALUES
                ('prices', 'A', 'success', '2026-01-01Z', NULL, '2026-01-01', '2026-01-01', 2, '{}', NULL),
                ('prices', 'A', 'failed', '2026-01-02Z', NULL, '2026-01-02', '2026-01-01', 0, '{}', 'failure'),
                ('prices', 'B', 'success', '2026-01-03Z', NULL, '2026-01-03', '2026-01-03', 3, '{}', NULL)""")
            row = latest_sync_run(cur, 'prices', 'A')
            assert row['status'] == 'failed' and row['target_trade_date'] == date(2026, 1, 2)
            assert row['latest_trade_date_after'] == date(2026, 1, 1) and row['error_text'] == 'failure'
            assert latest_sync_run(cur, 'prices')['asset_scope'] == 'B'
            assert latest_sync_run(cur, 'missing') is None
    finally:
        conn.rollback()
        conn.close()
