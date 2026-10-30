"""Verify cleanup is restricted to marked, disposable PostgreSQL databases."""
import os
import sys
from pathlib import Path
from uuid import uuid4

import psycopg2
import pandas as pd
from psycopg2 import sql
from psycopg2.extensions import parse_dsn
import pytest


ROOT = Path(__file__).resolve().parents[2]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))
SCRIPT = (ROOT / "database/postgres/cleanup/strategy_rehearsal.sql").read_text(encoding="utf-8")
REMOVED = (
    "pams_order_drafts", "pams_strategy_candidates", "pams_strategy_state",
    "pams_strategy_blacklist", "pams_watchlist_log", "pams_watchlist",
    "pams_event_monitor", "pams_position_cooldown",
    "pams_risk_snapshot",
)
SHARED_WITH_BACKTEST = (
    "pams_sentiment_state", "pams_sim_accounts", "pams_sim_cash_ledger",
    "pams_sim_holdings_snapshots", "pams_sim_reconcile_summaries", "pams_sim_sync_logs",
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
    name = "pams_cleanup_test_" + uuid4().hex
    with admin.cursor() as cur:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        cur.execute(sql.SQL("COMMENT ON DATABASE {} IS 'pams-isolated-strategy-cleanup'").format(sql.Identifier(name)))
    try:
        yield {**params, "dbname": name}
    finally:
        with admin.cursor() as cur:
            cur.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()


def prepare(cur):
    cur.execute("CREATE SCHEMA pams_module; CREATE SCHEMA pams_external_market")
    for name in REMOVED:
        cur.execute(sql.SQL("CREATE TABLE pams_module.{} (id int PRIMARY KEY)").format(sql.Identifier(name)))
        cur.execute(sql.SQL("INSERT INTO pams_module.{} VALUES (1)").format(sql.Identifier(name)))
        cur.execute(sql.SQL("CREATE VIEW public.{} AS SELECT * FROM pams_module.{}").format(
            sql.Identifier(name), sql.Identifier(name)))
    for name in SHARED_WITH_BACKTEST:
        cur.execute(sql.SQL("CREATE TABLE pams_module.{} (id int PRIMARY KEY, evidence text)").format(sql.Identifier(name)))
        cur.execute(sql.SQL("INSERT INTO pams_module.{} VALUES (1, 'shared-evidence')").format(sql.Identifier(name)))
        cur.execute(sql.SQL("CREATE VIEW public.{} AS SELECT * FROM pams_module.{}").format(
            sql.Identifier(name), sql.Identifier(name)))
    cur.execute("""ALTER TABLE pams_module.pams_order_drafts
        ADD FOREIGN KEY (id) REFERENCES pams_module.pams_strategy_candidates(id);
        CREATE TABLE pams_module.pams_policy (base_currency text);
        INSERT INTO pams_module.pams_policy VALUES ('CNY');
        CREATE VIEW public.pams_policy AS SELECT * FROM pams_module.pams_policy;
        CREATE TABLE pams_module.pams_fundamental_snapshot (symbol text, industry_code text, as_of date);
        INSERT INTO pams_module.pams_fundamental_snapshot VALUES ('sh600000', 'industry-evidence', '2026-01-01');
        CREATE VIEW public.pams_fundamental_snapshot AS SELECT * FROM pams_module.pams_fundamental_snapshot;
        CREATE TABLE public.pit_industry_history (ts_code text, industry_l2 text, industry_l1 text, end_date date, standard text, start_date date);
        CREATE TABLE public.pit_security_master (ts_code text, industry text);
        CREATE TABLE public.pams_holdings_snapshot (value numeric);
        INSERT INTO public.pams_holdings_snapshot VALUES (123.45);
        CREATE TABLE pams_external_market.pams_ohlcv_daily (close numeric);
        INSERT INTO pams_external_market.pams_ohlcv_daily VALUES (9.87);
        CREATE VIEW public.pams_ohlcv_daily AS SELECT * FROM pams_external_market.pams_ohlcv_daily;
        CREATE TABLE public.pit_trade_cal (day date);
        INSERT INTO public.pit_trade_cal VALUES ('2026-01-01');
    """)


def test_strategy_rehearsal_preserves_shared_data_and_is_idempotent(isolated_database, monkeypatch):
    from domain.risk import risk_state

    with psycopg2.connect(**isolated_database) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            prepare(cur)
            conn.commit()
            monkeypatch.setattr(risk_state, "get_conn", lambda: psycopg2.connect(**isolated_database))
            positions = pd.DataFrame([{"symbol": "600000.SH"}])
            before = risk_state._load_db_industry_lookup(positions)
            assert before["600000.SH"] == "industry-evidence"
            cur.execute("SET pams.cleanup_rehearsal = 'on'")
            cur.execute(SCRIPT)
            cur.execute(SCRIPT)
            assert risk_state._load_db_industry_lookup(positions) == before
            for name in REMOVED:
                cur.execute("SELECT to_regclass(%s), to_regclass(%s)", ("public." + name, "pams_module." + name))
                assert cur.fetchone() == (None, None)
            for name in SHARED_WITH_BACKTEST:
                cur.execute(sql.SQL("SELECT evidence FROM public.{} WHERE id = 1").format(sql.Identifier(name)))
                assert cur.fetchone()[0] == "shared-evidence"
            for query, value in (
                ("SELECT base_currency FROM public.pams_policy", "CNY"),
                ("SELECT industry_code FROM public.pams_fundamental_snapshot", "industry-evidence"),
                ("SELECT value FROM public.pams_holdings_snapshot", 123.45),
                ("SELECT close FROM public.pams_ohlcv_daily", 9.87),
                ("SELECT count(*) FROM public.pit_trade_cal", 1),
            ):
                cur.execute(query)
                assert str(cur.fetchone()[0]) == str(value)


def test_unknown_view_dependency_aborts_entire_transaction(isolated_database):
    with psycopg2.connect(**isolated_database) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            prepare(cur)
            cur.execute("CREATE VIEW public.protected_consumer AS SELECT * FROM public.pams_strategy_candidates")
            conn.commit()
            cur.execute("SET pams.cleanup_rehearsal = 'on'")
            with pytest.raises(psycopg2.errors.DependentObjectsStillExist):
                cur.execute(SCRIPT)
            cur.execute("ROLLBACK")
            for name in REMOVED:
                cur.execute("SELECT to_regclass(%s), to_regclass(%s)", ("public." + name, "pams_module." + name))
                assert all(cur.fetchone())


def test_rehearsal_guard_refuses_unmarked_database(isolated_database):
    with psycopg2.connect(**isolated_database) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            prepare(cur)
            conn.commit()
            with pytest.raises(psycopg2.errors.RaiseException):
                cur.execute(SCRIPT)
            cur.execute("ROLLBACK")
            cur.execute("SELECT count(*) FROM pams_module.pams_strategy_candidates")
            assert cur.fetchone()[0] == 1


def test_rehearsal_guard_refuses_non_disposable_database(isolated_database):
    with psycopg2.connect(os.environ["PAMS_SCHEMA_TEST_POSTGRES_DSN"]) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET pams.cleanup_rehearsal = 'on'")
            with pytest.raises(psycopg2.errors.RaiseException):
                cur.execute(SCRIPT)
            cur.execute("ROLLBACK")
