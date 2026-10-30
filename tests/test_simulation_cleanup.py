"""Rehearse simulation removal only in a marked, disposable PostgreSQL database."""

import os
from pathlib import Path
from uuid import uuid4

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import parse_dsn
import pytest


SCRIPT = (Path(__file__).resolve().parents[1] / "database/postgres/cleanup/simulation_rehearsal.sql").read_text(encoding="utf-8")
REMOVED = (
    "pams_sim_accounts", "pams_sim_holdings_snapshots", "pams_sim_cash_ledger",
    "pams_sim_sync_logs", "pams_sim_reconcile_summaries",
)
PRESERVED = (
    "pams_holdings_snapshot", "pams_cashflow_fact", "pams_investment_trade_fact",
    "pams_transaction_sync_log", "pams_transaction_lifecycle", "pams_sentiment_state",
    "pams_ohlcv_daily", "pit_trade_cal",
)


@pytest.fixture
def isolated_database():
    dsn = os.environ.get("PAMS_SCHEMA_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Explicit disposable schema-test PostgreSQL DSN required")
    params = parse_dsn(dsn)
    assert params.get("host") in {"127.0.0.1", "localhost", "::1"}
    admin = psycopg2.connect(dsn, connect_timeout=5)
    assert admin.server_version // 10000 == 16
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute("SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = current_database()")
        assert cur.fetchone()[0] == "pams-disposable-schema-test"
    name = "pams_sim_cleanup_test_" + uuid4().hex
    with admin.cursor() as cur:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        cur.execute(sql.SQL("COMMENT ON DATABASE {} IS 'pams-isolated-simulation-cleanup'").format(sql.Identifier(name)))
    try:
        yield {**params, "dbname": name}
    finally:
        with admin.cursor() as cur:
            cur.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()


def prepare(cur):
    cur.execute("CREATE SCHEMA pams_module")
    for name in REMOVED:
        cur.execute(sql.SQL("CREATE TABLE pams_module.{} (id int PRIMARY KEY, value text)").format(sql.Identifier(name)))
        cur.execute(sql.SQL("INSERT INTO pams_module.{} VALUES (1, 'simulation')").format(sql.Identifier(name)))
        cur.execute(sql.SQL("CREATE VIEW public.{} AS SELECT * FROM pams_module.{}").format(
            sql.Identifier(name), sql.Identifier(name)))
    for name in PRESERVED:
        cur.execute(sql.SQL("CREATE TABLE public.{} (id int PRIMARY KEY, value text)").format(sql.Identifier(name)))
        cur.execute(sql.SQL("INSERT INTO public.{} VALUES (1, 'preserve')").format(sql.Identifier(name)))


def test_simulation_rehearsal_removes_only_simulation_and_is_idempotent(isolated_database):
    with psycopg2.connect(**isolated_database) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            prepare(cur)
            conn.commit()
            cur.execute("SET pams.sim_cleanup_rehearsal = 'on'")
            cur.execute(SCRIPT)
            cur.execute(SCRIPT)
            for name in REMOVED:
                cur.execute("SELECT to_regclass(%s), to_regclass(%s)", ("public." + name, "pams_module." + name))
                assert cur.fetchone() == (None, None)
            for name in PRESERVED:
                cur.execute(sql.SQL("SELECT value FROM public.{} WHERE id = 1").format(sql.Identifier(name)))
                assert cur.fetchone()[0] == "preserve"


def test_simulation_rehearsal_rolls_back_on_unknown_dependency(isolated_database):
    with psycopg2.connect(**isolated_database) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            prepare(cur)
            cur.execute("CREATE VIEW public.unexpected_sim_consumer AS SELECT * FROM public.pams_sim_accounts")
            conn.commit()
            cur.execute("SET pams.sim_cleanup_rehearsal = 'on'")
            with pytest.raises(psycopg2.errors.DependentObjectsStillExist):
                cur.execute(SCRIPT)
            cur.execute("ROLLBACK")
            for name in REMOVED:
                cur.execute("SELECT to_regclass(%s), to_regclass(%s)", ("public." + name, "pams_module." + name))
                assert all(cur.fetchone())


def test_simulation_rehearsal_refuses_unexpected_public_table(isolated_database):
    with psycopg2.connect(**isolated_database) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            prepare(cur)
            cur.execute("DROP VIEW public.pams_sim_holdings_snapshots")
            cur.execute("CREATE TABLE public.pams_sim_holdings_snapshots (id int PRIMARY KEY)")
            cur.execute("INSERT INTO public.pams_sim_holdings_snapshots VALUES (1)")
            conn.commit()
            cur.execute("SET pams.sim_cleanup_rehearsal = 'on'")
            with pytest.raises(psycopg2.errors.WrongObjectType):
                cur.execute(SCRIPT)
            cur.execute("ROLLBACK")
            cur.execute("SELECT count(*) FROM public.pams_sim_holdings_snapshots")
            assert cur.fetchone()[0] == 1
            for name in REMOVED:
                cur.execute("SELECT to_regclass(%s), to_regclass(%s)", ("public." + name, "pams_module." + name))
                assert all(cur.fetchone())


def test_simulation_rehearsal_guard_refuses_missing_switch(isolated_database):
    with psycopg2.connect(**isolated_database) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            prepare(cur)
            conn.commit()
            with pytest.raises(psycopg2.errors.RaiseException):
                cur.execute(SCRIPT)
            cur.execute("ROLLBACK")
            cur.execute("SELECT value FROM public.pams_sim_accounts WHERE id = 1")
            assert cur.fetchone()[0] == "simulation"


def test_simulation_rehearsal_guard_refuses_application_database(isolated_database):
    with psycopg2.connect(os.environ["PAMS_SCHEMA_TEST_POSTGRES_DSN"]) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET pams.sim_cleanup_rehearsal = 'on'")
            with pytest.raises(psycopg2.errors.RaiseException):
                cur.execute(SCRIPT)
            cur.execute("ROLLBACK")
