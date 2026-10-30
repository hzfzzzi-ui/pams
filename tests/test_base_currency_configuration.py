"""Base currency comes from the asset configuration, without querying strategy policy."""
import os
from pathlib import Path
import sys

import psycopg2
from psycopg2.extensions import parse_dsn
from psycopg2.extras import RealDictCursor
import pytest

ROOT = Path(__file__).resolve().parents[1]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))
import main


class NoQueries:
    def execute(self, *_args, **_kwargs):
        pytest.fail("Base currency must not query the removed strategy policy")


@pytest.mark.parametrize("config, expected", [({"base_currency": "CNY"}, "CNY"),
                                             ({"base_currency": "USD"}, "USD"),
                                             ({}, "CNY"),
                                             (["CNY"], "CNY")])
def test_configuration_without_query(monkeypatch, config, expected):
    monkeypatch.setattr(main, "load_risk_limits", lambda: config)
    assert main._get_base_currency(NoQueries()) == expected


def test_missing_configuration_falls_back(monkeypatch):
    def missing():
        raise FileNotFoundError
    monkeypatch.setattr(main, "load_risk_limits", missing)
    assert main._get_base_currency(NoQueries()) == "CNY"


@pytest.fixture
def isolated_connection():
    dsn = os.environ.get("PAMS_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Explicit disposable PostgreSQL DSN required")
    assert parse_dsn(dsn).get("host") in {"127.0.0.1", "localhost", "::1"}
    conn = psycopg2.connect(dsn, connect_timeout=5)
    assert conn.server_version // 10000 == 16
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = current_database()")
            assert cur.fetchone()[0] == "pams-disposable-schema-test"
        conn.commit()
        yield conn
    finally:
        conn.close()


def test_missing_policy_keeps_transaction_usable(isolated_connection, monkeypatch):
    monkeypatch.setattr(main, "load_risk_limits", lambda: {"base_currency": "CNY"})
    with isolated_connection.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT to_regclass('public.pams_policy') AS rel")
        assert cur.fetchone()["rel"] is None
        assert main._get_base_currency(cur) == "CNY"
        cur.execute("SELECT 1 AS value")
        assert cur.fetchone()["value"] == 1


@pytest.mark.parametrize("currency", ["CNY", "USD"])
def test_matching_existing_policy_returns_same_value(isolated_connection, monkeypatch, currency):
    monkeypatch.setattr(main, "load_risk_limits", lambda: {"base_currency": currency})
    with isolated_connection.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("CREATE TEMP TABLE pams_policy (key text, value jsonb)")
        cur.execute("INSERT INTO pams_policy VALUES ('ips_v1', jsonb_build_object('base_currency', %s::text))", (currency,))
        cur.execute("SELECT value->>'base_currency' AS base_currency FROM pams_policy WHERE key='ips_v1'")
        old_value = cur.fetchone()["base_currency"]
        assert main._get_base_currency(cur) == old_value
        cur.execute("SELECT 1 AS value")
        assert cur.fetchone()["value"] == 1


def test_configuration_wins_if_existing_policy_differs(isolated_connection, monkeypatch):
    monkeypatch.setattr(main, "load_risk_limits", lambda: {"base_currency": "CNY"})
    with isolated_connection.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("CREATE TEMP TABLE pams_policy (key text, value jsonb)")
        cur.execute("INSERT INTO pams_policy VALUES ('ips_v1', '{\"base_currency\":\"USD\"}')")
        assert main._get_base_currency(cur) == "CNY"
        cur.execute("SELECT value->>'base_currency' AS base_currency FROM pams_policy WHERE key='ips_v1'")
        assert cur.fetchone()["base_currency"] == "USD"


def test_existing_policy_without_currency_matches_cny_configuration(isolated_connection, monkeypatch):
    monkeypatch.setattr(main, "load_risk_limits", lambda: {"base_currency": "CNY"})
    with isolated_connection.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("CREATE TEMP TABLE pams_policy (key text, value jsonb)")
        cur.execute("INSERT INTO pams_policy VALUES ('ips_v1', '{}'::jsonb)")
        cur.execute("SELECT value->>'base_currency' AS base_currency FROM pams_policy WHERE key='ips_v1'")
        old_value = cur.fetchone()["base_currency"] or "CNY"
        assert main._get_base_currency(cur) == old_value == "CNY"
        cur.execute("SELECT 1 AS value")
        assert cur.fetchone()["value"] == 1
