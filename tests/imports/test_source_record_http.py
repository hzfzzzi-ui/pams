"""Exercise redesigned source HTTP endpoints using synthetic isolated data."""
from pathlib import Path
import sys

import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend/api'))
from domain.imports import account_import_access, account_import_statement, native_source, native_source_http
from endpoints import account_import
from tests.imports.test_redesigned_fact_schema import install_redesign
from tests.imports.test_account_import_ths import event, export
from tests.support.account_import_support import scratch_database


class SchemaCursor:
    def __init__(self, inner): self.inner = inner
    @property
    def connection(self): return self.inner.connection
    @property
    def rowcount(self): return self.inner.rowcount
    def execute(self, sql, args=None): return self.inner.execute(sql.replace('public.', 'redesign_test.'), args)
    def fetchone(self): return self.inner.fetchone()
    def fetchall(self): return self.inner.fetchall()


def test_cash_source_upload_review_preview_and_publish(monkeypatch):
    monkeypatch.setattr(account_import_statement, 'profiles', lambda *args: {
        'ccb_test_1234': [{'statement_template': 'ccb_credit_pdf'}]})
    monkeypatch.setattr(native_source_http, '_token_key', lambda: b'synthetic-test-key')
    monkeypatch.setattr(native_source, 'enabled', lambda: True)
    data = ('中国建设银行信用卡交易明细\n交易日,入账日,信用卡卡号,类型,入账币种,入账金额,交易描述\n'
            '20260101,20260102,****1234,消费,人民币,20.00,示例商户\n').encode('gb18030')
    with scratch_database() as params, psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        install_redesign(cur)
        cur.execute("INSERT INTO redesign_test.pams_accounts VALUES ('ccb_test_1234'),('tonghuashun')")
        wrapped = SchemaCursor(cur)

        def execute(request, fn, *, write=False):
            if write:
                account_import_access.same_origin(request)
            try:
                return account_import.store.norm(fn(wrapped))
            except ValueError as error:
                raise HTTPException(400, detail=str(error)) from error

        monkeypatch.setattr(account_import, 'execute_source_record', execute)
        app = FastAPI()
        app.include_router(account_import.router, prefix='/api')
        with TestClient(app) as client:
            url = '/api/account-import/source-records?account_code=ccb_test_1234&source_name=synthetic&filename=test.csv'
            assert client.post(url, content=data, headers={'Origin': 'http://other'}).status_code == 403
            client.headers['Origin'] = 'http://testserver'
            staged = client.post(url, content=data)
            assert staged.status_code == 200, staged.text
            source_id = staged.json()['source_id']
            source = client.get('/api/account-import/source-records/' + source_id).json()
            item = source['items'][0]
            response = client.put('/api/account-import/source-records/' + source_id + '/items/' + item['source_item_id'],
                json={'revision': source['revision'], 'parsed_fields': item['parsed_fields'], 'review_status': 'confirmed'})
            assert response.status_code == 200, response.text
            revision = response.json()['revision']
            preview = client.post('/api/account-import/source-records/' + source_id + '/cash-preview',
                                  json={'revision': revision, 'mode': 'all'}).json()
            assert preview['can_publish'] and preview['preview_token']
            response = client.post('/api/account-import/source-records/' + source_id + '/cash-publish',
                json={'revision': revision, 'mode': 'all', 'preview_token': preview['preview_token']})
            assert response.status_code == 200 and response.json()['published'] == 1
            broker_url = '/api/account-import/source-records?account_code=tonghuashun&source_name=synthetic&filename=table.xls'
            staged = client.post(broker_url, content=export(event()))
            assert staged.status_code == 200, staged.text
            source_id = staged.json()['source_id']
            source = client.get('/api/account-import/source-records/' + source_id).json()
            item = source['items'][0]
            response = client.put('/api/account-import/source-records/' + source_id + '/items/' + item['source_item_id'],
                json={'revision': source['revision'], 'parsed_fields': item['parsed_fields'], 'review_status': 'confirmed'})
            revision = response.json()['revision']
            preview = client.post('/api/account-import/source-records/' + source_id + '/trade-preview',
                                  json={'revision': revision})
            assert preview.status_code == 200, preview.text
            response = client.post('/api/account-import/source-records/' + source_id + '/trade-publish',
                json={'revision': revision, 'preview_token': preview.json()['preview_token']})
            assert response.status_code == 200 and response.json()['published'] == 1


def test_new_source_routes_fail_closed_against_legacy_schema(monkeypatch):
    with scratch_database() as params:
        monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED', 'true')
        monkeypatch.delenv('PAMS_SOURCE_MODE', raising=False)
        monkeypatch.setattr(account_import.store, 'get_conn', lambda: psycopg2.connect(**params))
        app = FastAPI()
        app.include_router(account_import.router, prefix='/api')
        with TestClient(app) as client:
            response = client.get('/api/account-import/source-records/00000000-0000-0000-0000-000000000001')
            assert response.status_code == 503 and response.json()['detail']['code'] == 'schema_unavailable'


def test_batch_reviews_add_manual_item_atomically():
    from uuid import UUID
    from domain.imports import source_records_v2

    with scratch_database() as params, psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        install_redesign(cur)
        source_id = UUID('00000000-0000-0000-0000-000000000010')
        wrapped = SchemaCursor(cur)
        source_records_v2.create_source(wrapped, source_id=source_id, account_code='bank-a', source_name='synthetic',
                                       acquisition_method='screenshot', document_type='cash', filename='synthetic.png',
                                       file_format='png', content=b'synthetic image')
        fields = {'business_date': '2026-01-01', 'net_cash': '12.00', 'currency': 'CNY'}
        missing = UUID('00000000-0000-0000-0000-000000000011')
        cur.execute('SAVEPOINT before_review')
        try:
            source_records_v2.review_items(wrapped, source_id=source_id, expected_revision=1, items=[
                {'source_item_id': None, 'parsed_fields': fields, 'review_status': 'confirmed'},
                {'source_item_id': missing, 'parsed_fields': fields, 'review_status': 'confirmed'},
            ])
            assert False, 'missing source item must fail'
        except ValueError as error:
            assert 'missing' in str(error)
        cur.execute('ROLLBACK TO SAVEPOINT before_review')
        cur.execute('SELECT revision FROM redesign_test.source_records WHERE source_id=%s', (str(source_id),))
        assert cur.fetchone()['revision'] == 1
        cur.execute('SELECT count(*) AS count FROM redesign_test.source_items WHERE source_id=%s', (str(source_id),))
        assert cur.fetchone()['count'] == 0
        revision = source_records_v2.review_items(wrapped, source_id=source_id, expected_revision=1, items=[
            {'source_item_id': None, 'parsed_fields': fields, 'review_status': 'confirmed'},
        ])
        assert revision == 3
        cur.execute('SELECT parse_method, review_status, raw_fields FROM redesign_test.source_items WHERE source_id=%s', (str(source_id),))
        item = cur.fetchone()
        assert item['parse_method'] == 'manual' and item['review_status'] == 'confirmed'
        assert item['raw_fields']['entered_fields'] == fields
