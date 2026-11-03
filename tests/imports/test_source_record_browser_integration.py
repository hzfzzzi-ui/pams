"""Browser-to-database import against a disposable PostgreSQL database."""
import base64
import os
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time

import psycopg2
from psycopg2.extras import RealDictCursor
import pytest
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from tests.imports.test_source_record_http import SchemaCursor
from tests.imports.test_account_import_ths import event, export
from tests.imports.test_redesigned_fact_schema import install_redesign
from tests.support.account_import_support import scratch_database

ROOT = Path(__file__).resolve().parents[2]


def test_new_source_browser_to_postgres(monkeypatch):
    if not os.environ.get('PAMS_FRONTEND_PLAYWRIGHT_MODULE') or not shutil.which('node'):
        pytest.skip('Optional offline browser runtime not configured')
    from endpoints import account_import
    from domain.imports import account_import_statement, native_source, native_source_http
    monkeypatch.setattr(account_import_statement, 'profiles', lambda *args: {
        'ccb_test_1234': [{'statement_template': 'ccb_credit_pdf'}]})
    monkeypatch.setattr(native_source_http, '_token_key', lambda: b'synthetic-test-key')
    monkeypatch.setattr(native_source, 'enabled', lambda: True)
    document = ('中国建设银行信用卡交易明细\n交易日,入账日,信用卡卡号,类型,入账币种,入账金额,交易描述\n'
                '20260101,20260102,****1234,消费,人民币,20.00,示例商户\n').encode('gb18030')
    with scratch_database() as params:
        with psycopg2.connect(**params) as conn, conn.cursor() as cur:
            install_redesign(cur)
            cur.execute("ALTER TABLE redesign_test.pams_accounts ADD COLUMN display_name text, ADD COLUMN base_currency text DEFAULT 'CNY', ADD COLUMN active boolean DEFAULT true")
            cur.execute("INSERT INTO redesign_test.pams_accounts(account_code,display_name) VALUES ('ccb_test_1234','Synthetic bank'),('tonghuashun','Synthetic broker')")
        def execute(request, fn, *, write=False):
            if write:
                account_import.access.same_origin(request)
            try:
                with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute('SET LOCAL search_path TO redesign_test,public')
                    return account_import.store.norm(fn(SchemaCursor(cur)))
            except ValueError as error:
                raise HTTPException(400, detail=str(error)) from error
        monkeypatch.setattr(account_import, 'execute_source_record', execute)
        app = FastAPI()
        @app.middleware('http')
        async def capabilities(request: Request, call_next):
            if request.url.path == '/api/account-import/capabilities':
                return JSONResponse({'schema_ready': False, 'source_record_schema_ready': True,
                                     'source_record_publish_enabled': True, 'limits': {'files': 10, 'file_bytes': 10485760}})
            return await call_next(request)
        app.include_router(account_import.router, prefix='/api')
        app.mount('/', StaticFiles(directory=ROOT / 'frontend/public', html=True), name='static')
        sock = socket.socket(); sock.bind(('127.0.0.1', 0)); sock.listen(128)
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='off'))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.05)
            assert server.started
            env = {**os.environ, 'PAMS_IMPORT_TEST_URL': f'http://127.0.0.1:{port}',
                   'PAMS_IMPORT_TEST_DOCUMENT': base64.b64encode(document).decode(),
                   'PAMS_IMPORT_TEST_TRADE': base64.b64encode(export(event())).decode()}
            result = subprocess.run(['node', str(ROOT / 'tests/frontend/account_import_real_source_browser.mjs')],
                                    cwd=ROOT, env=env, capture_output=True, text=True, encoding='utf-8', timeout=90)
            assert result.returncode == 0, result.stdout + result.stderr
            with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute('SELECT amount,account_code,source_item_id FROM redesign_test.pams_cashflow_fact')
                fact = cur.fetchone()
                assert fact and str(fact['amount']) == '-20.0000' and fact['account_code'] == 'ccb_test_1234'
                cur.execute('SELECT review_status FROM redesign_test.source_items WHERE source_item_id=%s', (str(fact['source_item_id']),))
                assert cur.fetchone()['review_status'] == 'confirmed'
                cur.execute('SELECT quantity,gross_amount,net_cash,source_item_id FROM redesign_test.pams_investment_trade_fact')
                trade = cur.fetchone()
                assert trade and str(trade['quantity']) == '100.0000000000'
                assert str(trade['gross_amount']) == '1000.0000000000' and str(trade['net_cash']) == '-1003.1000000000'
                cur.execute('SELECT review_status FROM redesign_test.source_items WHERE source_item_id=%s', (str(trade['source_item_id']),))
                assert cur.fetchone()['review_status'] == 'confirmed'
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            sock.close()
