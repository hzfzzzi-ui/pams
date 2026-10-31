"""Exercise the existing HTTP endpoints against an explicitly disposable database."""
# ruff: noqa: F811
import psycopg2
from psycopg2.extras import RealDictCursor
import pytest

from tests.imports.test_account_import_publish import client, draft, trade, preview, publish, sql  # noqa: F401
from tests.database.test_source_candidate_publish import setup_candidate
from domain.imports import account_import_publish as publisher
from domain.imports import account_import_store as store
from domain.imports.unified_source import review_item


def test_http_bridge_links_fact_and_replays(client, monkeypatch):
    web, params = client
    batch = draft(web, [trade(source_ref='isolated-http-bridge')])
    setup_candidate(params, batch)
    monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED', 'true')
    check = preview(web, batch)
    result = publish(web, batch, check)
    assert result.status_code == 200, result.text
    record = result.json()['records'][0]
    fact = sql(params, 'SELECT source_item_id FROM pams_investment_trade_fact')[0]
    assert str(fact['source_item_id']) == record['row_id']
    replay = publish(web, batch, check)
    assert replay.status_code == 200 and replay.json()['replayed']
    assert sql(params, 'SELECT count(*) AS n FROM pams_investment_trade_fact')[0]['n'] == 1


def test_http_bridge_rejects_changed_source_after_preview(client, monkeypatch):
    web, params = client
    batch = draft(web, [trade(source_ref='isolated-http-changed')])
    rev = setup_candidate(params, batch)
    monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED', 'true')
    check = preview(web, batch)
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute('SELECT source_item_id, parsed_fields FROM source_items WHERE batch_id=%s', (batch['batch_id'],))
        item = cur.fetchone()
        review_item(cur, batch_id=batch['batch_id'], item_id=item['source_item_id'], expected_revision=rev,
                    parsed_fields=item['parsed_fields'], review_status='confirmed', review_note='changed')
    response = publish(web, batch, check)
    assert response.status_code == 409, response.text
    assert sql(params, 'SELECT count(*) AS n FROM pams_investment_trade_fact')[0]['n'] == 0
    assert sql(params, 'SELECT status FROM source_batches')[0]['status'] == 'draft'


def test_bridge_cannot_be_bypassed_by_disabling_flag_after_preview(client, monkeypatch):
    web, params = client
    batch = draft(web, [trade(source_ref='isolated-http-disable')])
    setup_candidate(params, batch)
    monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED', 'true')
    check = preview(web, batch)
    monkeypatch.delenv('PAMS_UNIFIED_SOURCE_ISOLATED')
    assert publish(web, batch, check).status_code == 409
    response = web.post(f"/api/account-import/batches/{batch['batch_id']}/preview", json={'revision': batch['revision']})
    assert response.status_code == 409
    assert sql(params, 'SELECT count(*) AS n FROM pams_investment_trade_fact')[0]['n'] == 0


def test_publication_rejects_unexpected_existing_fact_link(client, monkeypatch):
    web, params = client
    earlier = draft(web, [trade(source_ref='existing-independent-fact')])
    assert publish(web, earlier, preview(web, earlier)).status_code == 200
    batch = draft(web, [trade(source_ref='new-reviewed-source')])
    setup_candidate(params, batch)
    monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED', 'true')
    check = preview(web, batch)
    sql(params, 'UPDATE pams_investment_trade_fact SET source_item_id=%s', (batch['rows'][0]['row_id'],))
    response = publish(web, batch, check)
    assert response.status_code == 409, response.text
    assert sql(params, 'SELECT count(*) AS n FROM pams_investment_trade_fact')[0]['n'] == 1
    assert sql(params, 'SELECT status FROM source_batches')[0]['status'] == 'draft'


@pytest.mark.parametrize('host,name,marker', [
    ('db.example', 'pams_import_test_' + 'a' * 32, 'pams-isolated-account-import'),
    ('127.0.0.1', 'production', 'pams-isolated-account-import'),
    ('127.0.0.1', 'pams_import_test_' + 'a' * 32, None),
])
def test_bridge_refuses_non_disposable_database(monkeypatch, host, name, marker):
    class Connection:
        server_version = 160000

        def get_dsn_parameters(self):
            return {'host': host, 'dbname': name}

    class Cursor:
        connection = Connection()

        def execute(self, statement):
            assert statement.startswith('SELECT shobj_description')

        def fetchone(self):
            return {'marker': marker}

    monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED', 'true')
    with pytest.raises(store.ImportProblem) as exc:
        publisher.preview(Cursor(), 'unused', 1)
    assert exc.value.code == 'isolated_only'
