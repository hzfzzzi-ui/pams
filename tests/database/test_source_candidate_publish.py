"""Isolated integration of the source candidate with the existing publisher."""
# ruff: noqa: F811
from pathlib import Path
import sys

import psycopg2
from psycopg2.extras import Json, RealDictCursor
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend/api'))
from tests.imports.test_account_import_publish import client, draft, trade, holding, native_batch  # noqa: F401
from domain.imports import account_import_store as store
from domain.imports.source_import_adapter import stage_legacy_draft, prepare_candidate, publish_candidate
from domain.imports.unified_source import review_item

ROOT = Path(__file__).resolve().parents[2]


def setup_candidate(params, batch, *, create_schema=True):
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        if create_schema:
            for name in ('080_unified_source_staging.sql', '081_unified_source_fact_links.sql'):
                cur.execute((ROOT / 'database/postgres/schema' / name).read_text(encoding='utf-8'))
        legacy = store.batch(cur, batch['batch_id'], lock=True)
        cur.execute('SELECT * FROM pams_account_import_file WHERE batch_id=%s', (batch['batch_id'],))
        files = [dict(row) for row in cur.fetchall()]
        stage_legacy_draft(cur, legacy, files, institution='synthetic-broker')
        cur.execute('SELECT source_item_id, parsed_fields, review_status FROM source_items WHERE batch_id=%s ORDER BY source_uid', (batch['batch_id'],))
        items = cur.fetchall()
        cur.execute('SELECT revision FROM source_batches WHERE batch_id=%s', (batch['batch_id'],))
        rev = cur.fetchone()['revision']
        for item in items:
            if item['review_status'] != 'excluded':
                rev = review_item(cur, batch_id=batch['batch_id'], item_id=item['source_item_id'],
                                  expected_revision=rev, parsed_fields=item['parsed_fields'],
                                  review_status='confirmed', review_note='synthetic confirmation')
    return rev


def test_candidate_publish_and_replay_preserve_source_identity(client):
    web, params = client
    batch = draft(web, [trade(source_ref='synthetic-external-001')])
    rev = setup_candidate(params, batch)
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        prepared = prepare_candidate(cur, batch_id=batch['batch_id'], legacy_revision=batch['revision'], source_revision=rev)
        assert prepared['legacy_preview']['can_publish']
    token = prepared['legacy_preview']['preview_token']
    evidence = prepared['source_snapshot_sha256']
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        result = publish_candidate(cur, batch_id=batch['batch_id'], legacy_revision=batch['revision'],
                                   source_revision=rev, preview_token=token, source_snapshot_sha256=evidence)
        assert not result['replayed'] and len(result['records']) == 1
        cur.execute('SELECT source_item_id FROM pams_investment_trade_fact')
        assert str(cur.fetchone()['source_item_id']) == str(result['records'][0]['row_id'])
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        replay = publish_candidate(cur, batch_id=batch['batch_id'], legacy_revision=batch['revision'],
                                   source_revision=rev, preview_token=token, source_snapshot_sha256=evidence)
        assert replay['replayed']
        cur.execute('SELECT count(*) AS n FROM pams_investment_trade_fact')
        assert cur.fetchone()['n'] == 1
        with pytest.raises(ValueError, match='snapshot'):
            publish_candidate(cur, batch_id=batch['batch_id'], legacy_revision=batch['revision'],
                              source_revision=rev, preview_token=token, source_snapshot_sha256='0' * 64)
        cur.execute('SAVEPOINT corrupt_result')
        cur.execute("UPDATE pams_account_import_batch SET published_result=jsonb_set(published_result, '{records}', '[]') WHERE batch_id=%s", (batch['batch_id'],))
        with pytest.raises(ValueError, match='do not cover'):
            publish_candidate(cur, batch_id=batch['batch_id'], legacy_revision=batch['revision'],
                              source_revision=rev, preview_token=token, source_snapshot_sha256=evidence)
        cur.execute('ROLLBACK TO SAVEPOINT corrupt_result')
        cur.execute('RELEASE SAVEPOINT corrupt_result')
    duplicate = draft(web, [trade(source_ref='synthetic-external-001')])
    duplicate_rev = setup_candidate(params, duplicate, create_schema=False)
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        with pytest.raises(ValueError, match='duplicate source'):
            prepare_candidate(cur, batch_id=duplicate['batch_id'], legacy_revision=duplicate['revision'], source_revision=duplicate_rev)
        cur.execute('SELECT count(*) AS n FROM pams_investment_trade_fact')
        assert cur.fetchone()['n'] == 1


def test_failed_fact_link_rolls_back_published_facts(client):
    web, params = client
    batch = draft(web, [trade(source_ref='synthetic-rollback-001')])
    rev = setup_candidate(params, batch)
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        prepared = prepare_candidate(cur, batch_id=batch['batch_id'], legacy_revision=batch['revision'], source_revision=rev)
        cur.execute("""CREATE FUNCTION reject_source_link() RETURNS trigger LANGUAGE plpgsql AS $$
                       BEGIN RAISE EXCEPTION 'synthetic link failure'; END $$""")
        cur.execute('CREATE TRIGGER reject_source_link BEFORE UPDATE OF source_item_id ON pams_investment_trade_fact FOR EACH ROW EXECUTE FUNCTION reject_source_link()')
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        with pytest.raises(psycopg2.errors.RaiseException, match='synthetic link failure'):
            publish_candidate(cur, batch_id=batch['batch_id'], legacy_revision=batch['revision'], source_revision=rev,
                              preview_token=prepared['legacy_preview']['preview_token'],
                              source_snapshot_sha256=prepared['source_snapshot_sha256'])
        cur.execute('SELECT count(*) AS n FROM pams_investment_trade_fact')
        assert cur.fetchone()['n'] == 0
        cur.execute('SELECT status FROM pams_account_import_batch WHERE batch_id=%s', (batch['batch_id'],))
        assert cur.fetchone()['status'] == 'draft'
        cur.execute('SELECT status FROM source_batches WHERE batch_id=%s', (batch['batch_id'],))
        assert cur.fetchone()['status'] == 'draft'


def test_cash_legs_share_origin_with_investment_facts(client):
    web, params = client
    batch, _ = native_batch(web)
    for row in batch['rows']:
        if row['fields']['action'] in {'deposit', 'withdraw'}:
            row['fields']['counter_account_code'] = 'bank_test'
    response = web.put(f"/api/account-import/batches/{batch['batch_id']}",
                       json={'revision': batch['revision'], 'rows': batch['rows']})
    assert response.status_code == 200
    batch = response.json()
    rev = setup_candidate(params, batch)
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        prepared = prepare_candidate(cur, batch_id=batch['batch_id'], legacy_revision=batch['revision'], source_revision=rev)
        assert prepared['legacy_preview']['can_publish']
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        result = publish_candidate(cur, batch_id=batch['batch_id'], legacy_revision=batch['revision'], source_revision=rev,
                                   preview_token=prepared['legacy_preview']['preview_token'],
                                   source_snapshot_sha256=prepared['source_snapshot_sha256'])
        legs = [record for record in result['records'] if record.get('role') == 'bank_leg']
        assert len(legs) == 2
        for leg in legs:
            cur.execute('SELECT source_item_id FROM pams_cashflow_fact WHERE cashflow_id=%s', (leg['id'],))
            assert str(cur.fetchone()['source_item_id']) == str(leg['row_id'])
            corresponding = [r for r in result['records'] if r['table'] == 'pams_investment_trade_fact' and r['row_id'] == leg['row_id']]
            assert len(corresponding) == 1
        cur.execute('SELECT status FROM source_batches WHERE batch_id=%s', (batch['batch_id'],))
        assert cur.fetchone()['status'] == 'published'
        cur.execute('SAVEPOINT omit_cash_leg')
        cur.execute("UPDATE pams_account_import_batch SET published_result=jsonb_set(published_result, '{records}', %s::jsonb) WHERE batch_id=%s",
                    (Json([r for r in result['records'] if r is not legs[0]]), batch['batch_id']))
        with pytest.raises(ValueError, match='cash legs'):
            publish_candidate(cur, batch_id=batch['batch_id'], legacy_revision=batch['revision'], source_revision=rev,
                              preview_token=prepared['legacy_preview']['preview_token'],
                              source_snapshot_sha256=prepared['source_snapshot_sha256'])
        cur.execute('ROLLBACK TO SAVEPOINT omit_cash_leg')
        cur.execute('RELEASE SAVEPOINT omit_cash_leg')


def test_existing_holding_source_conflict_rolls_back_snapshot_update(client):
    web, params = client
    first = draft(web, [holding(source_ref='synthetic-holding-first')])
    rev = setup_candidate(params, first)
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        prepared = prepare_candidate(cur, batch_id=first['batch_id'], legacy_revision=first['revision'], source_revision=rev)
        publish_candidate(cur, batch_id=first['batch_id'], legacy_revision=first['revision'], source_revision=rev,
                          preview_token=prepared['legacy_preview']['preview_token'],
                          source_snapshot_sha256=prepared['source_snapshot_sha256'])
        cur.execute('SELECT id, quantity, market_value, source_item_id FROM pams_holdings_snapshot')
        original = dict(cur.fetchone())
    second = draft(web, [holding(source_ref='synthetic-holding-second', quantity='110', market_value='1100')])
    second_rev = setup_candidate(params, second, create_schema=False)
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        prepared = prepare_candidate(cur, batch_id=second['batch_id'], legacy_revision=second['revision'], source_revision=second_rev)
        assert prepared['legacy_preview']['can_publish']
        with pytest.raises(ValueError, match='source association conflict'):
            publish_candidate(cur, batch_id=second['batch_id'], legacy_revision=second['revision'], source_revision=second_rev,
                              preview_token=prepared['legacy_preview']['preview_token'],
                              source_snapshot_sha256=prepared['source_snapshot_sha256'])
        cur.execute('SELECT id, quantity, market_value, source_item_id FROM pams_holdings_snapshot')
        assert dict(cur.fetchone()) == original
        cur.execute('SELECT status FROM pams_account_import_batch WHERE batch_id=%s', (second['batch_id'],))
        assert cur.fetchone()['status'] == 'draft'
        cur.execute('SELECT status FROM source_batches WHERE batch_id=%s', (second['batch_id'],))
        assert cur.fetchone()['status'] == 'draft'
