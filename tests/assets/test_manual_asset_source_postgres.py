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



def test_automatic_sync_preserves_explicit_manual_asset_and_industry_evidence(scratch_cursor):
    from psycopg2.extras import Json

    cur = scratch_cursor
    raw = {"symbol": "sh600000", "asset_name": "automatic name", "asset_type": "stock_cn", "currency": "CNY", "industry": "半导体"}
    asset = master._asset_record_from_raw(master._raw_from_position(raw), source="system")
    cur.execute(
        "INSERT INTO pams_asset_master(asset_id,canonical_name,primary_symbol,asset_type,source,confidence,raw_json) VALUES (%s,%s,%s,%s,'manual','high',%s)",
        (asset["asset_id"], "manual name", "sh600000", asset["asset_type"], Json({"industry": "manual industry", "note": "user evidence"})),
    )
    cur.execute(
        "INSERT INTO pams_asset_industry_map(asset_id,industry_system,industry_leaf,industry_bucket,classification_scope,source,confidence,evidence,is_primary) VALUES (%s,'manual','manual industry','金融地产','listed_equity','manual','high','user evidence',TRUE)",
        (asset["asset_id"],),
    )
    for _ in range(2):
        master.sync_asset_master_from_analysis(cur, positions=[raw], units=[], config={})
        cur.execute("SELECT source,canonical_name,raw_json FROM pams_asset_master WHERE asset_id=%s", (asset["asset_id"],))
        stored = cur.fetchone()
        assert stored["source"] == "manual"
        assert stored["canonical_name"] == "manual name"
        assert stored["raw_json"]["industry"] == "manual industry"
        assert stored["raw_json"]["note"] == "user evidence"
        cur.execute("SELECT source,industry_leaf,industry_bucket,confidence,evidence FROM pams_asset_industry_map WHERE asset_id=%s AND industry_system='manual' AND classification_scope='listed_equity' AND is_primary", (asset["asset_id"],))
        assert dict(cur.fetchone()) == {"source": "manual", "industry_leaf": "manual industry", "industry_bucket": "金融地产", "confidence": "high", "evidence": "user evidence"}


def test_automatic_industry_records_still_update_with_new_evidence(scratch_cursor):
    cur = scratch_cursor
    raw = {"symbol": "sh600001", "asset_name": "automatic stock", "asset_type": "stock_cn", "currency": "CNY", "industry": "银行"}
    master.sync_asset_master_from_analysis(cur, positions=[raw], units=[], config={})
    asset_id = master._asset_record_from_raw(master._raw_from_position(raw), source="system")["asset_id"]
    cur.execute("SELECT source,industry_bucket FROM pams_asset_industry_map WHERE asset_id=%s", (asset_id,))
    assert dict(cur.fetchone()) == {"source": "system", "industry_bucket": "金融地产"}
    updated = {**raw, "industry": "半导体"}
    cur.execute("UPDATE pams_asset_master SET last_verified_at='2001-01-01 00:00:00+00' WHERE asset_id=%s", (asset_id,))
    master.sync_asset_master_from_analysis(cur, positions=[updated], units=[], config={})
    cur.execute("SELECT source,industry_leaf,industry_bucket FROM pams_asset_industry_map WHERE asset_id=%s", (asset_id,))
    assert dict(cur.fetchone()) == {"source": "system", "industry_leaf": "半导体", "industry_bucket": "科技TMT"}
    cur.execute("SELECT raw_json FROM pams_asset_master WHERE asset_id=%s", (asset_id,))
    assert cur.fetchone()["raw_json"]["industry"] == "半导体"
    cur.execute("SELECT last_verified_at > '2001-01-01 00:00:00+00'::timestamptz AS refreshed FROM pams_asset_master WHERE asset_id=%s", (asset_id,))
    assert cur.fetchone()["refreshed"]


def test_explicit_manual_industry_update_remains_possible(scratch_cursor):
    cur = scratch_cursor
    raw = {"symbol": "sh600002", "asset_name": "manual stock", "asset_type": "stock_cn", "currency": "CNY", "industry": "银行"}
    asset_id = master._upsert_asset(cur, raw, source="manual")
    asset = master._asset_record_from_raw(raw, source="manual")
    master._upsert_industry(cur, asset_id, {**raw, "industry": "半导体", "evidence": "manual correction"}, asset, source="manual")
    cur.execute("SELECT source,industry_leaf,industry_bucket,evidence FROM pams_asset_industry_map WHERE asset_id=%s", (asset_id,))
    assert dict(cur.fetchone()) == {"source": "manual", "industry_leaf": "半导体", "industry_bucket": "科技TMT", "evidence": "manual correction"}


def test_manual_primary_industry_wins_over_newer_auto_classification(scratch_cursor):
    from psycopg2.extras import Json

    cur = scratch_cursor
    raw = {"symbol": "sh600003", "asset_name": "synthetic", "asset_type": "stock_cn", "currency": "CNY", "industry": "半导体", "sw_l1": "电子"}
    asset = master._asset_record_from_raw(master._raw_from_position(raw), source="system")
    cur.execute("INSERT INTO pams_asset_master(asset_id,canonical_name,primary_symbol,asset_type,source,raw_json) VALUES (%s,%s,%s,%s,'manual',%s)",
                (asset["asset_id"], "manual stock", "sh600003", asset["asset_type"], Json({"note": "manual evidence"})))
    cur.execute("INSERT INTO pams_asset_industry_map(asset_id,industry_system,industry_leaf,industry_bucket,classification_scope,source,evidence,is_primary) VALUES (%s,'manual','银行','金融地产','listed_equity','manual','manual evidence',TRUE)", (asset["asset_id"],))
    lookup = master.sync_asset_master_from_analysis(cur, positions=[raw], units=[], config={})
    cur.execute("SELECT source,industry_system,industry_bucket FROM pams_asset_industry_map WHERE asset_id=%s ORDER BY source", (asset["asset_id"],))
    assert [dict(row) for row in cur.fetchall()] == [
        {"source": "manual", "industry_system": "manual", "industry_bucket": "金融地产"},
        {"source": "system", "industry_system": "SW", "industry_bucket": "科技TMT"},
    ]
    assert lookup["assets"][asset["asset_id"]]["industry"]["source"] == "manual"
    assert lookup["assets"][asset["asset_id"]]["industry"]["industry_bucket"] == "金融地产"
    assert lookup["assets"][asset["asset_id"]]["industry"]["evidence"] == "manual evidence"


def test_nullable_legacy_source_does_not_override_manual_or_block_auto_update(scratch_cursor):
    cur = scratch_cursor
    cur.execute("ALTER TABLE pams_asset_industry_map ALTER COLUMN source DROP NOT NULL")
    raw = {"symbol": "sh600004", "asset_name": "nullable source", "asset_type": "stock_cn",
           "currency": "CNY", "industry": "半导体", "sw_l1": "电子"}
    master.sync_asset_master_from_analysis(cur, positions=[raw], units=[], config={})
    asset_id = master._asset_record_from_raw(master._raw_from_position(raw), source="system")["asset_id"]
    cur.execute("UPDATE pams_asset_industry_map SET source=NULL WHERE asset_id=%s", (asset_id,))
    cur.execute("INSERT INTO pams_asset_industry_map(asset_id,industry_system,industry_leaf,industry_bucket,classification_scope,source,evidence,is_primary) VALUES (%s,'manual','银行','金融地产','listed_equity','manual','user evidence',TRUE)", (asset_id,))
    lookup = master.load_asset_master_lookup(cur)
    assert lookup["assets"][asset_id]["industry"]["source"] == "manual"
    assert lookup["assets"][asset_id]["industry"]["evidence"] == "user evidence"
    master.sync_asset_master_from_analysis(cur, positions=[{**raw, "industry": "银行"}], units=[], config={})
    cur.execute("SELECT source,industry_leaf FROM pams_asset_industry_map WHERE asset_id=%s AND industry_system='SW'", (asset_id,))
    assert dict(cur.fetchone()) == {"source": "system", "industry_leaf": "银行"}
    cur.execute("SELECT source,evidence FROM pams_asset_industry_map WHERE asset_id=%s AND industry_system='manual'", (asset_id,))
    assert dict(cur.fetchone()) == {"source": "manual", "evidence": "user evidence"}


def test_automatic_sync_preserves_manual_verification_timestamp(scratch_cursor):
    cur = scratch_cursor
    raw = {"symbol": "sh600005", "asset_name": "verified manual", "asset_type": "stock_cn",
           "currency": "CNY", "industry": "银行"}
    asset_id = master._upsert_asset(cur, raw, source="manual")
    cur.execute("UPDATE pams_asset_master SET last_verified_at='2001-01-01 00:00:00+00' WHERE asset_id=%s", (asset_id,))
    master.sync_asset_master_from_analysis(cur, positions=[raw], units=[], config={})
    cur.execute("SELECT last_verified_at = '2001-01-01 00:00:00+00'::timestamptz AS preserved FROM pams_asset_master WHERE asset_id=%s", (asset_id,))
    assert cur.fetchone()["preserved"]
    cur.execute("UPDATE pams_asset_master SET last_verified_at=NULL WHERE asset_id=%s", (asset_id,))
    master.sync_asset_master_from_analysis(cur, positions=[raw], units=[], config={})
    cur.execute("SELECT last_verified_at IS NULL AS preserved FROM pams_asset_master WHERE asset_id=%s", (asset_id,))
    assert cur.fetchone()["preserved"]
    master._upsert_asset(cur, raw, source="manual")
    cur.execute("SELECT last_verified_at > '2001-01-01 00:00:00+00'::timestamptz AS refreshed FROM pams_asset_master WHERE asset_id=%s", (asset_id,))
    assert cur.fetchone()["refreshed"]
