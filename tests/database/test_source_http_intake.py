"""Opt-in HTTP draft synchronization on a disposable PostgreSQL database."""
# ruff: noqa: F811
from copy import deepcopy
from pathlib import Path

import psycopg2
import pytest

from tests.imports.test_account_import_publish import client, draft, trade, preview, publish, sql, native_batch, DAY  # noqa: F401
from domain.imports import account_import_store as store
from domain.imports import account_import_statement as statement

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def isolated(client, monkeypatch):
    web, params = client
    with psycopg2.connect(**params) as conn, conn.cursor() as cur:
        for name in ('080_unified_source_staging.sql', '081_unified_source_fact_links.sql'):
            cur.execute((ROOT / 'database/postgres/schema' / name).read_text(encoding='utf-8'))
    monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED', 'true')
    monkeypatch.setenv('PAMS_SOURCE_LEGACY_BRIDGE', 'true')
    return web, params


def test_manual_create_edit_and_publish_preserves_original_evidence(isolated):
    web, params = isolated
    batch = draft(web, [trade(source_ref='http-manual-source')])
    item = sql(params, 'SELECT * FROM source_items')[0]
    assert item['review_status'] == 'confirmed' and item['parse_method'] == 'manual'
    original = item['raw_fields']['manual_draft_fields']
    rows = deepcopy(batch['rows'])
    rows[0]['fields']['note'] = 'explicit correction'
    response = web.put(f"/api/account-import/batches/{batch['batch_id']}", json={'revision': batch['revision'], 'rows': rows})
    assert response.status_code == 200, response.text
    updated = response.json()
    item = sql(params, 'SELECT * FROM source_items')[0]
    assert item['raw_fields']['manual_draft_fields'] == original
    assert item['parsed_fields']['fields']['note'] == 'explicit correction'
    stale = web.put(f"/api/account-import/batches/{batch['batch_id']}", json={'revision': batch['revision'], 'rows': rows})
    assert stale.status_code == 409
    check = preview(web, updated)
    assert check['can_publish']
    result = publish(web, updated, check)
    assert result.status_code == 200, result.text
    assert str(sql(params, 'SELECT source_item_id FROM pams_investment_trade_fact')[0]['source_item_id']) == item['source_item_id']


@pytest.mark.parametrize('preview_then_save', [False, True])
def test_disabling_bridge_cannot_bypass_unpreviewed_or_changed_draft(isolated, monkeypatch, preview_then_save):
    web, params = isolated
    batch = draft(web, [trade(source_ref='http-disable-source')])
    if preview_then_save:
        preview(web, batch)
        response = web.put(f"/api/account-import/batches/{batch['batch_id']}",
                           json={'revision': batch['revision'], 'rows': batch['rows']})
        assert response.status_code == 200, response.text
        batch = response.json()
    monkeypatch.delenv('PAMS_UNIFIED_SOURCE_ISOLATED')
    path = f"/api/account-import/batches/{batch['batch_id']}"
    assert web.post(path + '/preview', json={'revision': batch['revision']}).status_code == 409
    assert web.put(path, json={'revision': batch['revision'], 'rows': batch['rows']}).status_code == 409
    assert web.post(path + '/publish', json={'revision': batch['revision'], 'preview_token': '0' * 64}).status_code == 409
    assert sql(params, 'SELECT count(*) AS n FROM pams_investment_trade_fact')[0]['n'] == 0


def test_upload_requires_explicit_save_before_publication(isolated):
    web, params = isolated
    batch, _ = native_batch(web)
    assert len(sql(params, 'SELECT * FROM source_files')) == 1
    assert all(row['review_status'] == 'pending' and row['parse_method'] == 'template'
               for row in sql(params, 'SELECT * FROM source_items'))
    assert not preview(web, batch)['can_publish']
    for row in batch['rows']:
        if row['fields']['action'] in {'deposit', 'withdraw'}:
            row['fields']['counter_account_code'] = 'bank_test'
    response = web.put(f"/api/account-import/batches/{batch['batch_id']}", json={'revision': batch['revision'], 'rows': batch['rows']})
    assert response.status_code == 200, response.text
    batch = response.json()
    assert preview(web, batch)['can_publish']
    result = publish(web, batch, preview(web, batch))
    assert result.status_code == 200, result.text
    assert sql(params, 'SELECT count(*) AS n FROM pams_investment_trade_fact WHERE source_item_id IS NOT NULL')[0]['n'] == 7


def test_cash_statement_sources_link_only_published_cashflow(isolated, monkeypatch):
    web, params = isolated
    monkeypatch.setattr(statement, 'config', lambda: {'profiles': {'test': {'account_code': 'bank_test', 'profile_code': 'test',
        'statement_template': 'manual_statement_csv', 'currency': 'CNY'}}, 'rules': [], 'amount_rules': []})
    batch = web.post('/api/account-import/batches', json={'account_code': 'bank_test', 'mode': 'upload', 'kind': 'cash'}).json()
    path = f"/api/account-import/batches/{batch['batch_id']}"
    content = f'transaction_date,amount,currency,balance,summary\n{DAY},100.05,CNY,200.05,工资\n'.encode()
    response = web.post(path + '/files?filename=bank.csv', content=content)
    assert response.status_code == 200, response.text
    batch = response.json()
    assert len(batch['rows']) == 2 and batch['rows'][1]['excluded']
    response = web.put(path, json={'revision': batch['revision'], 'rows': batch['rows']})
    assert response.status_code == 200, response.text
    batch = response.json()
    check = preview(web, batch)
    assert check['can_publish'] and check['write_count'] == 1
    result = publish(web, batch, check)
    assert result.status_code == 200, result.text
    facts = sql(params, 'SELECT source_item_id, amount FROM pams_cashflow_fact')
    assert len(facts) == 1 and str(facts[0]['source_item_id']) == batch['rows'][0]['row_id']
    assert sql(params, "SELECT count(*) AS n FROM source_items WHERE review_status='excluded'")[0]['n'] == 1


def test_reparse_replaces_candidates_but_keeps_original_file(isolated, monkeypatch):
    web, params = isolated
    monkeypatch.setattr(store, 'parse', lambda content, filename, account, kind, mapping: {
        'origin': 'file', 'rows': [{'fields': trade(note='mapped' if mapping else 'initial'), 'source': {'row': 1}, 'issues': []}]})
    batch = web.post('/api/account-import/batches', json={'account_code': 'tonghuashun', 'mode': 'upload', 'kind': 'trade'}).json()
    path = f"/api/account-import/batches/{batch['batch_id']}"
    response = web.post(path + '/files?filename=trades.csv', content=b'synthetic csv')
    assert response.status_code == 200, response.text
    batch = response.json()
    original_id = batch['rows'][0]['row_id']
    file_id = batch['files'][0]['file_id']
    response = web.post(path + f'/files/{file_id}/parse', json={'mapping': {'symbol': 'ticker'}})
    assert response.status_code == 200, response.text
    batch = response.json()
    item = sql(params, 'SELECT source_item_id, review_status, parsed_fields FROM source_items')[0]
    assert str(item['source_item_id']) != original_id
    assert item['parsed_fields']['fields']['note'] == 'mapped' and item['review_status'] == 'pending'
    assert bytes(sql(params, 'SELECT content FROM source_files')[0]['content']) == b'synthetic csv'


def test_sync_error_rolls_back_legacy_draft(isolated, monkeypatch):
    from domain.imports import source_import_adapter

    web, params = isolated
    batch = draft(web, [trade(source_ref='http-atomic-source')])
    before = sql(params, 'SELECT revision, rows_json FROM pams_account_import_batch')[0]
    def fail_sync(*args, **kwargs):
        raise ValueError('synthetic source synchronization failure')
    monkeypatch.setattr(source_import_adapter, 'sync_legacy_draft', fail_sync)
    rows = deepcopy(batch['rows'])
    rows[0]['fields']['note'] = 'should roll back'
    response = web.put(f"/api/account-import/batches/{batch['batch_id']}", json={'revision': batch['revision'], 'rows': rows})
    assert response.status_code == 400
    assert sql(params, 'SELECT revision, rows_json FROM pams_account_import_batch')[0] == before


def test_ocr_empty_file_then_human_row_and_review(isolated, monkeypatch):
    web, params = isolated
    monkeypatch.setattr(store, 'parse', lambda *args: {'origin': 'ocr', 'rows': []})
    response = web.post('/api/account-import/batches', json={'account_code': 'tonghuashun', 'mode': 'upload', 'kind': 'trade'})
    batch = response.json()
    path = f"/api/account-import/batches/{batch['batch_id']}"
    response = web.post(path + '/files?filename=missing.png', content=b'synthetic image')
    assert response.status_code == 200, response.text
    batch = response.json()
    file_id = batch['files'][0]['file_id']
    assert len(sql(params, 'SELECT * FROM source_files')) == 1
    response = web.put(path, json={'revision': batch['revision'], 'rows': [{'fields': trade(), 'image_file_id': file_id}]})
    assert response.status_code == 200, response.text
    batch = response.json()
    assert sql(params, 'SELECT review_status FROM source_items')[0]['review_status'] == 'pending'
    assert not preview(web, batch)['can_publish']
    response = web.post(path + f'/files/{file_id}/review', json={'revision': batch['revision']})
    assert response.status_code == 200, response.text
    batch = response.json()
    assert sql(params, 'SELECT review_status FROM source_items')[0]['review_status'] == 'confirmed'
    check = preview(web, batch)
    assert check['can_publish']
    response = publish(web, batch, check)
    assert response.status_code == 200, response.text


def test_review_of_unmodified_image_survives_edit_to_other_image(isolated, monkeypatch):
    web, params = isolated
    monkeypatch.setattr(store, 'parse', lambda *args: {'origin': 'ocr', 'rows': [{'fields': trade(), 'source': {'row': 1}, 'issues': []}]})
    batch = web.post('/api/account-import/batches', json={'account_code': 'tonghuashun', 'mode': 'upload', 'kind': 'trade'}).json()
    path = f"/api/account-import/batches/{batch['batch_id']}"
    for name in ('one', 'two'):
        response = web.post(path + f'/files?filename={name}.png', content=name.encode())
        assert response.status_code == 200, response.text
        batch = response.json()
    for file in batch['files']:
        response = web.post(path + f"/files/{file['file_id']}/review", json={'revision': batch['revision']})
        assert response.status_code == 200, response.text
        batch = response.json()
    rows = deepcopy(batch['rows'])
    rows[0]['fields']['note'] = 'corrected first screenshot'
    response = web.put(path, json={'revision': batch['revision'], 'rows': rows})
    assert response.status_code == 200, response.text
    statuses = {str(row['source_item_id']): row['review_status'] for row in sql(params, 'SELECT source_item_id, review_status FROM source_items')}
    assert statuses[rows[0]['row_id']] == 'pending'
    assert statuses[rows[1]['row_id']] == 'confirmed'
