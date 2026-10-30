from __future__ import annotations

import os
import sys
from pathlib import Path
from uuid import uuid4

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import parse_dsn
from psycopg2.extras import RealDictCursor
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "api"))
from domain.assets import asset_master as master  # noqa: E402


@pytest.fixture
def scratch_cursor():
    dsn = os.environ.get("PAMS_ASSET_ISOLATED_TEST_DSN", "")
    if not dsn:
        pytest.skip("explicit marked isolated Unix-socket test database required")
    params = parse_dsn(dsn)
    socket = params.get("host", "")
    name = params.get("dbname", "")
    if not socket.startswith("/tmp/pams-asset-test-") or not name.startswith("pams_asset_test_"):
        raise AssertionError("refusing non-isolated asset test DSN")
    conn = psycopg2.connect(dsn, connect_timeout=2)
    schema = "asset_test_" + uuid4().hex
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT current_database() AS name, inet_server_addr() AS host, shobj_description(oid, 'pg_database') AS marker FROM pg_database WHERE datname = current_database()")
            identity = cur.fetchone()
            assert identity["name"] == name and identity["host"] is None
            assert identity["marker"] == "pams-isolated-asset-identity"
            assert conn.server_version // 10000 == 16
            cur.execute(sql.SQL("CREATE SCHEMA {} ").format(sql.Identifier(schema)))
            cur.execute(sql.SQL("SET search_path TO {}, pg_catalog").format(sql.Identifier(schema)))
            yield cur
    finally:
        conn.rollback()
        conn.close()



def _relation_count(cur):
    cur.execute("SELECT count(*) AS n FROM pg_class WHERE relnamespace=current_schema()::regnamespace AND relkind IN ('r','p','v','m')")
    return cur.fetchone()["n"]


def test_missing_schema_stops_sync_without_creating_objects(scratch_cursor):
    cur = scratch_cursor
    assert _relation_count(cur) == 0
    with pytest.raises(master.AssetMasterSchemaUnavailable, match="pams_asset_master"):
        master.sync_asset_master_from_analysis(cur, positions=[], units=[], config={})
    assert _relation_count(cur) == 0


def test_schema_check_detects_missing_column_without_altering_table(scratch_cursor):
    cur = scratch_cursor
    cur.execute("CREATE TABLE pams_asset_master(asset_id text)")
    with pytest.raises(master.AssetMasterSchemaUnavailable, match="primary_symbol"):
        master.ensure_asset_master_schema(cur)
    cur.execute("SELECT count(*) AS n FROM pg_attribute WHERE attrelid='pams_asset_master'::regclass AND attnum > 0 AND NOT attisdropped")
    assert cur.fetchone()["n"] == 1


def test_preprovisioned_schema_supports_existing_sync_without_creating_objects(scratch_cursor):
    cur = scratch_cursor
    schema_sql = (ROOT / "database/postgres/schema/023_asset_master.sql").read_text(encoding="utf-8")
    cur.execute(schema_sql)
    before = _relation_count(cur)
    master.ensure_asset_master_schema(cur)
    raw = {"symbol": "sh600001", "asset_name": "synthetic", "asset_type": "stock_cn", "currency": "CNY", "industry": "银行"}
    lookup = master.sync_asset_master_from_analysis(cur, positions=[raw], units=[], config={})
    assert master._find_asset(raw, lookup)["primary_symbol"] == "sh600001"
    assert _relation_count(cur) == before


def test_missing_unique_key_stops_preflight_before_writes(scratch_cursor):
    cur = scratch_cursor
    cur.execute((ROOT / "database/postgres/schema/023_asset_master.sql").read_text(encoding="utf-8"))
    cur.execute("SELECT conname FROM pg_constraint WHERE conrelid='pams_asset_alias'::regclass AND contype='u'")
    constraint = cur.fetchone()["conname"]
    cur.execute(sql.SQL("ALTER TABLE pams_asset_alias DROP CONSTRAINT {} ").format(sql.Identifier(constraint)))
    before = _relation_count(cur)
    with pytest.raises(master.AssetMasterSchemaUnavailable, match="pams_asset_alias.*unique"):
        master.sync_asset_master_from_analysis(cur, positions=[], units=[], config={})
    assert _relation_count(cur) == before
    cur.execute("SELECT count(*) AS n FROM pams_asset_master")
    assert cur.fetchone()["n"] == 0
