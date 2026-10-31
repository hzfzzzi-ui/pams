"""Exercise the candidate only against a newly-created marked test database."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import os
from pathlib import Path
import sys
from uuid import uuid4

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import parse_dsn
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend/api"))
from domain.investments.valuation_input_evidence import (
    record_result_version, recorded_input_freshness, verify_recorded_version,
)


def _input(kind, identity, value):
    tables = {"holding": "pams_holdings_snapshot", "trade": "pams_investment_trade_fact",
              "quote": "pams_market_quote_snapshot", "fx": "pams_fx_rates_daily"}
    ref = {"table": tables[kind], "key": {"id": identity}}
    if kind in {"holding", "trade"}:
        ref["account_code"] = "acct"
    return {"kind": kind, "source_ref": ref, "source_version": "v1",
            "business_date": "2026-01-02", "observed_at": "2026-01-02T12:00:00+00:00",
            "quality": "confirmed", "snapshot": {"value": value}}


def _inputs():
    return [_input("holding", "h1", "1"), _input("trade", "t1", "2"),
            _input("trade", "t2", "3"), _input("quote", "q1", "4"), _input("fx", "fx1", "5")]


def _record(cur, inputs, scope="acct/a"):
    return record_result_version(cur, result_kind="holding", scope_key=scope,
        business_date=date(2026, 1, 2), base_currency="CNY", calculation_version="test-v1",
        result_hash="a" * 64, inputs=inputs, account_code="acct")


def test_freshness_never_guesses_unknown_inputs():
    original = _inputs()
    assert recorded_input_freshness(original, list(reversed(original))) == "unchanged_for_recorded_inputs"
    changed = _inputs()
    changed[-1]["snapshot"] = {"value": "6"}
    assert recorded_input_freshness(original, changed) == "changed"
    missing = _inputs()
    missing[-1]["observed_at"] = None
    assert recorded_input_freshness(original, missing) == "unknown"
    assert recorded_input_freshness(original, original[:-1]) == "unknown"
    assert recorded_input_freshness([], []) == "unknown"
    empty = _inputs()
    empty[0]["snapshot"] = {}
    assert recorded_input_freshness(original, empty) == "unknown"
    assert recorded_input_freshness(empty, empty) == "unknown"
    no_account = _inputs()
    del no_account[0]["source_ref"]["account_code"]
    assert recorded_input_freshness(no_account, no_account) == "unknown"
    invalid_ref = _inputs()
    invalid_ref[-1]["source_ref"]["key"]["id"] = ""
    with pytest.raises(ValueError, match="source_ref"):
        recorded_input_freshness(invalid_ref, invalid_ref)
    unknown = _inputs()
    unknown[0]["source_ref"] = None
    assert recorded_input_freshness(original, unknown) == "unknown"


@pytest.mark.parametrize("field,value", [
    ("source_version", "v2"), ("business_date", "2026-01-03"),
    ("observed_at", "2026-01-02T13:00:00+00:00"), ("quality", "estimated"),
])
def test_changed_input_attributes_invalidate_evidence(field, value):
    changed = _inputs()
    changed[2][field] = value
    assert recorded_input_freshness(_inputs(), changed) == "changed"


def test_disposable_database_versions_are_atomic_immutable_and_idempotent():
    dsn = os.environ.get("PAMS_SCHEMA_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("explicit disposable PostgreSQL DSN required")
    params = parse_dsn(dsn)
    assert params.get("host") in {"127.0.0.1", "localhost", "::1"}
    admin = psycopg2.connect(dsn, connect_timeout=5)
    assert admin.server_version // 10000 == 16
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute("SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname=current_database()")
        assert cur.fetchone()[0] == "pams-disposable-schema-test"
    name = "pams_evidence_test_" + uuid4().hex
    with admin.cursor() as cur:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    target = {**params, "dbname": name}
    try:
        with psycopg2.connect(**target) as conn:
            with conn.cursor() as cur:
                cur.execute((ROOT / "database/postgres/schema/083_valuation_input_evidence_candidate.sql").read_text(encoding="utf-8"))
                cur.execute((ROOT / "database/postgres/schema/083_valuation_input_evidence_candidate.sql").read_text(encoding="utf-8"))
                key = _record(cur, _inputs())
                assert key == _record(cur, list(reversed(_inputs())))
                assert verify_recorded_version(cur, key)
            conn.commit()
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM public.pams_valuation_result_version")
                assert cur.fetchone()[0] == 1
                cur.execute("SELECT count(*) FROM public.pams_valuation_input_evidence WHERE version_key=%s", (key,))
                assert cur.fetchone()[0] == 5
                for statement, args in [
                    ("UPDATE public.pams_valuation_result_version SET scope_key='altered' WHERE version_key=%s", (key,)),
                    ("DELETE FROM public.pams_valuation_input_evidence WHERE version_key=%s", (key,)),
                    ("TRUNCATE public.pams_valuation_input_evidence", ()),
                    ("INSERT INTO public.pams_valuation_input_evidence VALUES (%s, %s, 'fx', '{}'::jsonb)", (key, "b" * 64)),
                ]:
                    cur.execute("SAVEPOINT rejected")
                    with pytest.raises(psycopg2.errors.RaiseException):
                        cur.execute(statement, args)
                    cur.execute("ROLLBACK TO SAVEPOINT rejected")
                assert verify_recorded_version(cur, key)
            conn.rollback()
            with conn.cursor() as cur:
                conflict = _inputs()
                conflict[1]["source_ref"]["account_code"] = "different"
                with pytest.raises(ValueError, match="account conflicts"):
                    _record(cur, conflict)
                changed = _inputs()
                changed[-1]["snapshot"] = {"value": "6"}
                changed_key = _record(cur, changed)
                assert changed_key != key and verify_recorded_version(cur, changed_key)
                missing = _inputs()
                missing[0]["snapshot"] = None
                unknown_key = _record(cur, missing)
                assert verify_recorded_version(cur, unknown_key)
                cur.execute("SELECT evidence FROM public.pams_valuation_input_evidence WHERE version_key=%s AND input_kind='holding'", (unknown_key,))
                assert cur.fetchone()[0]["snapshot"] is None
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM public.pams_valuation_result_version")
                assert cur.fetchone()[0] == 1
                cur.execute("SELECT count(*) FROM public.pams_valuation_input_evidence WHERE version_key=%s", (changed_key,))
                assert cur.fetchone()[0] == 0
            conn.rollback()

        def create_same_version():
            with psycopg2.connect(**target) as connection:
                with connection.cursor() as cursor:
                    return _record(cursor, _inputs(), scope="acct/concurrent")

        with ThreadPoolExecutor(max_workers=2) as pool:
            keys = list(pool.map(lambda _: create_same_version(), range(2)))
        assert keys[0] == keys[1]
        with psycopg2.connect(**target) as conn:
            with conn.cursor() as cur:
                assert verify_recorded_version(cur, keys[0])
                cur.execute("SELECT count(*) FROM public.pams_valuation_result_version")
                assert cur.fetchone()[0] == 2
    finally:
        with admin.cursor() as cur:
            cur.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()
