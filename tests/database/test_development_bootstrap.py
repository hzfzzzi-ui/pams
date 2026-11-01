"""Development initialization is limited to a fresh marked local PG16 database."""
from pathlib import Path
from uuid import uuid4

import os
import sys

import psycopg2
import pytest
from psycopg2 import sql
from psycopg2.extensions import parse_dsn


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend/api'))
from scripts import bootstrap_development_database as bootstrap


@pytest.fixture
def fresh_development_database(monkeypatch):
    admin_dsn = os.environ.get('PAMS_SCHEMA_TEST_POSTGRES_DSN')
    if not admin_dsn:
        pytest.skip('Explicit disposable schema-test PostgreSQL DSN required')
    params = parse_dsn(admin_dsn)
    assert params.get('host') in {'127.0.0.1', 'localhost', '::1'}
    admin = psycopg2.connect(admin_dsn, connect_timeout=5)
    admin.autocommit = True
    assert admin.server_version // 10000 == 16
    with admin.cursor() as cur:
        cur.execute("SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname=current_database()")
        assert cur.fetchone()[0] == 'pams-disposable-schema-test'
    name = 'pams_dev_' + uuid4().hex
    with admin.cursor() as cur:
        cur.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        cur.execute(sql.SQL('COMMENT ON DATABASE {} IS {}').format(
            sql.Identifier(name), sql.Literal(bootstrap.MARKER)))
    monkeypatch.setenv('PAMS_SOURCE_MODE', 'development')
    monkeypatch.delenv('PAMS_UNIFIED_SOURCE_ISOLATED', raising=False)
    try:
        yield {**params, 'dbname': name}, admin
    finally:
        with admin.cursor() as cur:
            cur.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
        admin.close()


def test_development_bootstrap_initializes_once_and_preserves_data(fresh_development_database):
    params, _ = fresh_development_database
    with psycopg2.connect(**params) as conn:
        bootstrap.initialize_development_database(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.source_batches'), to_regclass('public.pams_holdings_snapshot')")
            assert all(cur.fetchone())
            for table in (
                'valuation_evidence', 'account_snapshot_version', 'account_snapshot_member', 'evidence_confirmation',
                'broker_processing_state', 'asset_classifications', 'market_observations', 'valuation_results',
            ):
                cur.execute('SELECT to_regclass(%s)', (f'public.{table}',))
                assert cur.fetchone()[0] is not None, table
            for table in (
                'valuation_result_version', 'valuation_input_evidence',
                'confirmation_revocation', 'ibkr_report_publication', 'ibkr_cash_event_progress',
                'pams_valuation_result_version', 'pams_valuation_input_evidence',
                'pams_account_snapshot_version', 'pams_account_snapshot_member',
                'pams_evidence_confirmation', 'pams_confirmation_revocation',
                'pams_ibkr_report_publication', 'pams_ibkr_cash_event_progress',
                'pams_asset_industry_map', 'pams_asset_theme_map', 'pams_asset_market_daily',
                'pams_market_quote_snapshot', 'pams_fund_nav_estimates',
                'pams_holding_return_snapshot', 'pams_portfolio_valuation_state',
                'pams_trade_identity_transition_event',
            ):
                cur.execute('SELECT to_regclass(%s)', (f'public.{table}',))
                assert cur.fetchone()[0] is None, table
            cur.execute("SELECT relkind FROM pg_class WHERE oid='public.pams_ohlcv_daily'::regclass")
            assert cur.fetchone()[0] == 'r'
            cur.execute("INSERT INTO pams_accounts (account_code, display_name) VALUES ('synthetic','Synthetic')")
        conn.commit()
        with pytest.raises(bootstrap.BootstrapError, match='not empty'):
            bootstrap.initialize_development_database(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT display_name FROM pams_accounts WHERE account_code='synthetic'")
            assert cur.fetchone()[0] == 'Synthetic'


def test_development_bootstrap_rolls_back_every_schema_on_failure(fresh_development_database, monkeypatch, tmp_path):
    params, _ = fresh_development_database
    bad_sql = tmp_path / 'invalid.sql'
    bad_sql.write_text('THIS IS INVALID SQL;', encoding='utf-8')
    original_paths = bootstrap.bootstrap_paths
    monkeypatch.setattr(bootstrap, 'bootstrap_paths', lambda: [*original_paths(), bad_sql])
    with psycopg2.connect(**params) as conn:
        with pytest.raises(psycopg2.errors.SyntaxError):
            bootstrap.initialize_development_database(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.source_batches'), to_regclass('public.pams_accounts')")
            assert cur.fetchone() == (None, None)


@pytest.mark.parametrize('mode,test_mode', [('', ''), ('production', ''), ('development', 'true')])
def test_development_bootstrap_refuses_wrong_or_mixed_mode(fresh_development_database, monkeypatch, mode, test_mode):
    params, _ = fresh_development_database
    monkeypatch.setenv('PAMS_SOURCE_MODE', mode)
    monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED', test_mode)
    with psycopg2.connect(**params) as conn:
        with pytest.raises((bootstrap.BootstrapError, ValueError)):
            bootstrap.initialize_development_database(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.pams_accounts')")
            assert cur.fetchone()[0] is None


def test_development_bootstrap_refuses_wrong_database_marker(fresh_development_database):
    params, admin = fresh_development_database
    with admin.cursor() as cur:
        cur.execute(sql.SQL('COMMENT ON DATABASE {} IS {}').format(
            sql.Identifier(params['dbname']), sql.Literal('not-development')))
    with psycopg2.connect(**params) as conn:
        with pytest.raises(ValueError):
            bootstrap.initialize_development_database(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.pams_accounts')")
            assert cur.fetchone()[0] is None


@pytest.mark.parametrize('dsn', [
    'host=database.example port=5432 dbname=pams_dev_' + 'a' * 32 + ' user=test',
    'host=127.0.0.1 port=5432 dbname=pams user=test',
    'host=127.0.0.1 dbname=pams_dev_' + 'a' * 32 + ' user=test',
    'host=127.0.0.1 port=5432 dbname=pams_dev_' + 'a' * 32 + ' user=test options=-csearch_path=public',
])
def test_cli_rejects_ambiguous_dsn_before_connecting(monkeypatch, dsn):
    monkeypatch.setenv('PAMS_SOURCE_MODE', 'development')
    monkeypatch.setenv('PAMS_DEV_BOOTSTRAP_DSN', dsn)
    monkeypatch.setattr(sys, 'argv', ['bootstrap_development_database.py', '--apply'])
    monkeypatch.setattr(bootstrap.psycopg2, 'connect', lambda *args, **kwargs: pytest.fail('must not connect'))
    assert bootstrap.main() == 1


def test_cli_rejects_localhost_resolving_off_machine_before_connecting(monkeypatch):
    from domain.imports import isolation

    monkeypatch.setenv('PAMS_SOURCE_MODE', 'development')
    monkeypatch.setenv('PAMS_DEV_BOOTSTRAP_DSN',
                       'host=localhost port=5432 dbname=pams_dev_' + 'a' * 32 + ' user=test')
    monkeypatch.setattr(sys, 'argv', ['bootstrap_development_database.py', '--apply'])
    monkeypatch.setattr(isolation.socket, 'getaddrinfo', lambda *a, **k: [(None, None, None, None, ('203.0.113.1', 0))])
    monkeypatch.setattr(bootstrap.psycopg2, 'connect', lambda *a, **k: pytest.fail('must not connect'))
    assert bootstrap.main() == 1
