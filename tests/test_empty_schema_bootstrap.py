"""Exercise the complete schema only on an explicitly supplied disposable PG16 server."""
import os
import sys
from pathlib import Path
from uuid import uuid4

import psycopg2
import pandas as pd
from psycopg2 import sql
from psycopg2.extensions import parse_dsn
from psycopg2.extras import RealDictCursor
import pytest


ROOT = Path(__file__).resolve().parents[1]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))


def bootstrap_paths():
    manifest = ROOT / "database/postgres/bootstrap/manifest.txt"
    return [ROOT / line.strip() for line in manifest.read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]


@pytest.mark.parametrize("optional_etf_source", [False, True])
def test_empty_database_initializes_shared_market_and_asset_tables(optional_etf_source, monkeypatch):
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
        assert cur.fetchone()[0] == "pams-disposable-schema-test", "Dedicated test database marker required"
    name = "pams_schema_test_" + uuid4().hex
    with admin.cursor() as cur:
        cur.execute(sql.SQL("CREATE DATABASE {} ").format(sql.Identifier(name)))
    try:
        with psycopg2.connect(**{**params, "dbname": name}) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                for path in bootstrap_paths():
                    if optional_etf_source and path.name == "026_core_architecture_refactor.sql":
                        cur.execute("""CREATE TABLE public.pams_cn_etf_daily (
                            trade_date date, source text, ts_code text, symbol text,
                            open numeric, high numeric, low numeric, close numeric,
                            volume numeric, turnover numeric)""")
                        cur.execute("""INSERT INTO public.pams_cn_etf_daily
                            VALUES ('2026-01-01','test','TEST','TEST',1,2,1,2,3,4)""")
                        # These public tables are owned by the independent backtest app.
                        # It creates indexes on public relations, which requires real tables.
                        cur.execute("""CREATE TABLE public.pams_transaction_lifecycle
                            (id text PRIMARY KEY, payload text)""")
                        cur.execute("""CREATE TABLE public.pams_transaction_sync_log
                            (id text PRIMARY KEY)""")
                        cur.execute("INSERT INTO public.pams_transaction_lifecycle VALUES ('existing', 'before')")
                    cur.execute(path.read_text(encoding="utf-8"))
                for table in ("pams_ohlcv_daily", "pams_fx_rates_daily",
                              "pams_holdings_snapshot", "pams_asset_market_daily"):
                    cur.execute("SELECT to_regclass(%s)", ("public." + table,))
                    assert cur.fetchone()[0], table
                # 026 moves the physical table and leaves a compatibility view.
                cur.execute("INSERT INTO public.pams_ohlcv_daily (symbol, trade_date, close) VALUES ('TEST', '2026-01-01', 123.45)")
                for filename in ("010_market_ohlcv.sql", "013a_alter_ohlcv.sql"):
                    cur.execute((ROOT / "database/postgres/schema" / filename).read_text(encoding="utf-8"))
                cur.execute("SELECT close FROM public.pams_ohlcv_daily WHERE symbol = 'TEST'")
                assert str(cur.fetchone()[0]) == '123.4500'
                cur.execute("DROP VIEW public.pams_ohlcv_daily")
                # An older physical table can also lack the newer market columns.
                cur.execute("ALTER TABLE pams_external_market.pams_ohlcv_daily DROP COLUMN adj_factor, DROP COLUMN limit_up, DROP COLUMN limit_down, DROP COLUMN is_suspended")
                for filename in ("010_market_ohlcv.sql", "013a_alter_ohlcv.sql"):
                    cur.execute((ROOT / "database/postgres/schema" / filename).read_text(encoding="utf-8"))
                cur.execute("SELECT close, adj_factor FROM public.pams_ohlcv_daily WHERE symbol = 'TEST'")
                assert str(cur.fetchone()[0]) == '123.4500'
                cur.execute("""SELECT COUNT(*) FROM pg_constraint
                    WHERE conrelid = 'public.pams_holdings_snapshot'::regclass
                    AND confrelid = 'public.pams_asset_master'::regclass AND contype = 'f'""")
                assert cur.fetchone()[0] == 1
                cur.execute("""SELECT COUNT(*) FROM information_schema.columns
                    WHERE table_schema='pams_external_market' AND table_name='pams_ohlcv_daily'
                    AND column_name IN ('adj_factor','limit_up','limit_down','is_suspended','turnover_rate','turnover_rate_f','volume_ratio','float_share','free_share','circ_mv')""")
                assert cur.fetchone()[0] == 10
                for schema_name in ("public", "pams_external_market"):
                    cur.execute("""SELECT COUNT(*) FROM information_schema.columns
                        WHERE table_schema=%s AND table_name='pams_ohlcv_daily'
                        AND column_name IN ('turnover_rate','turnover_rate_f','volume_ratio','float_share','free_share','circ_mv')
                        AND data_type='double precision'""", (schema_name,))
                    assert cur.fetchone()[0] == 6
                cur.execute("""INSERT INTO public.pams_ohlcv_daily
                    (symbol,market,trade_date,close,turnover_rate,turnover_rate_f,volume_ratio,float_share,free_share,circ_mv)
                    VALUES ('SHARED','CN_A','2026-01-01',1,2,3,4,5,6,7)
                    ON CONFLICT(symbol,market,trade_date) DO UPDATE SET close=excluded.close, circ_mv=excluded.circ_mv""")
                cur.execute("""INSERT INTO public.pams_ohlcv_daily
                    (symbol,market,trade_date,close,turnover_rate,turnover_rate_f,volume_ratio,float_share,free_share,circ_mv)
                    VALUES ('SHARED','CN_A','2026-01-01',8,2,3,4,5,6,9)
                    ON CONFLICT(symbol,market,trade_date) DO UPDATE SET close=excluded.close, circ_mv=excluded.circ_mv""")
                cur.execute("SELECT close,circ_mv FROM pams_external_market.pams_ohlcv_daily WHERE symbol='SHARED'")
                assert cur.fetchone() == (8, 9)
                cur.execute("INSERT INTO public.pams_fx_rates_daily(as_of,base_currency,quote_currency,rate) VALUES ('2026-01-01','USD','CNY',7.2)")
                cur.execute("INSERT INTO public.pams_transactions(date,amount,currency,category,account,account_code) VALUES ('2026-01-01',10,'USD','测试','Santander','santander')")
                cur.execute("SELECT amount_cny FROM public.v_transactions_cny")
                assert cur.fetchone()[0] == 72
                cur.execute("INSERT INTO public.pams_transactions(date,amount,currency,category,account,account_code) VALUES ('2026-01-01',10,'MXN','测试','Santander','santander')")
                cur.execute("SELECT amount_cny FROM public.v_transactions_cny WHERE currency='MXN'")
                assert cur.fetchone()[0] is None
                cur.execute("""INSERT INTO public.pams_holdings_snapshot
                    (name,asset_liability,currency,account_code,major_category,minor_category,market_value,cost,as_of)
                    VALUES ('Test','资产','CNY','ibkr','测试','测试',100,80,'2026-01-01')""")
                cur.execute("SELECT holding_pnl FROM public.pams_holdings_snapshot")
                assert cur.fetchone()[0] == 20
                cur.execute("""INSERT INTO public.pams_fundamental_snapshot (symbol, as_of, industry_code)
                    VALUES ('sh600000', '2026-01-01', 'industry-evidence')""")
                conn.commit()
                from services import risk_state

                monkeypatch.setattr(risk_state, "get_conn", lambda: psycopg2.connect(**{**params, "dbname": name}))
                assert risk_state._load_db_industry_lookup(pd.DataFrame([{"symbol": "600000.SH"}]))["600000.SH"] == "industry-evidence"
                if optional_etf_source:
                    cur.execute("SELECT close_price FROM public.pams_asset_market_daily WHERE source = 'test'")
                    assert cur.fetchone()[0] == 2
                    for shared_table_name in ("pams_transaction_lifecycle", "pams_transaction_sync_log"):
                        cur.execute("""SELECT c.relkind FROM pg_class c
                            JOIN pg_namespace n ON n.oid=c.relnamespace
                            WHERE n.nspname='public' AND c.relname=%s""", (shared_table_name,))
                        assert cur.fetchone()[0] == 'r'
                    cur.execute("""INSERT INTO public.pams_transaction_lifecycle VALUES ('existing', 'after')
                        ON CONFLICT (id) DO UPDATE SET payload=EXCLUDED.payload""")
                    cur.execute("SELECT payload FROM public.pams_transaction_lifecycle WHERE id='existing'")
                    assert cur.fetchone()[0] == 'after'
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_shared_lifecycle_payload ON public.pams_transaction_lifecycle (payload)")
                # Both independently executable market scripts must refuse ambiguous ownership.
                cur.execute("DROP VIEW public.pams_ohlcv_daily")
                cur.execute("CREATE TABLE public.pams_ohlcv_daily (symbol text, trade_date date, close numeric)")
                cur.execute("INSERT INTO public.pams_ohlcv_daily VALUES ('OTHER', '2026-01-01', 99)")
                conn.commit()
                for filename in ("010_market_ohlcv.sql", "013a_alter_ohlcv.sql"):
                    with pytest.raises(psycopg2.errors.RaiseException, match="Two physical"):
                        cur.execute((ROOT / "database/postgres/schema" / filename).read_text(encoding="utf-8"))
                    conn.rollback()
                cur.execute("SELECT close FROM public.pams_ohlcv_daily WHERE symbol = 'OTHER'")
                assert cur.fetchone()[0] == 99
                cur.execute("SELECT close FROM pams_external_market.pams_ohlcv_daily WHERE symbol = 'TEST'")
                assert str(cur.fetchone()[0]) == '123.4500'
    finally:
        with admin.cursor() as cur:
            cur.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()


@pytest.fixture
def bootstrap_database():
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
    name = "pams_schema_test_" + uuid4().hex
    with admin.cursor() as cur:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        cur.execute(sql.SQL("COMMENT ON DATABASE {} IS 'pams-empty-bootstrap'").format(sql.Identifier(name)))
    try:
        yield {**params, "dbname": name}
    finally:
        with admin.cursor() as cur:
            cur.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()


def test_guarded_bootstrap_commits_empty_database_and_refuses_repeat(bootstrap_database):
    from scripts.bootstrap_empty_database import BootstrapError, initialize_empty_database

    with psycopg2.connect(**bootstrap_database) as conn:
        initialize_empty_database(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.pams_holdings_snapshot'), to_regclass('public.pams_ohlcv_daily')")
            assert all(cur.fetchone())
            cur.execute("SELECT (SELECT count(*) FROM pams_holdings_snapshot), (SELECT count(*) FROM pams_cashflow_fact), (SELECT count(*) FROM pams_investment_trade_fact)")
            assert cur.fetchone() == (0, 0, 0)
            for table in ("pams_policy", "pams_strategy_candidates", "pams_watchlist", "pams_live_signal_packages", "pams_sim_accounts", "pams_data_version"):
                cur.execute("SELECT to_regclass(%s)", ("public." + table,))
                assert cur.fetchone()[0] is None, table
            for table in ("pams_sentiment_state", "pit_trade_cal", "pit_st_history", "pams_fundamental_snapshot"):
                cur.execute("SELECT to_regclass(%s)", ("public." + table,))
                assert cur.fetchone()[0] is not None, table
        conn.commit()
        import main
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            assert main._get_base_currency(cur) == "CNY"
            payload = main._build_summary2_latest_payload_for_request(cur)
            assert isinstance(payload, dict)
            cur.execute("SELECT 1 AS value")
            assert cur.fetchone()["value"] == 1
        conn.commit()
        with pytest.raises(BootstrapError, match="not empty"):
            initialize_empty_database(conn)


def test_guarded_bootstrap_rolls_back_all_files_on_failure(bootstrap_database, tmp_path, monkeypatch):
    import scripts.bootstrap_empty_database as bootstrap

    paths = bootstrap.bootstrap_paths()
    bad = tmp_path / "bad.sql"
    bad.write_text("THIS IS INVALID SQL;", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "bootstrap_paths", lambda: [*paths, bad])
    with psycopg2.connect(**bootstrap_database) as conn:
        with pytest.raises(psycopg2.errors.SyntaxError):
            bootstrap.initialize_empty_database(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.pams_accounts')")
            assert cur.fetchone()[0] is None


def test_guarded_bootstrap_refuses_unmarked_database():
    from scripts.bootstrap_empty_database import BootstrapError, initialize_empty_database

    dsn = os.environ.get("PAMS_SCHEMA_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Explicit disposable schema-test PostgreSQL DSN required")
    assert parse_dsn(dsn).get("host") in {"127.0.0.1", "localhost", "::1"}
    with psycopg2.connect(dsn) as conn:
        with pytest.raises(BootstrapError, match="lacks the empty-bootstrap marker"):
            initialize_empty_database(conn)


def test_guarded_bootstrap_refuses_other_clients(bootstrap_database):
    from scripts.bootstrap_empty_database import BootstrapError, initialize_empty_database

    with psycopg2.connect(**bootstrap_database) as other:
        assert not other.closed
        with psycopg2.connect(**bootstrap_database) as conn:
            with pytest.raises(BootstrapError, match="other client"):
                initialize_empty_database(conn)
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('public.pams_accounts')")
                assert cur.fetchone()[0] is None


def test_guarded_bootstrap_refuses_unknown_transaction_statement(tmp_path):
    from scripts.bootstrap_empty_database import BootstrapError, _transaction_safe_sql

    sql_file = tmp_path / "unexpected.sql"
    sql_file.write_text("COMMIT; -- unsupported wrapper\n", encoding="utf-8")
    with pytest.raises(BootstrapError, match="unexpected transaction statement"):
        _transaction_safe_sql(sql_file)


def test_empty_bootstrap_supports_all_pams_ohlcv_writers(bootstrap_database, monkeypatch):
    from datetime import date
    from decimal import Decimal
    from endpoints import prices
    from services import price_history
    from scripts.bootstrap_empty_database import initialize_empty_database

    conn = psycopg2.connect(**bootstrap_database)
    try:
        initialize_empty_database(conn)
        monkeypatch.setattr(price_history, "fetch_yfinance_history", lambda *_args, **_kwargs:
                            ({"NVDA": {date(2026, 10, 26): Decimal("100.5")}}, []))
        with conn.cursor() as cur:
            for _ in range(2):
                assert price_history.upsert_yfinance_history(cur, symbols=["NVDA"],
                    start=date(2026, 10, 23), end=date(2026, 10, 26))["rows"] == 1
        conn.commit()
        monkeypatch.setattr(prices, "_get_conn", lambda: psycopg2.connect(**bootstrap_database))
        assert prices._upsert_cn_a_closes([{"symbol": "sh600000"}], {"sh600000": 10.5}, date(2026, 10, 26)) == 1
        monkeypatch.setattr(prices, "_YFINANCE_CLOSE_CACHE", {"NVDA": [(date(2026, 10, 26), 101.5)]})
        assert prices._upsert_yfinance_closes([{"symbol": "NVDA"}]) == 1
        rows = [{"id": 1, "symbol": "SIVE", "name": "Test", "currency": "SEK",
                 "major_category": "美股", "asset_liability": "资产", "account_code": "ibkr",
                 "quantity": Decimal("3.125"), "cost": Decimal("100.13")}]
        history = {"SIVE": [(date(2026, 10, 23), 100.0), (date(2026, 10, 26), 102.0)]}
        assert prices._upsert_scheduled_overseas_quotes(rows, "se", date(2026, 10, 26), history) == (2, 1, {"SIVE"})
        with conn.cursor() as cur:
            cur.execute("SELECT symbol,close FROM public.pams_ohlcv_daily ORDER BY symbol,trade_date")
            rows = cur.fetchall()
            assert ("NVDA", Decimal("101.5000")) in rows
            assert ("sh600000", Decimal("10.5000")) in rows
            assert ("SIVE", Decimal("102.0000")) in rows
            cur.execute("SELECT count(*) FROM pams_external_market.pams_ohlcv_daily")
            assert cur.fetchone()[0] == len(rows)
    finally:
        conn.close()


def test_stale_market_view_fails_without_partial_column_changes(bootstrap_database):
    conn = psycopg2.connect(**bootstrap_database)
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE SCHEMA pams_external_market")
            cur.execute("""CREATE TABLE pams_external_market.pams_ohlcv_daily (
                symbol text NOT NULL, market text NOT NULL, trade_date date NOT NULL,
                close numeric, PRIMARY KEY(symbol,market,trade_date))""")
            cur.execute("CREATE VIEW public.pams_ohlcv_daily AS SELECT * FROM pams_external_market.pams_ohlcv_daily")
            cur.execute("INSERT INTO public.pams_ohlcv_daily VALUES ('TEST','YF','2026-01-01',12)")
        conn.commit()
        with conn.cursor() as cur:
            with pytest.raises(psycopg2.errors.RaiseException, match="does not expose shared columns"):
                cur.execute((ROOT / "database/postgres/schema/013a_alter_ohlcv.sql").read_text(encoding="utf-8"))
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("SELECT close FROM public.pams_ohlcv_daily WHERE symbol='TEST'")
            assert cur.fetchone()[0] == 12
            cur.execute("""SELECT count(*) FROM information_schema.columns
                WHERE table_schema='pams_external_market' AND table_name='pams_ohlcv_daily'
                AND column_name='turnover_rate'""")
            assert cur.fetchone()[0] == 0
    finally:
        conn.close()


def test_bootstrap_manifest_uses_schema_files_without_historical_backfills():
    from scripts.bootstrap_empty_database import bootstrap_paths

    paths = bootstrap_paths()
    assert all(path.parent == ROOT / "database/postgres/schema" for path in paths)
    names = {path.name for path in paths}
    assert {"028a_account_reference_constraints.sql", "029a_holding_asset_reference_constraints.sql",
            "033a_benchmark_asset_seed.sql"} <= names
    assert not {"028_account_dimension_backfill.sql", "029_holding_asset_identity_backfill.sql",
                "033_benchmark_assets_and_gold_proxy.sql"} & names
