# ruff: noqa: F811
from datetime import date
from decimal import Decimal

import psycopg2
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from scripts import bootstrap_isolated_sources as bootstrap


def _client(empty_source_db, monkeypatch, accounts):
    from domain.imports import account_import_statement as statement, account_import_store as store
    from endpoints import account_import, bank_reconcile, ledger
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            for account in accounts:
                cur.execute("INSERT INTO pams_accounts(account_code,display_name,base_currency) VALUES (%s,%s,'USD')", (account, account))
    monkeypatch.setenv('PAMS_SOURCE_TEST_TOKEN_KEY', 'a1' * 32)
    monkeypatch.setenv('PAMS_IMPORT_PUBLISH_ENABLED', 'true')
    for module in (store, bank_reconcile, ledger):
        monkeypatch.setattr(module, 'get_conn', lambda: psycopg2.connect(**empty_source_db))
    profiles = {account: {'account_code': account, 'statement_template': 'manual_statement_csv', 'currency': 'USD'} for account in accounts}
    monkeypatch.setattr(statement, 'config', lambda: {'profiles': profiles, 'rules': [], 'amount_rules': []})
    app = FastAPI()
    for router in (account_import.router, bank_reconcile.router, ledger.router):
        app.include_router(router, prefix='/api')
    return TestClient(app)


def _import_statement(web, account, amount):
    url = f'/api/account-import/source-records?account_code={account}&source_name=synthetic&filename=bank.csv'
    content = f'transaction_date,amount,currency,summary\n2025-08-12,{amount},USD,synthetic\n'.encode()
    response = web.post(url, content=content)
    assert response.status_code == 200, response.text
    path = '/api/account-import/source-records/' + response.json()['source_id']
    draft = web.get(path).json()
    item = draft['items'][0]
    reviewed = web.put(path + '/items/' + item['source_item_id'], json={'revision': draft['revision'],
        'parsed_fields': item['parsed_fields'], 'review_status': 'confirmed'})
    assert reviewed.status_code == 200, reviewed.text
    revision = reviewed.json()['revision']
    preview = web.post(path + '/cash-preview', json={'revision': revision, 'mode': 'all'})
    assert preview.status_code == 200 and preview.json()['can_publish'], preview.text
    published = web.post(path + '/cash-publish', json={'revision': revision, 'mode': 'all',
        'preview_token': preview.json()['preview_token']})
    assert published.status_code == 200, published.text
    return path, content


def test_foreign_statement_import_preserves_native_amount_without_fx(empty_source_db, monkeypatch):
    with _client(empty_source_db, monkeypatch, ['usd_bank']) as web:
        web.headers['Origin'] = 'http://testserver'
        path, content = _import_statement(web, 'usd_bank', '-10')
        imports = web.get('/api/bank-reconcile/imports')
        assert imports.status_code == 200, imports.text
        assert imports.json()['summary'][0]['currency'] == 'USD'
        duplicate = web.post('/api/account-import/source-records?account_code=usd_bank&source_name=synthetic&filename=bank.csv', content=content)
        assert duplicate.status_code == 400
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
            cur.execute('SELECT cashflow_id,amount,currency FROM pams_cashflow_fact')
            cash_id, amount, currency = cur.fetchone()
            assert amount == Decimal('-10') and currency == 'USD'
        confirmed = web.post(f'/api/ledger/rows/{cash_id}/update', json={'cashflow_type': 'operating_expense', 'category': 'synthetic'})
        assert confirmed.status_code == 200, confirmed.text
        data = web.get('/api/ledger/workbench', params={'month': '2025-08', 'baseCurrency': 'CNY'})
        assert data.status_code == 200, data.text
        row = next(row for row in data.json()['recent_rows_month'] if row['account_code'] == 'usd_bank')
        assert row['amount'] == -10 and row['amount_cny'] is None and row['source_evidence_status'] == 'verified'
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7.2,'synthetic_fx')", (date(2025,8,12),))
        row = next(row for row in web.get('/api/ledger/workbench', params={'month': '2025-08', 'baseCurrency': 'CNY'}).json()['recent_rows_month'] if row['account_code'] == 'usd_bank')
        assert row['amount_cny'] == -72
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
            cur.execute('UPDATE pams_cashflow_fact SET amount=-11 WHERE cashflow_id=%s', (cash_id,))
        row = next(row for row in web.get('/api/ledger/workbench', params={'month': '2025-08', 'baseCurrency': 'CNY'}).json()['recent_rows_month'] if row['account_code'] == 'usd_bank')
        assert row['source_evidence_status'] == 'unknown' and row['amount_cny'] is None


def test_foreign_transfer_requires_two_proven_sides_and_replays(empty_source_db, monkeypatch):
    with _client(empty_source_db, monkeypatch, ['usd_a', 'usd_b']) as web:
        web.headers['Origin'] = 'http://testserver'
        _import_statement(web, 'usd_a', '-10')
        _import_statement(web, 'usd_b', '10')
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
            cur.execute('SELECT cashflow_id FROM pams_cashflow_fact ORDER BY account_code')
            first, second = [row[0] for row in cur.fetchall()]
        for ident in (first, second):
            classified = web.post(f'/api/ledger/rows/{ident}/update', json={'cashflow_type': 'internal_transfer'})
            assert classified.status_code == 200, classified.text
        candidates = web.post('/api/bank-reconcile/transfer-matches/run')
        assert candidates.status_code == 200, candidates.text
        proposed = candidates.json()['matches']
        assert len(proposed) == 1 and proposed[0]['source_row_id'] == first and proposed[0]['target_row_id'] == second
        assert proposed[0]['status'] == 'review_required' and proposed[0]['confirmation_supported'] is True
        payload = {'from_kind': 'cashflow', 'from_id': first, 'to_kind': 'cashflow', 'to_id': second,
                   'note': '双方核对', 'confirmed': True}
        response = web.post('/api/bank-reconcile/transfers/confirm', json=payload)
        assert response.status_code == 200 and not response.json()['replayed'], response.text
        replay = web.post('/api/bank-reconcile/transfers/confirm', json=payload)
        assert replay.status_code == 200 and replay.json()['replayed'], replay.text
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES ('2025-08-12','CNY','USD',7.2,'synthetic_fx')")
        matches = web.get('/api/bank-reconcile/transfer-matches').json()['matches']
        assert matches[0]['evidence_status'] == 'confirmed'
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
            cur.execute('UPDATE pams_cashflow_fact SET amount=-11 WHERE cashflow_id=%s', (first,))
        matches = web.get('/api/bank-reconcile/transfer-matches').json()['matches']
        assert matches[0]['evidence_status'] == 'invalidated'
