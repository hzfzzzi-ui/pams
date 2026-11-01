from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend/api'))
from domain.imports import isolation, native_source_http
from domain.imports import account_import_store as store


def cursor(db='runtime', marker='pams-empty-bootstrap'):
    cur = Mock()
    cur.connection.server_version = 160000
    cur.connection.get_dsn_parameters.return_value = {'dbname': db}
    cur.fetchone.return_value = {'marker': marker}
    return cur


def test_runtime_access_is_scoped_and_checks_database(monkeypatch):
    monkeypatch.delenv('PAMS_SOURCE_MODE', raising=False)
    monkeypatch.delenv('PAMS_UNIFIED_SOURCE_ISOLATED', raising=False)
    monkeypatch.setenv('PAMS_IMPORT_RUNTIME_ENABLED', 'true')
    monkeypatch.setenv('PAMS_IMPORT_RUNTIME_DATABASE', 'runtime')
    assert not isolation.source_enabled(cursor())
    with isolation.account_import_scope():
        assert isolation.source_enabled(cursor())
        with pytest.raises(isolation.IsolationError):
            isolation.source_enabled(cursor(db='other'))
        with pytest.raises(isolation.IsolationError):
            isolation.source_enabled(cursor(marker='other'))
    assert not isolation.source_enabled(cursor())


def test_runtime_scope_resets_after_exception(monkeypatch):
    monkeypatch.setenv('PAMS_IMPORT_RUNTIME_ENABLED', 'true')
    with pytest.raises(RuntimeError):
        with isolation.account_import_scope():
            assert isolation.runtime_import_active()
            raise RuntimeError()
    assert not isolation.runtime_import_active()


def test_runtime_token_must_be_separate(monkeypatch):
    monkeypatch.delenv('PAMS_SOURCE_MODE', raising=False)
    monkeypatch.delenv('PAMS_UNIFIED_SOURCE_ISOLATED', raising=False)
    monkeypatch.setenv('PAMS_SOURCE_TEST_TOKEN_KEY', 'ab' * 32)
    monkeypatch.setenv('PAMS_IMPORT_RUNTIME_TOKEN_KEY', 'ab' * 32)
    with pytest.raises(ValueError):
        native_source_http._token_key()
    monkeypatch.setenv('PAMS_IMPORT_RUNTIME_TOKEN_KEY', 'cd' * 32)
    assert native_source_http._token_key() == bytes.fromhex('cd' * 32)


def test_runtime_http_staging_preview_and_publication(monkeypatch):
    import psycopg2
    from psycopg2 import sql
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from endpoints.account_import import router
    from tests.support.account_import_support import scratch_database
    from tests.imports.test_account_import_publish import draft, trade, preview, publish

    with scratch_database() as params:
        with psycopg2.connect(**params) as conn:
            with conn.cursor() as cur:
                cur.execute(sql.SQL('COMMENT ON DATABASE {} IS %s').format(sql.Identifier(params['dbname'])),
                            ('pams-empty-bootstrap',))
        monkeypatch.delenv('PAMS_SOURCE_MODE', raising=False)
        monkeypatch.delenv('PAMS_UNIFIED_SOURCE_ISOLATED', raising=False)
        monkeypatch.setenv('PAMS_IMPORT_RUNTIME_ENABLED', 'true')
        monkeypatch.setenv('PAMS_IMPORT_RUNTIME_DATABASE', params['dbname'])
        monkeypatch.setenv('PAMS_IMPORT_RUNTIME_TOKEN_KEY', 'cd' * 32)
        monkeypatch.setenv('PAMS_IMPORT_PUBLISH_ENABLED', 'true')
        monkeypatch.setattr(store, 'get_conn', lambda: psycopg2.connect(**params))
        app = FastAPI()
        app.include_router(router, prefix='/api')
        with TestClient(app, headers={'Origin': 'http://testserver'}) as client:
            assert client.get('/api/account-import/context').status_code == 200
            batch = draft(client, [trade()])
            check = preview(client, batch)
            assert check['can_publish']
            assert publish(client, batch, check).status_code == 200
