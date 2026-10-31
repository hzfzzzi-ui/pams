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
    bound_holding_freshness, record_bound_holding_version, record_result_version,
    recorded_input_freshness, verify_recorded_version, versions_for_source_item,
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
        cur.execute(sql.SQL("COMMENT ON DATABASE {} IS 'pams-empty-bootstrap'").format(sql.Identifier(name)))
    target = {**params, "dbname": name}
    try:
        with psycopg2.connect(**target) as conn:
            from scripts.bootstrap_empty_database import initialize_empty_database
            initialize_empty_database(conn)
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

                # Use actual empty-bootstrap table definitions, populated only
                # with synthetic records and never with production data.
                cur.execute("INSERT INTO pams_accounts (account_code, display_name) VALUES ('acct', 'test'), ('other', 'test')")
                cur.execute("""INSERT INTO pams_holdings_snapshot
                    (name, asset_liability, currency, account_code, major_category, minor_category, market_value, as_of)
                    VALUES ('synthetic', '资产', 'CNY', 'acct', 'test', 'test', 100, '2026-01-02') RETURNING id""")
                holding_id = cur.fetchone()[0]
                cur.execute("""INSERT INTO pams_investment_trade_fact
                    (trade_date, account_code, source_table, source_id, source_hash)
                    VALUES ('2026-01-01', 'acct', 'synthetic', 't1', %s) RETURNING trade_id""", ("1" * 64,))
                trade_id = cur.fetchone()[0]
                cur.execute("""INSERT INTO pams_market_quote_snapshot
                    (quote_date, provider, source, source_key, return_status, price, price_time)
                    VALUES ('2026-01-02', 'synthetic', 'synthetic', 'q1', 'confirmed', 5, '2026-01-02T12:00:00Z') RETURNING quote_id""")
                quote_id = cur.fetchone()[0]
                cur.execute("""INSERT INTO pams_fx_rates_daily (as_of, base_currency, quote_currency, rate, source)
                    VALUES ('2026-01-02', 'CNY', 'USD', 7, 'synthetic')""")
            conn.commit()
            refs = [{"kind": "trade", "key": {"trade_id": trade_id}},
                    {"kind": "quote", "key": {"quote_id": quote_id}},
                    {"kind": "fx", "key": {"as_of": "2026-01-02", "base_currency": "CNY", "quote_currency": "USD"}},
                    {"kind": "source_item", "role": "ibkr_report"}]
            with conn.cursor() as cur:
                bound_key = record_bound_holding_version(cur, holding_id=holding_id, calculation_version="v1", input_refs=refs)
                assert bound_key == record_bound_holding_version(cur, holding_id=holding_id, calculation_version="v1", input_refs=list(reversed(refs)))
                assert verify_recorded_version(cur, bound_key)
                cur.execute("SELECT evidence FROM public.pams_valuation_input_evidence WHERE version_key=%s AND input_kind='source_item'", (bound_key,))
                unknown = cur.fetchone()[0]
                assert unknown["role"] == "ibkr_report" and unknown["source_ref"] is None and unknown["snapshot"] is None
                cur.execute("SELECT evidence FROM public.pams_valuation_input_evidence WHERE version_key=%s AND input_kind='holding'", (bound_key,))
                original_holding = cur.fetchone()[0]["snapshot"]["content"]
                assert original_holding["market_value"] == "100.0000"
                with psycopg2.connect(**target) as other:
                    with other.cursor() as other_cur:
                        other_cur.execute("SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0))", ("account-import:acct",))
                        assert other_cur.fetchone()[0] is False
                # Historical holding/trade/FX quality and observation times are
                # not invented merely because references were found.
                assert bound_holding_freshness(cur, bound_key) == "unknown"
            conn.commit()
            with conn.cursor() as cur:
                cur.execute("UPDATE pams_market_quote_snapshot SET price=6 WHERE quote_id=%s", (quote_id,))
                assert bound_holding_freshness(cur, bound_key) == "changed"
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute("UPDATE pams_fx_rates_daily SET rate=8 WHERE as_of='2026-01-02' AND base_currency='CNY' AND quote_currency='USD'")
                assert bound_holding_freshness(cur, bound_key) == "changed"
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute("UPDATE pams_holdings_snapshot SET market_value=101 WHERE id=%s", (holding_id,))
                assert bound_holding_freshness(cur, bound_key) == "changed"
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute("UPDATE pams_market_quote_snapshot SET quote_date='2026-01-03' WHERE quote_id=%s", (quote_id,))
                assert bound_holding_freshness(cur, bound_key) == "inconsistent"
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute("UPDATE pams_investment_trade_fact SET account_code='other' WHERE trade_id=%s", (trade_id,))
                assert bound_holding_freshness(cur, bound_key) == "inconsistent"
                with pytest.raises(ValueError, match="account or business date"):
                    record_bound_holding_version(cur, holding_id=holding_id, calculation_version="v1", input_refs=refs)
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute("UPDATE pams_investment_trade_fact SET trade_date='2026-01-03' WHERE trade_id=%s", (trade_id,))
                assert bound_holding_freshness(cur, bound_key) == "inconsistent"
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute("INSERT INTO pams_fx_rates_daily (as_of, base_currency, quote_currency, rate, source) VALUES ('2026-01-03', 'CNY', 'USD', 7, 'synthetic')")
                wrong_date = [{"kind": "fx", "key": {"as_of": "2026-01-03", "base_currency": "CNY", "quote_currency": "USD"}}]
                with pytest.raises(ValueError, match="business date"):
                    record_bound_holding_version(cur, holding_id=holding_id, calculation_version="v1", input_refs=wrong_date)
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute("DELETE FROM pams_market_quote_snapshot WHERE quote_id=%s", (quote_id,))
                assert bound_holding_freshness(cur, bound_key) == "missing"
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute("DELETE FROM pams_holdings_snapshot WHERE id=%s", (holding_id,))
                cur.execute("""INSERT INTO pams_holdings_snapshot
                    (name, asset_liability, currency, account_code, major_category, minor_category, market_value, as_of)
                    VALUES ('synthetic', '资产', 'CNY', 'acct', 'test', 'test', 105, '2026-01-02') RETURNING id""")
                replacement_id = cur.fetchone()[0]
                assert bound_holding_freshness(cur, bound_key) == "replacement_candidate"
                new_key = record_bound_holding_version(cur, holding_id=replacement_id, calculation_version="v1", input_refs=refs)
                assert new_key != bound_key and verify_recorded_version(cur, bound_key)
                cur.execute("SELECT scope_key FROM public.pams_valuation_result_version WHERE version_key IN (%s,%s)", (bound_key, new_key))
                scopes = [row[0] for row in cur.fetchall()]
                assert len(scopes) == 2 and scopes[0] == scopes[1]
                cur.execute("SELECT evidence FROM public.pams_valuation_input_evidence WHERE version_key=%s AND input_kind='holding'", (bound_key,))
                assert cur.fetchone()[0]["snapshot"]["content"] == original_holding
            conn.rollback()
            with conn.cursor() as cur:
                missing_refs = [{"kind": "quote", "key": {"quote_id": quote_id + 999}}]
                missing_key = record_bound_holding_version(cur, holding_id=holding_id, calculation_version="v1", input_refs=missing_refs)
                assert bound_holding_freshness(cur, missing_key) == "unknown"
                with pytest.raises(ValueError, match="only source kind, key and role"):
                    record_bound_holding_version(cur, holding_id=holding_id, calculation_version="v1",
                        input_refs=[{"kind": "quote", "key": {"quote_id": quote_id}, "snapshot": {"price": "forged"}}])
                with pytest.raises(ValueError, match="invalid fx date"):
                    record_bound_holding_version(cur, holding_id=holding_id, calculation_version="v1",
                        input_refs=[{"kind": "fx", "key": {"as_of": "bad", "base_currency": "CNY", "quote_currency": "USD"}}])
                cur.execute("SELECT market_value FROM pams_holdings_snapshot WHERE id=%s", (holding_id,))
                assert str(cur.fetchone()[0]) == "100.0000"
            conn.rollback()
            with conn.cursor() as cur:
                # Candidate source schema is isolated and mirrors the fields
                # read from source_batches/source_items; no external data used.
                cur.execute("""CREATE TABLE public.source_batches (
                    batch_id uuid PRIMARY KEY, account_code text NOT NULL, business_type text NOT NULL,
                    status text NOT NULL, date_from date, date_to date, published_source_hash text)""")
                cur.execute("""CREATE TABLE public.source_items (
                    source_item_id uuid PRIMARY KEY, batch_id uuid NOT NULL REFERENCES public.source_batches,
                    record_hash text NOT NULL, review_status text NOT NULL, raw_fields jsonb NOT NULL)""")
                cur.execute("ALTER TABLE public.pams_holdings_snapshot ADD COLUMN source_item_id uuid REFERENCES public.source_items")
                batch1, batch2, draft_batch, foreign_batch, future_batch = (str(uuid4()) for _ in range(5))
                item1, item2, draft_item, foreign_item, future_item, pending_item = (str(uuid4()) for _ in range(6))
                for batch, status in ((batch1, 'published'), (batch2, 'published'), (draft_batch, 'draft')):
                    cur.execute("""INSERT INTO public.source_batches VALUES
                        (%s,'acct','holding',%s,'2026-01-02','2026-01-02',%s)""",
                        (batch, status, "b" * 64))
                for item, batch in ((item1, batch1), (item2, batch2), (draft_item, draft_batch)):
                    cur.execute("INSERT INTO public.source_items VALUES (%s,%s,%s,'confirmed','{}')", (item, batch, "c" * 64))
                cur.execute("INSERT INTO public.source_batches VALUES (%s,'other','holding','published','2026-01-02','2026-01-02',%s)", (foreign_batch, "b" * 64))
                cur.execute("INSERT INTO public.source_batches VALUES (%s,'acct','holding','published','2026-01-03','2026-01-03',%s)", (future_batch, "b" * 64))
                for item, batch, review in ((foreign_item, foreign_batch, 'confirmed'), (future_item, future_batch, 'confirmed'), (pending_item, batch2, 'pending')):
                    cur.execute("INSERT INTO public.source_items VALUES (%s,%s,%s,%s,'{}')", (item, batch, "c" * 64, review))
                cur.execute("UPDATE pams_holdings_snapshot SET source_item_id=%s WHERE id=%s", (item1, holding_id))
            conn.commit()
            with conn.cursor() as cur:
                old_key = record_bound_holding_version(cur, holding_id=holding_id, calculation_version="v2", input_refs=[])
                old_versions = versions_for_source_item(cur, item1)
                assert [row["version_key"] for row in old_versions] == [old_key]
                cur.execute("SELECT evidence FROM public.pams_valuation_input_evidence WHERE version_key=%s AND input_kind='holding'", (old_key,))
                old_snapshot = cur.fetchone()[0]["snapshot"]["content"]
                assert old_snapshot["source_item_id"] == item1 and old_snapshot["market_value"] == "100.0000"
                for rejected in (draft_item, foreign_item, future_item, pending_item):
                    with pytest.raises(ValueError, match="account or business date"):
                        record_bound_holding_version(cur, holding_id=holding_id, calculation_version="v2",
                            input_refs=[{"kind": "source_item", "role": "new_holding_source", "key": {"source_item_id": rejected}}])
            conn.commit()
            with conn.cursor() as cur:
                cur.execute("UPDATE pams_holdings_snapshot SET market_value=105 WHERE id=%s", (holding_id,))
                new_key = record_bound_holding_version(cur, holding_id=holding_id, calculation_version="v2",
                    input_refs=[{"kind": "source_item", "role": "new_holding_source", "key": {"source_item_id": item2}}])
                assert new_key != old_key
                assert {row["version_key"] for row in versions_for_source_item(cur, item1)} == {old_key, new_key}
                assert [row["version_key"] for row in versions_for_source_item(cur, item2)] == [new_key]
                assert bound_holding_freshness(cur, old_key) == "changed"
                cur.execute("SELECT evidence FROM public.pams_valuation_input_evidence WHERE version_key=%s AND input_kind='holding'", (old_key,))
                assert cur.fetchone()[0]["snapshot"]["content"] == old_snapshot
            conn.rollback()
    finally:
        with admin.cursor() as cur:
            cur.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()
