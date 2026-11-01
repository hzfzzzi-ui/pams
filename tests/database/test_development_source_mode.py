"""Development source mode must only use a disposable local PG16 sandbox."""
import sys
import subprocess
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import psycopg2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend/api'))
from domain.imports import isolation, native_source_http, account_import_store
from domain.investments.investment_return_ibkr import _fetch_yfinance_closes
from endpoints.account_import import router
from tests.imports.test_account_import_publish import draft, trade, publish, sql
from tests.support.account_import_support import scratch_database


def test_fail_closed_without_connecting_to_unsafe_target(monkeypatch):
    monkeypatch.setenv('PAMS_SOURCE_MODE', 'development')
    monkeypatch.delenv('PAMS_UNIFIED_SOURCE_ISOLATED', raising=False)
    monkeypatch.setattr(psycopg2, 'connect', lambda *a, **k: pytest.fail('connected to unsafe target'))
    with pytest.raises(isolation.IsolationError):
        account_import_store.get_conn()  # defaults are never a dev target
    for host, name in [('example.org', 'pams_dev_' + 'a'*32), ('127.0.0.1','pams')]:
        with pytest.raises(isolation.IsolationError):
            isolation.guarded_connect(host=host, dbname=name)
    monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED', 'true')
    with pytest.raises(isolation.IsolationError):
        isolation.guarded_connect(host='127.0.0.1', dbname='pams_dev_' + 'a'*32)


def test_development_key_is_distinct_and_required(monkeypatch):
    monkeypatch.setenv('PAMS_SOURCE_MODE', 'development')
    monkeypatch.delenv('PAMS_UNIFIED_SOURCE_ISOLATED', raising=False)
    monkeypatch.setenv('PAMS_SOURCE_TEST_TOKEN_KEY', 'a1'*32)
    for value in ('', 'xyz', 'a1'*32):
        monkeypatch.setenv('PAMS_SOURCE_DEV_TOKEN_KEY', value)
        with pytest.raises(account_import_store.ImportProblem):
            native_source_http._token_key()
    monkeypatch.setenv('PAMS_SOURCE_DEV_TOKEN_KEY', 'b2'*32)
    assert native_source_http._token_key() == bytes.fromhex('b2'*32)


def test_no_external_network_on_refresh_or_return_read(monkeypatch):
    from endpoints import prices
    from collectors import ibkr_flex
    from domain.market import fund_nav_estimates, price_history
    monkeypatch.setenv('PAMS_SOURCE_MODE', 'development')
    monkeypatch.delenv('PAMS_UNIFIED_SOURCE_ISOLATED', raising=False)
    for f, args in [(prices._fetch_stock_prices_tencent, (['000001'],)),
                    (prices._fetch_crypto_prices, (['BTC'],)),
                    (ibkr_flex.request_flex_reference, ('x', 'y')),
                    (fund_nav_estimates.fetch_confirmed_nav, ('000001',))]:
        with pytest.raises(isolation.IsolationError):
            f(*args)
    with pytest.raises(isolation.IsolationError):
        price_history.fetch_yfinance_history(['AAPL'], start=date(2025,1,1), end=date(2025,1,2))
    from fastapi import HTTPException
    for f in (prices.refresh_a_stock, prices.refresh_crypto):
        with pytest.raises(HTTPException) as exc:
            f()
        assert exc.value.status_code == 409
    rows, warnings = _fetch_yfinance_closes(['AAPL'], start_date=date(2025,1,1), end_date=date(2025,1,2))
    assert rows == {} and 'development mode' in warnings[0]


def test_development_manual_publish_and_replay(monkeypatch):
    with scratch_database(development=True) as params:
        monkeypatch.setenv('PAMS_SOURCE_MODE', 'development')
        monkeypatch.delenv('PAMS_UNIFIED_SOURCE_ISOLATED', raising=False)
        monkeypatch.setenv('PAMS_SOURCE_DEV_TOKEN_KEY', 'b2'*32)
        monkeypatch.setenv('PAMS_IMPORT_PUBLISH_ENABLED', 'true')
        for key in ('host','port','dbname','user','password'):
            if key in params:
                monkeypatch.setenv({'host':'PGHOST','port':'PGPORT','dbname':'PGDATABASE','user':'PGUSER','password':'PGPASSWORD'}[key], str(params[key]))
        app = FastAPI()
        app.include_router(router, prefix='/api')
        with TestClient(app) as client:
            client.headers['Origin'] = 'http://testserver'
            batch = draft(client, [trade(source_ref='synthetic-dev')])
            script = '''
import json, sys
from fastapi import FastAPI
from fastapi.testclient import TestClient
sys.path.insert(0, 'backend/api')
from endpoints.account_import import router
app = FastAPI()
app.include_router(router, prefix='/api')
with TestClient(app) as c:
    c.headers['Origin'] = 'http://testserver'
    response = c.post('/api/account-import/batches/' + sys.argv[1] + '/preview', json={'revision':int(sys.argv[2])})
    response.raise_for_status()
    print(json.dumps(response.json()))
'''
            def worker(_):
                result = subprocess.run([sys.executable, '-c', script, str(batch['batch_id']), str(batch['revision'])], capture_output=True, text=True, check=True)
                return json.loads(result.stdout)
            with ThreadPoolExecutor(max_workers=2) as pool:
                checks = list(pool.map(worker, range(2)))
            assert all(check['can_publish'] for check in checks)
            assert checks[0]['preview_token'] == checks[1]['preview_token']
            result = publish(client, batch, checks[0])
            assert result.status_code == 200, result.text
            assert publish(client, batch, checks[1]).json()['replayed']
            assert sql(params, 'SELECT count(*) n FROM pams_investment_trade_fact')[0]['n'] == 1


def test_wrong_database_marker_is_rejected_and_connection_closed(monkeypatch):
    with scratch_database(development=True) as params:
        monkeypatch.setenv('PAMS_SOURCE_MODE', 'development')
        monkeypatch.delenv('PAMS_UNIFIED_SOURCE_ISOLATED', raising=False)
        from psycopg2 import sql as psql
        with psycopg2.connect(**params) as conn, conn.cursor() as cur:
            cur.execute(psql.SQL("COMMENT ON DATABASE {} IS 'incorrect-marker'").format(psql.Identifier(params['dbname'])))
        with pytest.raises(isolation.IsolationError, match='marker'):
            isolation.guarded_connect(**params)
