"""Development IBKR evidence is synthetic, local and database-bound."""
import os
from pathlib import Path
import sys
from uuid import uuid4

import psycopg2
import pytest
from psycopg2 import sql
from psycopg2.extensions import parse_dsn
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend/api'))
from domain.brokers.ibkr import ibkr_daily_close_read as daily
from endpoints.ibkr import router
from scripts.bootstrap_empty_database import _initialize_empty_database
from scripts.bootstrap_isolated_sources import isolated_source_paths
from tests.brokers.ibkr.test_ibkr_cash_events import publish_cash_days


def _dev_settings(monkeypatch, root, dbname):
    monkeypatch.setenv('PAMS_SOURCE_MODE', 'development')
    monkeypatch.delenv('PAMS_UNIFIED_SOURCE_ISOLATED', raising=False)
    monkeypatch.setenv('PGHOST', '127.0.0.1')
    monkeypatch.setenv('PGDATABASE', dbname)
    monkeypatch.setenv(daily.DEV_EVIDENCE_DIR_ENV, str(root))
    (root / daily.DEV_EVIDENCE_MARKER).write_text(dbname, encoding='utf-8')


def test_development_evidence_rejects_wrong_scope(tmp_path, monkeypatch):
    root = tmp_path / 'evidence'
    root.mkdir()
    manifest, _, _ = publish_cash_days(root, monkeypatch, {'2026-10-22': [{}]})
    dbname = 'pams_dev_' + 'a' * 32
    _dev_settings(monkeypatch, root, dbname)
    assert daily.load_evidence()['status'] == 'ok'
    monkeypatch.delenv(daily.DEV_EVIDENCE_DIR_ENV)
    assert daily.load_evidence()['status'] == 'unavailable'
    monkeypatch.setenv(daily.DEV_EVIDENCE_DIR_ENV, str(root))
    (root / daily.DEV_EVIDENCE_MARKER).write_text('pams_dev_' + 'b' * 32, encoding='utf-8')
    assert daily.load_evidence()['status'] == 'unavailable'
    (root / daily.DEV_EVIDENCE_MARKER).write_text(dbname, encoding='utf-8')
    outside = tmp_path / 'outside.json'
    outside.write_bytes(manifest.read_bytes())
    monkeypatch.setenv(daily.MANIFEST_ENV, str(outside))
    assert daily.load_evidence()['status'] == 'unavailable'


def test_development_evidence_rejects_symlinks(tmp_path, monkeypatch):
    root = tmp_path / 'evidence'
    root.mkdir()
    manifest, source, _ = publish_cash_days(root, monkeypatch, {'2026-10-22': [{}]})
    _dev_settings(monkeypatch, root, 'pams_dev_' + 'a' * 32)
    outside = tmp_path / 'outside.json'
    outside.write_bytes(manifest.read_bytes())
    monkeypatch.setenv(daily.MANIFEST_ENV, str(manifest))
    try:
        link = root / 'link.json'
        link.symlink_to(outside)
    except OSError:
        pytest.skip('Windows does not permit this process to create symlinks')
    monkeypatch.setenv(daily.MANIFEST_ENV, str(link))
    assert daily.load_evidence()['status'] == 'unavailable'
    monkeypatch.setenv(daily.MANIFEST_ENV, str(manifest))
    outside_xml = tmp_path / source.name
    outside_xml.write_bytes(source.read_bytes())
    source.unlink()
    source.symlink_to(outside_xml)
    assert daily.load_evidence()['status'] == 'unavailable'


@pytest.mark.parametrize('link_kind', ['root', 'marker', 'manifest', 'source'])
def test_development_evidence_rejects_linked_components(tmp_path, monkeypatch, link_kind):
    root = tmp_path / 'evidence'
    root.mkdir()
    manifest, source, _ = publish_cash_days(root, monkeypatch, {'2026-10-22': [{}]})
    _dev_settings(monkeypatch, root, 'pams_dev_' + 'a' * 32)
    original = daily._path_is_link
    target = {'root': root, 'marker': root / daily.DEV_EVIDENCE_MARKER,
              'manifest': manifest, 'source': source}[link_kind]
    monkeypatch.setattr(daily, '_path_is_link', lambda path: path == target or original(path))
    assert daily.load_evidence()['status'] == 'unavailable'


def test_development_cash_import_and_replay_from_bound_evidence(tmp_path, monkeypatch):
    dsn = os.environ.get('PAMS_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('explicit disposable PostgreSQL DSN required')
    params = parse_dsn(dsn)
    assert params.get('host') in {'127.0.0.1', 'localhost', '::1'}
    admin = psycopg2.connect(dsn, connect_timeout=5)
    admin.autocommit = True
    assert admin.server_version // 10000 == 16
    dbname = 'pams_dev_' + uuid4().hex
    with admin.cursor() as cur:
        cur.execute("SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname=current_database()")
        assert cur.fetchone()[0] == 'pams-disposable-schema-test'
        cur.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(dbname)))
        cur.execute(sql.SQL("COMMENT ON DATABASE {} IS 'pams-development-sandbox'").format(sql.Identifier(dbname)))
    try:
        target = {**params, 'dbname': dbname}
        with psycopg2.connect(**target) as conn:
            _initialize_empty_database(conn, isolated_source_paths(), 'pams-development-sandbox')
        root = tmp_path / 'evidence'
        root.mkdir()
        publish_cash_days(root, monkeypatch, {'2026-10-22': [{'type': 'Dividends', 'amount': '10'}]})
        _dev_settings(monkeypatch, root, dbname)
        for key in ('port', 'user', 'password'):
            if key in target:
                monkeypatch.setenv({'port': 'PGPORT', 'user': 'PGUSER', 'password': 'PGPASSWORD'}[key], str(target[key]))
        monkeypatch.setenv('PAMS_IBKR_CASH_EVENTS_AFTER', '2026-10-21')
        app = FastAPI()
        app.include_router(router, prefix='/api')
        with TestClient(app) as client:
            first = client.post('/api/ibkr/cash-events/sync', json={})
            assert first.status_code == 200, first.text
            assert first.json()['facts_written'] == 1
            again = client.post('/api/ibkr/cash-events/sync', json={})
            assert again.status_code == 200 and again.json()['facts_written'] == 0
        with psycopg2.connect(**target) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM pams_investment_trade_fact WHERE account_code='ibkr' AND side='dividend'")
            assert cur.fetchone()[0] == 1
    finally:
        with admin.cursor() as cur:
            cur.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(dbname)))
        admin.close()
