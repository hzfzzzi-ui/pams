"""QDII proxy names must not become QQQ asset identities."""
from __future__ import annotations

import copy
import os
import sys
from pathlib import Path
from uuid import uuid4

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import parse_dsn
from psycopg2.extras import RealDictCursor
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "api"))
from domain.assets.asset_master import (  # noqa: E402
    sync_asset_master_from_analysis,
    sync_asset_master_from_lookthrough_config,
)
from domain.assets.lookthrough_state import derive_lookthrough_payload  # noqa: E402

FEEDER_NAMES = {
    "华泰柏瑞纳斯达克100ETF发起式联接(QDII)I",
    "广发纳斯达克100ETF联接人民币(QDII)A",
    "建信纳斯达克100指数(QDII)A人民币",
    "建信纳斯达克100指数(QDII)C人民币",
}


def config_and_mapping():
    config = yaml.safe_load((ROOT / "config/lookthrough.yaml").read_text(encoding="utf-8"))
    mapping = next(row for row in config["asset_mapping"] if row.get("symbol") == "QQQ")
    return config, mapping


def test_feeder_proxy_mapping_preserves_previous_exposure_results():
    config, mapping = config_and_mapping()
    assert set(mapping["lookthrough_aliases"]) == FEEDER_NAMES
    assert FEEDER_NAMES.isdisjoint(mapping["aliases"])
    legacy = copy.deepcopy(config)
    old_mapping = next(row for row in legacy["asset_mapping"] if row.get("symbol") == "QQQ")
    old_mapping["aliases"].extend(old_mapping.pop("lookthrough_aliases"))
    positions = [
        {"asset_name": name, "symbol": str(100001 + i), "asset_type": "fund",
         "currency": "CNY", "market_value_cny": 100.0, "as_of": "2026-06-19"}
        for i, name in enumerate(sorted(FEEDER_NAMES))
    ]
    before = derive_lookthrough_payload(copy.deepcopy(positions), legacy, as_of="2026-06-19")
    after = derive_lookthrough_payload(copy.deepcopy(positions), config, as_of="2026-06-19")
    assert after == before


def test_config_sync_does_not_seed_feeder_names_as_qqq_aliases():
    dsn = os.environ.get("PAMS_SCHEMA_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Explicit disposable schema-test PostgreSQL DSN required")
    assert parse_dsn(dsn).get("host") in {"127.0.0.1", "localhost", "::1"}
    config, mapping = config_and_mapping()
    config = {"asset_mapping": [mapping], "asset_lookthrough": []}
    with psycopg2.connect(dsn, connect_timeout=5) as conn:
        assert conn.server_version // 10000 == 16
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT shobj_description(oid, 'pg_database') AS marker FROM pg_database WHERE datname = current_database()")
            assert cur.fetchone()["marker"] == "pams-disposable-schema-test"
            name = "qdii_alias_test_" + uuid4().hex
            cur.execute(sql.SQL("CREATE SCHEMA {} ").format(sql.Identifier(name)))
            cur.execute(sql.SQL("SET LOCAL search_path TO {} ").format(sql.Identifier(name)))
            cur.execute((ROOT / "database/postgres/schema/023_asset_master.sql").read_text(encoding="utf-8"))
            for _ in range(2):
                sync_asset_master_from_lookthrough_config(cur, config=config, load_lookup=False)
            cur.execute("SELECT asset_id FROM pams_asset_master WHERE primary_symbol = %s", ("QQQ",))
            asset_id = cur.fetchone()["asset_id"]
            cur.execute("SELECT alias_value FROM pams_asset_alias WHERE asset_id = %s", (asset_id,))
            aliases = {row["alias_value"] for row in cur.fetchall()}
            assert "INVESCO QQQ TRUST SERIES 1" in aliases
            assert FEEDER_NAMES.isdisjoint(aliases)
            positions = [
                {"asset_name": value, "symbol": str(100001 + i), "asset_type": "fund", "currency": "CNY"}
                for i, value in enumerate(sorted(FEEDER_NAMES))
            ]
            sync_asset_master_from_analysis(cur, positions=positions, units=[], config=config)
            cur.execute("SELECT alias_value FROM pams_asset_alias WHERE asset_id = %s", (asset_id,))
            assert FEEDER_NAMES.isdisjoint({row["alias_value"] for row in cur.fetchall()})
            # Existing mistaken records need a separately authorized data correction.
            existing = sorted(FEEDER_NAMES)[0]
            cur.execute("INSERT INTO pams_asset_alias (asset_id, alias_type, alias_value) VALUES (%s, %s, %s)", (asset_id, "alias", existing))
            sync_asset_master_from_lookthrough_config(cur, config=config, load_lookup=False)
            cur.execute("SELECT count(*) AS n FROM pams_asset_alias WHERE asset_id = %s AND alias_value = %s", (asset_id, existing))
            assert cur.fetchone()["n"] == 1
        conn.rollback()
