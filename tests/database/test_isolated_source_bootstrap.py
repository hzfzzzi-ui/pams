"""Atomic source schema bootstrap for a specifically marked disposable database."""
import os
from uuid import uuid4

import psycopg2
import pytest
from psycopg2 import sql
from psycopg2.extensions import parse_dsn

from scripts import bootstrap_isolated_sources as bootstrap
from scripts.bootstrap_empty_database import BootstrapError


@pytest.fixture
def empty_source_db(monkeypatch):
    dsn = os.environ.get('PAMS_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('explicit disposable PostgreSQL DSN required')
    params = parse_dsn(dsn)
    assert params.get('host') in {'127.0.0.1', 'localhost', '::1'}
    admin = psycopg2.connect(dsn)
    admin.autocommit = True
    assert admin.server_version // 10000 == 16
    name = 'pams_import_test_' + uuid4().hex
    with admin.cursor() as cur:
        cur.execute("SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname=current_database()")
        assert cur.fetchone()[0] == 'pams-disposable-schema-test'
        cur.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        cur.execute(sql.SQL("COMMENT ON DATABASE {} IS 'pams-isolated-account-import'").format(sql.Identifier(name)))
    monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED', 'true')
    try:
        yield {**params, 'dbname': name}
    finally:
        with admin.cursor() as cur:
            cur.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
        admin.close()


def test_empty_isolated_database_gets_sources_and_facts_atomically(empty_source_db):
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            for table in ('source_batches', 'source_files', 'source_items', 'pams_cashflow_fact',
                          'pams_investment_trade_fact', 'pams_holdings_snapshot'):
                cur.execute('SELECT to_regclass(%s)', ('public.' + table,))
                assert cur.fetchone()[0] is not None
            cur.execute("""SELECT count(*) FROM information_schema.columns WHERE table_schema='public'
                AND table_name IN ('pams_cashflow_fact','pams_investment_trade_fact','pams_holdings_snapshot')
                AND column_name='source_item_id'""")
            assert cur.fetchone()[0] == 3


def test_initialization_failure_rolls_back_every_table(empty_source_db, monkeypatch, tmp_path):
    bad = tmp_path / 'invalid.sql'
    bad.write_text('SELECT nonexistent_function_for_test();', encoding='utf-8')
    paths = bootstrap.isolated_source_paths()
    monkeypatch.setattr(bootstrap, 'isolated_source_paths', lambda: [*paths, bad])
    with psycopg2.connect(**empty_source_db) as conn:
        with pytest.raises(psycopg2.Error):
            bootstrap.initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.pams_accounts'),to_regclass('public.source_batches')")
            assert cur.fetchone() == (None, None)


def test_disabled_or_nonempty_target_is_refused(empty_source_db, monkeypatch):
    monkeypatch.delenv('PAMS_UNIFIED_SOURCE_ISOLATED')
    with psycopg2.connect(**empty_source_db) as conn:
        with pytest.raises(BootstrapError, match='disabled'):
            bootstrap.initialize_isolated_sources(conn)
        monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED', 'true')
        with conn.cursor() as cur:
            cur.execute('CREATE TABLE public.not_empty(id integer)')
        conn.commit()
        with pytest.raises(BootstrapError, match='not empty'):
            bootstrap.initialize_isolated_sources(conn)
