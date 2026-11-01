from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

import psycopg2
from psycopg2 import sql
from psycopg2.extras import RealDictCursor
from tests.support.asset_pg_safety import resolve_asset_test_target, validate_asset_test_database
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "api"))
from domain.assets import asset_master as master  # noqa: E402
from domain.assets import lookthrough_state  # noqa: E402


@pytest.fixture
def scratch_cursor():
    target = resolve_asset_test_target()
    if target is None:
        pytest.skip("explicit marked isolated asset test database required")
    dsn, mode, name = target
    conn = psycopg2.connect(dsn, connect_timeout=2)
    schema = "asset_test_" + uuid4().hex
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            validate_asset_test_database(conn, mode, name)
            cur.execute(sql.SQL("CREATE SCHEMA {} ").format(sql.Identifier(schema)))
            cur.execute(sql.SQL("SET search_path TO {}, pg_catalog").format(sql.Identifier(schema)))
            cur.execute((ROOT / "database/postgres/schema/023_asset_master.sql").read_text(encoding="utf-8"))
            yield cur
    finally:
        conn.rollback()
        conn.close()


def _business_snapshot(cur):
    rows = {}
    for table, fields in (
        ("pams_asset_master", "asset_id,canonical_name,primary_symbol,asset_type,source,confidence,raw_json"),
        ("pams_asset_alias", "asset_id,alias_type,alias_value,source,priority"),
        ("asset_classifications", "asset_id,classification_type,industry_system,industry_leaf,industry_sub_bucket,industry_bucket,classification_scope,is_primary,theme_id,theme_name,parent_theme_id,theme_level,family,score,role,source,confidence,evidence"),
        ("pams_fund_underlying_position", "fund_asset_id,underlying_asset_id,underlying_name,underlying_symbol,underlying_weight,underlying_asset_type,effective_source,coverage_type,confidence,source,raw_json"),
    ):
        cur.execute(sql.SQL("SELECT {} FROM {}").format(sql.SQL(fields), sql.Identifier(table)))
        rows[table] = sorted((dict(row) for row in cur.fetchall()), key=repr)
    return rows


def test_classification_table_keeps_industry_and_theme_separate(scratch_cursor):
    cur = scratch_cursor
    cur.execute("INSERT INTO pams_asset_master(asset_id,canonical_name,asset_type) VALUES ('CLASS_TEST','Example','stock_cn')")
    cur.execute("""INSERT INTO asset_classifications
        (asset_id,classification_type,industry_system,industry_bucket,classification_scope,is_primary)
        VALUES ('CLASS_TEST','industry','SW','科技TMT','listed_equity',TRUE)""")
    cur.execute("""INSERT INTO asset_classifications
        (asset_id,classification_type,theme_id,theme_name,theme_level,score,role)
        VALUES ('CLASS_TEST','theme','ai','人工智能','parent',1,'core')""")
    cur.execute("SELECT classification_type FROM asset_classifications WHERE asset_id='CLASS_TEST' ORDER BY classification_type")
    assert [row['classification_type'] for row in cur.fetchall()] == ['industry', 'theme']
    cur.execute("SAVEPOINT invalid_cross_dimension")
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute("""INSERT INTO asset_classifications
            (asset_id,classification_type,industry_system,industry_bucket,classification_scope,is_primary,theme_id)
            VALUES ('CLASS_TEST','industry','manual','科技TMT','other',TRUE,'not-an-industry')""")
    cur.execute("ROLLBACK TO SAVEPOINT invalid_cross_dimension")


def test_fund_seed_is_read_from_database_without_read_side_writes(scratch_cursor):
    cur = scratch_cursor
    config = {
        "asset_mapping": [{"symbol": "F1", "asset_name": "Fund", "asset_class": "fund_cn"}],
        "asset_lookthrough": [{"parent_symbol": "F1", "parent_asset_name": "Fund", "parent_asset_type": "fund_cn",
            "lookthrough_type": "TOP10", "source_type": "FUND_REPORT", "confidence": "HIGH", "as_of": "2026-01-01",
            "holdings": [{"underlying_name": "Stock", "underlying_symbol": "AAA", "underlying_weight": 0.4}]}],
    }
    master.sync_asset_master_from_lookthrough_config(cur, config=config)
    before = _business_snapshot(cur)
    payload = lookthrough_state.derive_lookthrough_payload(
        [{"symbol": "F1", "asset_name": "Fund", "asset_type": "fund_cn", "market_value_cny": 100, "as_of": "2026-01-02"}],
        config, as_of="2026-01-02", asset_master_cur=cur,
    )
    assert payload['quality']['items'][0]['lookthrough_source'] == 'database'
    assert payload['quality']['items'][0]['coverage_ratio'] == pytest.approx(0.4)
    assert _business_snapshot(cur) == before


def test_current_portfolio_sync_preserves_fund_source_date(scratch_cursor):
    cur = scratch_cursor
    config = {"asset_mapping": [{"symbol": "F1", "asset_name": "Fund", "asset_class": "fund_cn"}],
              "asset_lookthrough": [{"parent_symbol": "F1", "parent_asset_name": "Fund", "parent_asset_type": "fund_cn",
                 "lookthrough_type": "TOP10", "as_of": "2026-01-01", "holdings": [
                     {"underlying_name": "Stock", "underlying_symbol": "AAA", "underlying_weight": 0.4}]}]}
    lookthrough_state.derive_lookthrough_payload(
        [{"symbol": "F1", "asset_name": "Fund", "asset_type": "fund_cn", "market_value_cny": 100,
          "as_of": "2026-01-05"}], config, asset_master_cur=cur, sync_asset_master=True,
    )
    cur.execute("SELECT DISTINCT as_of::text AS as_of FROM pams_fund_underlying_position WHERE source='lookthrough_config'")
    assert [row['as_of'] for row in cur.fetchall()] == ['2026-01-01']


def test_manual_security_projection_preserves_all_master_tables(scratch_cursor):
    cur = scratch_cursor
    cur.execute("""INSERT INTO pams_asset_master
        (asset_id, canonical_name, primary_symbol, asset_type, currency, source, status)
        VALUES ('MANUAL_TEST', 'Example', 'TEST', '欧股个股', 'SEK', 'manual', 'active')""")
    before = _business_snapshot(cur)
    payload = lookthrough_state.derive_lookthrough_payload(
        [{"asset_id": "TEST", "symbol": "TEST", "asset_name": "Example", "asset_type": "us_stock",
          "currency": "SEK", "holding_snapshot_currency": "SEK", "holding_snapshot_asset_id": "MANUAL_TEST",
          "market_value_cny": 70, "as_of": "2026-01-01"}],
        {"asset_mapping": [], "asset_lookthrough": []}, asset_master_cur=cur,
    )
    assert payload["quality"]["items"][0]["lookthrough_type"] == "DIRECT"
    assert payload["total_market_value_cny"] == 70
    assert _business_snapshot(cur) == before


def test_sync_is_business_idempotent_and_ambiguous_names_do_not_match(scratch_cursor):
    cur = scratch_cursor
    positions = [
        {"symbol": "AAA", "asset_name": "Shared", "asset_type": "us_stock", "currency": "USD", "industry": "半导体"},
        {"symbol": "BBB", "asset_name": "Shared", "asset_type": "us_stock", "currency": "USD", "industry": "银行"},
    ]
    lookup = master.sync_asset_master_from_analysis(cur, positions=positions, units=[], config={})
    assert master._find_asset({"asset_name": "Shared"}, lookup) is None
    assert master._find_asset({"symbol": "AAA", "asset_name": "Shared"}, lookup)["primary_symbol"] == "AAA"
    assert master._find_asset({"symbol": "BBB", "asset_name": "Shared"}, lookup)["primary_symbol"] == "BBB"
    first = _business_snapshot(cur)
    master.sync_asset_master_from_analysis(cur, positions=positions, units=[], config={})
    assert _business_snapshot(cur) == first


def test_manual_fund_underlying_is_not_deleted_by_repeat_sync(scratch_cursor):
    cur = scratch_cursor
    unit = {
        "holding_symbol": "FUND_A", "holding_name": "Fund A", "holding_asset_class": "fund",
        "underlying_symbol": "AAA", "underlying_name": "Stock A", "underlying_asset_class": "us_stock",
        "underlying_industry": "半导体", "underlying_weight": 0.25, "lookthrough_type": "TOP10",
    }
    master.sync_asset_master_from_analysis(cur, positions=[], units=[unit], config={})
    cur.execute("SELECT fund_asset_id FROM pams_fund_underlying_position WHERE source='lookthrough_config'")
    fund_id = cur.fetchone()["fund_asset_id"]
    cur.execute("INSERT INTO pams_fund_underlying_position (fund_asset_id, underlying_name, underlying_weight, source, effective_source, raw_json) VALUES (%s, 'manual holding', 0.1, 'manual', 'manual', %s::jsonb)", (fund_id, '{"note":"user evidence"}'))
    first = _business_snapshot(cur)
    master.sync_asset_master_from_analysis(cur, positions=[], units=[unit], config={})
    assert _business_snapshot(cur) == first
    cur.execute("SELECT raw_json FROM pams_fund_underlying_position WHERE fund_asset_id=%s AND source='manual'", (fund_id,))
    assert cur.fetchone()["raw_json"]["note"] == "user evidence"


def test_conflicting_exchange_suffixes_refuse_sync_before_any_asset_write(scratch_cursor):
    cur = scratch_cursor
    positions = [
        {"symbol": "600000.SH", "asset_name": "Shanghai", "asset_type": "stock_cn", "currency": "CNY", "industry": "银行"},
        {"symbol": "600000.SZ", "asset_name": "Shenzhen", "asset_type": "stock_cn", "currency": "CNY", "industry": "半导体"},
    ]
    before = _business_snapshot(cur)
    with pytest.raises(master.AmbiguousAssetIdentityError, match="sync refused"):
        master.sync_asset_master_from_analysis(cur, positions=positions, units=[], config={})
    assert _business_snapshot(cur) == before


def test_existing_id_with_conflicting_exchange_is_not_overwritten(scratch_cursor):
    cur = scratch_cursor
    first = {"symbol": "600000.SH", "asset_name": "Shanghai", "asset_type": "stock_cn", "currency": "CNY", "industry": "银行"}
    second = {"symbol": "600000.SZ", "asset_name": "Shenzhen", "asset_type": "stock_cn", "currency": "CNY", "industry": "半导体"}
    master.sync_asset_master_from_analysis(cur, positions=[first], units=[], config={})
    before = _business_snapshot(cur)
    with pytest.raises(master.AmbiguousAssetIdentityError, match="sync refused"):
        master.sync_asset_master_from_analysis(cur, positions=[second], units=[], config={})
    assert _business_snapshot(cur) == before


def test_unqualified_exchange_and_qualified_symbol_do_not_get_assumed_equal(scratch_cursor):
    cur = scratch_cursor
    positions = [
        {"symbol": "600000", "asset_name": "Unknown", "asset_type": "stock_cn", "currency": "CNY"},
        {"symbol": "600000.SH", "asset_name": "Shanghai", "asset_type": "stock_cn", "currency": "CNY"},
    ]
    before = _business_snapshot(cur)
    with pytest.raises(master.AmbiguousAssetIdentityError, match="unqualified exchange"):
        master.sync_asset_master_from_analysis(cur, positions=positions, units=[], config={})
    assert _business_snapshot(cur) == before


def test_concurrent_conflicting_syncs_are_serialized_and_loser_rolls_back(scratch_cursor):
    from concurrent.futures import ThreadPoolExecutor
    from queue import Queue
    from time import monotonic, sleep

    cur = scratch_cursor
    cur.execute("SELECT current_schema() AS schema")
    schema = cur.fetchone()["schema"]
    cur.connection.commit()  # Publish only this isolated schema before opening a second connection.
    first = {"symbol": "600000.SH", "asset_name": "Shanghai", "asset_type": "stock_cn", "currency": "CNY", "industry": "银行"}
    second = {"symbol": "600000.SZ", "asset_name": "Shenzhen", "asset_type": "stock_cn", "currency": "CNY", "industry": "半导体"}
    unrelated = {"symbol": "000001.SH", "asset_name": "Unrelated", "asset_type": "stock_cn", "currency": "CNY"}
    master.sync_asset_master_from_analysis(cur, positions=[first], units=[], config={})
    before = _business_snapshot(cur)
    dsn, mode, name = resolve_asset_test_target()
    worker_pid: Queue[int] = Queue()

    def competing_sync():
        conn = psycopg2.connect(dsn, connect_timeout=2)
        try:
            validate_asset_test_database(conn, mode, name)
            with conn.cursor(cursor_factory=RealDictCursor) as other:
                other.execute(sql.SQL("SET search_path TO {}, pg_catalog").format(sql.Identifier(schema)))
                worker_pid.put(conn.get_backend_pid())
                try:
                    master.sync_asset_master_from_analysis(other, positions=[unrelated, second], units=[], config={})
                except master.AmbiguousAssetIdentityError:
                    conn.rollback()
                    return "ambiguous"
                conn.rollback()
                return "unexpected-success"
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(competing_sync)
        try:
            pid = worker_pid.get(timeout=5)
            deadline = monotonic() + 5
            waiting = False
            with psycopg2.connect(dsn, connect_timeout=2) as monitor, monitor.cursor() as monitor_cur:
                validate_asset_test_database(monitor, mode, name)
                while monotonic() < deadline:
                    monitor_cur.execute("SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid=%s AND locktype='advisory' AND NOT granted)", (pid,))
                    if monitor_cur.fetchone()[0]:
                        waiting = True
                        break
                    sleep(0.02)
            assert waiting, "competing request did not wait on the identity transaction lock"
        finally:
            cur.connection.commit()  # Release the first transaction even if the wait assertion fails.
        assert future.result(timeout=5) == "ambiguous"

    assert _business_snapshot(cur) == before
