# ruff: noqa: F811
from uuid import uuid4

import psycopg2
from psycopg2.extras import RealDictCursor

from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from scripts import bootstrap_isolated_sources as bootstrap
from domain.imports import source_records_v2 as source
from domain.investments.valuation_input_evidence import _bound_input


def test_new_source_item_is_bound_and_changes_invalidate_evidence(empty_source_db):
    source_id, item_id = uuid4(), uuid4()
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            source.create_source(cur, source_id=source_id, account_code='tonghuashun', source_name='synthetic',
                                 acquisition_method='manual', document_type='holding')
            source.add_item(cur, source_id=source_id, item_id=item_id, parse_method='manual',
                            raw_fields={'synthetic': 'holding'}, parsed_fields={'asset_type': 'cash'})
            before, raw = _bound_input(cur, 'source_item', {'source_item_id': str(item_id)}, snapshot_cache={})
            assert before['source_ref']['account_code'] == 'tonghuashun'
            assert before['snapshot']['business_type'] == 'holding'
            assert before['snapshot']['review_status'] == 'pending'
            assert before['snapshot']['source_integrity'] == 'verified'
            assert before['snapshot']['source_fields']['asset_type'] == 'cash'
            assert raw['item']['source_id'] == str(source_id)
            source.review_item(cur, source_id=source_id, item_id=item_id, expected_revision=2,
                               parsed_fields={'asset_type': 'cash'}, review_status='confirmed')
            after, _ = _bound_input(cur, 'source_item', {'source_item_id': str(item_id)}, snapshot_cache={})
            assert after['snapshot']['review_status'] == 'confirmed'
            assert before['snapshot']['content_hash'] != after['snapshot']['content_hash']
        conn.commit()
    with psycopg2.connect(**empty_source_db) as conn:
        conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            current, _ = _bound_input(cur, 'source_item', {'source_item_id': str(item_id)}, locking=False)
            assert current['snapshot']['content_hash'] == after['snapshot']['content_hash']


def test_new_file_source_integrity_detects_altered_item(empty_source_db):
    source_id, item_id = uuid4(), uuid4()
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            source.create_source(cur, source_id=source_id, account_code='tonghuashun', source_name='synthetic',
                acquisition_method='file', document_type='holding', filename='data.csv', file_format='csv', content=b'original')
            source.add_item(cur, source_id=source_id, item_id=item_id, parse_method='manual',
                            raw_fields={'row': 'original', 'quantity': 1.25}, parsed_fields={'asset_type': 'cash'})
            before, _ = _bound_input(cur, 'source_item', {'source_item_id': str(item_id)})
            assert before['snapshot']['source_integrity'] == 'verified'
            cur.execute('ALTER TABLE source_items DISABLE TRIGGER USER')
            cur.execute("UPDATE source_items SET raw_fields='{}'::jsonb WHERE source_item_id=%s", (str(item_id),))
            cur.execute('ALTER TABLE source_items ENABLE TRIGGER USER')
            after, _ = _bound_input(cur, 'source_item', {'source_item_id': str(item_id)})
            assert after['snapshot']['source_integrity'] == 'unknown'
