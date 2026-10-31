"""Validate the optional category write on an explicitly disposable database."""

import os
import sys
from pathlib import Path

import psycopg2
import pytest
from psycopg2.extensions import parse_dsn
from psycopg2.extras import RealDictCursor

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend/api"))
from domain.assets.reference_data import ensure_legacy_category


def test_category_write_is_idempotent_without_changing_existing_categories():
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
            cur.execute("""CREATE TEMP TABLE pams_asset_categories (
                major_category text NOT NULL, minor_category text NOT NULL,
                UNIQUE (major_category, minor_category))""")
            cur.execute("INSERT INTO pams_asset_categories VALUES ('资产', '原分类')")
            ensure_legacy_category(cur, "现金", "活期")
            ensure_legacy_category(cur, "现金", "活期")
            cur.execute("SELECT major_category, minor_category FROM pams_asset_categories ORDER BY major_category, minor_category")
            assert {(row["major_category"], row["minor_category"]) for row in cur.fetchall()} == {
                ("资产", "原分类"), ("现金", "活期")
            }
    finally:
        conn.rollback()
        conn.close()
