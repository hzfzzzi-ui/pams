"""Migration of old drafts occurs only in the marked disposable database."""
# ruff: noqa: F811
from copy import deepcopy
from pathlib import Path

import psycopg2

from tests.imports.test_account_import_publish import client, draft, trade, preview, publish, sql, native_batch  # noqa: F401
from domain.imports import account_import_store as store

ROOT = Path(__file__).resolve().parents[2]


def enable_candidate(params, monkeypatch):
    with psycopg2.connect(**params) as conn, conn.cursor() as cur:
        for name in ('080_unified_source_staging.sql', '081_unified_source_fact_links.sql'):
            cur.execute((ROOT / 'database/postgres/schema' / name).read_text(encoding='utf-8'))
    monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED', 'true')


def test_migration_before_edit_preserves_original_manual_evidence(client, monkeypatch):
    web, params = client
    batch = draft(web, [trade(source_ref='old-manual')])
    original = deepcopy(batch['rows'][0]['fields'])
    enable_candidate(params, monkeypatch)
    rows = deepcopy(batch['rows'])
    rows[0]['fields']['note'] = 'corrected after migration'
    path = f"/api/account-import/batches/{batch['batch_id']}"
    response = web.put(path, json={'revision': batch['revision'], 'rows': rows})
    assert response.status_code == 200, response.text
    batch = response.json()
    source = sql(params, 'SELECT raw_fields, parsed_fields, review_status FROM source_items')[0]
    assert source['raw_fields']['manual_draft_fields'] == original
    assert source['parsed_fields']['fields']['note'] == 'corrected after migration'
    assert source['review_status'] == 'confirmed'
    check = preview(web, batch)
    assert check['can_publish']
    response = publish(web, batch, check)
    assert response.status_code == 200, response.text
    assert publish(web, batch, check).json()['replayed']


def test_first_preview_stages_legacy_without_inferring_approval(client, monkeypatch):
    web, params = client
    batch = draft(web, [trade(source_ref='old-preview')])
    enable_candidate(params, monkeypatch)
    check = preview(web, batch)
    assert not check['can_publish']
    assert sql(params, 'SELECT review_status FROM source_items')[0]['review_status'] == 'pending'
    assert publish(web, batch, check).status_code == 409
    response = web.put(f"/api/account-import/batches/{batch['batch_id']}",
                       json={'revision': batch['revision'], 'rows': batch['rows']})
    assert response.status_code == 200, response.text
    assert preview(web, response.json())['can_publish']


def test_stale_legacy_preview_rolls_back_staging(client, monkeypatch):
    web, params = client
    batch = draft(web, [trade(source_ref='old-stale')])
    enable_candidate(params, monkeypatch)
    response = web.post(f"/api/account-import/batches/{batch['batch_id']}/preview", json={'revision': batch['revision'] - 1})
    assert response.status_code == 409
    assert sql(params, 'SELECT count(*) AS n FROM source_batches')[0]['n'] == 0
    assert sql(params, 'SELECT count(*) AS n FROM source_items')[0]['n'] == 0


def test_migration_preserves_valid_existing_image_review(client, monkeypatch):
    web, params = client
    monkeypatch.setattr(store, 'parse', lambda *args: {'origin': 'ocr', 'rows': [{'fields': trade(), 'source': {'row': 1}, 'issues': []}]})
    batch = web.post('/api/account-import/batches', json={'account_code': 'tonghuashun', 'mode': 'upload', 'kind': 'trade'}).json()
    path = f"/api/account-import/batches/{batch['batch_id']}"
    batch = web.post(path + '/files?filename=old.png', content=b'old synthetic image').json()
    file_id = batch['files'][0]['file_id']
    response = web.post(path + f'/files/{file_id}/review', json={'revision': batch['revision']})
    assert response.status_code == 200, response.text
    batch = response.json()
    enable_candidate(params, monkeypatch)
    check = preview(web, batch)
    assert check['can_publish']
    assert sql(params, 'SELECT review_status FROM source_items')[0]['review_status'] == 'confirmed'
    assert bytes(sql(params, 'SELECT content FROM source_files')[0]['content']) == b'old synthetic image'
    assert publish(web, batch, check).status_code == 200


def test_old_uploaded_file_is_preserved_and_reconfirmed(client, monkeypatch):
    web, params = client
    batch, content = native_batch(web)
    enable_candidate(params, monkeypatch)
    assert not preview(web, batch)['can_publish']
    assert bytes(sql(params, 'SELECT content FROM source_files')[0]['content']) == content
    assert all(row['review_status'] == 'pending' for row in sql(params, 'SELECT review_status FROM source_items'))
    rows = deepcopy(batch['rows'])
    for row in rows:
        if row['fields']['action'] in {'deposit', 'withdraw'}:
            row['fields']['counter_account_code'] = 'bank_test'
    response = web.put(f"/api/account-import/batches/{batch['batch_id']}", json={'revision': batch['revision'], 'rows': rows})
    assert response.status_code == 200, response.text
    batch = response.json()
    check = preview(web, batch)
    assert check['can_publish']
    assert publish(web, batch, check).status_code == 200


def test_published_legacy_batch_is_not_retroactively_claimed(client, monkeypatch):
    web, params = client
    batch = draft(web, [trade(source_ref='old-published')])
    check = preview(web, batch)
    assert publish(web, batch, check).status_code == 200
    enable_candidate(params, monkeypatch)
    assert publish(web, batch, check).status_code == 409
    assert sql(params, 'SELECT count(*) AS n FROM source_batches')[0]['n'] == 0
    assert sql(params, 'SELECT source_item_id FROM pams_investment_trade_fact')[0]['source_item_id'] is None
