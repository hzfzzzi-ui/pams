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
            cur.execute("SELECT to_regclass('public.pams_source_row')")
            assert cur.fetchone()[0] is None
            cur.execute("""SELECT count(*) FROM information_schema.columns WHERE table_schema='public'
                AND table_name IN ('pams_cashflow_fact','pams_investment_trade_fact','pams_holdings_snapshot')
                AND column_name='source_item_id'""")
            assert cur.fetchone()[0] == 3


def test_full_bootstrap_native_bank_http_reaches_account_ledger_and_sync(empty_source_db, monkeypatch):
    from datetime import date, timedelta
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from domain.imports import account_import_statement as statement, account_import_store as store
    from domain.investments import portfolio_state
    from domain.brokers.ibkr import ibkr_daily_close_read, ibkr_performance
    from endpoints import account_import, accounts, ledger, sync_center

    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.pams_source_row')")
            assert cur.fetchone()[0] is None
            cur.execute("INSERT INTO pams_accounts (account_code,display_name,base_currency) VALUES ('bank_test','Synthetic','CNY')")
    monkeypatch.setenv('PAMS_SOURCE_TEST_TOKEN_KEY', 'a1' * 32)
    monkeypatch.setenv('PAMS_IMPORT_PUBLISH_ENABLED', 'true')
    monkeypatch.delenv('PAMS_SOURCE_LEGACY_BRIDGE', raising=False)
    for module in (store, accounts, ledger, sync_center):
        monkeypatch.setattr(module, 'get_conn', lambda: psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(statement, 'config', lambda: {'profiles': {'test': {'account_code': 'bank_test',
        'profile_code': 'test', 'statement_template': 'manual_statement_csv', 'currency': 'CNY'}}, 'rules': [], 'amount_rules': []})
    monkeypatch.setattr(accounts, '_load_accounts_config', lambda: {})
    monkeypatch.setattr(accounts, '_load_bank_config', lambda: {})
    monkeypatch.setattr(portfolio_state, '_load_accounts_config', lambda: {})
    monkeypatch.setattr(ibkr_daily_close_read, 'configured', lambda: False)
    monkeypatch.setattr(ibkr_performance, 'published_report_status', lambda conn: {'status': 'missing', 'label': '缺数据',
        'statement_as_of': None, 'active_row_count': 0, 'expected_statement_as_of': None, 'last_attempt_status': None})
    monkeypatch.setattr(sync_center, 'read_ibkr_reconciliation', lambda: {'status': 'missing', 'label': '缺数据', 'details': ''})
    monkeypatch.setattr(sync_center, 'scheduler_health', lambda path: {'status': 'missing', 'label': '缺数据', 'jobs': {}})
    app = FastAPI()
    for router in (account_import.router, accounts.router, ledger.router, sync_center.router):
        app.include_router(router, prefix='/api')
    day = (date.today() - timedelta(days=2)).isoformat()
    with TestClient(app) as web:
        web.headers['Origin'] = 'http://testserver'
        response = web.post('/api/account-import/batches', json={'account_code':'bank_test','mode':'upload','kind':'cash'})
        assert response.status_code == 200, response.text
        path = '/api/account-import/batches/' + response.json()['batch_id']
        content = f'transaction_date,amount,currency,balance,summary\n{day},100.05,CNY,200.05,pay\n'.encode()
        response = web.post(path + '/files?filename=bank.csv', content=content)
        assert response.status_code == 200, response.text
        batch = response.json()
        response = web.put(path, json={'revision':batch['revision'], 'rows':batch['rows']})
        assert response.status_code == 200, response.text
        batch = response.json()
        response = web.post(path+'/preview', json={'revision':batch['revision']})
        assert response.status_code == 200, response.text
        check = response.json()
        assert check['can_publish'], check
        response = web.post(path+'/publish', json={'revision':batch['revision'], 'preview_token':check['preview_token']})
        assert response.status_code == 200, response.text
        # Once facts exist, read-only consumers do not require the isolated
        # write switch merely to read a new database without the old table.
        monkeypatch.delenv('PAMS_UNIFIED_SOURCE_ISOLATED')
        for url in ('/api/accounts/overview', '/api/ledger/workbench', '/api/sync/center'):
            response = web.get(url)
            assert response.status_code == 200, (url, response.text)
            if url.endswith('/accounts/overview'):
                account = next(item for item in response.json()['accounts'] if item['account_code'] == 'bank_test')
                assert sum(item['row_count'] for item in account['statement_profiles']) == 1
            if url.endswith('/ledger/workbench'):
                assert any(item['account_code'] == 'bank_test' and float(item['amount']) == 100.05
                           for item in response.json()['recent_rows'])
            if url.endswith('/sync/center'):
                row = next(item for item in response.json()['rows'] if item['id'] == 'account-reconcile')
                assert row['rows'] == 1 and row['latest_date'] == day
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.pams_source_row'),count(*) FROM pams_cashflow_fact")
            assert cur.fetchone() == (None, 1)


def test_account_constraint_is_preserved_when_legacy_source_table_really_exists(empty_source_db):
    from pathlib import Path

    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute('CREATE TABLE public.pams_source_row (account_code text)')
        conn.commit()
        with conn.cursor() as cur:
            script = Path(__file__).resolve().parents[2] / 'database/postgres/schema/028a_account_reference_constraints.sql'
            cur.execute(script.read_text(encoding='utf-8'))
            cur.execute("""SELECT convalidated FROM pg_constraint
                WHERE conrelid='public.pams_source_row'::regclass AND conname='fk_pams_source_row_account_code'""")
            assert cur.fetchone()[0] is True
            with pytest.raises(psycopg2.errors.ForeignKeyViolation):
                cur.execute("INSERT INTO public.pams_source_row VALUES ('nonexistent')")
        conn.rollback()


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
