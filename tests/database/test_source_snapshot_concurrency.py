"""Verify source review locks on two isolated PostgreSQL connections."""
# ruff: noqa: F811
from concurrent.futures import ThreadPoolExecutor
import os
from uuid import uuid4

import psycopg2

from tests.database.test_source_schema_contract import isolated_database  # noqa: F401
from domain.imports.unified_source import add_item, attach_file, create_batch, lock_source_review, review_item


def test_source_review_lock_blocks_concurrent_review(isolated_database):
    conn = isolated_database
    batch_id, item_id, file_id, spare_file_id = uuid4(), uuid4(), uuid4(), uuid4()
    with conn.cursor() as cur:
        create_batch(cur, batch_id=batch_id, account_code='synthetic-a', business_type='cash',
                     institution='synthetic-bank', acquisition_method='upload')
        attach_file(cur, file_id=file_id, batch_id=batch_id, filename='fixture.png',
                    file_format='png', content_type='image/png', content=b'synthetic')
        attach_file(cur, file_id=spare_file_id, batch_id=batch_id, filename='spare.png',
                    file_format='png', content_type='image/png', content=b'spare')
        add_item(cur, item_id=item_id, batch_id=batch_id, source_uid='row-1', parse_method='manual',
                 file_id=file_id, record_hash='1' * 64, raw_fields={'amount': '10'})
    conn.commit()
    with conn.cursor() as cur:
        lock_source_review(cur, batch_id=batch_id, expected_revision=4)

    def concurrent_action(action):
        other = psycopg2.connect(os.environ['PAMS_SCHEMA_TEST_POSTGRES_DSN'],
                                 dbname=conn.info.dbname, connect_timeout=5)
        try:
            with other.cursor() as cur:
                cur.execute("SET LOCAL lock_timeout='400ms'")
                try:
                    if action == 'review':
                        review_item(cur, batch_id=batch_id, item_id=item_id, expected_revision=4,
                                    parsed_fields={'amount': '20'}, review_status='confirmed')
                    elif action == 'update':
                        cur.execute("UPDATE source_items SET parsed_fields='{}'::jsonb WHERE source_item_id=%s", (str(item_id),))
                    elif action == 'delete':
                        cur.execute('DELETE FROM source_files WHERE file_id=%s', (str(spare_file_id),))
                    else:
                        add_item(cur, item_id=uuid4(), batch_id=batch_id, source_uid='new-row',
                                 parse_method='manual', record_hash='2' * 64)
                except psycopg2.errors.LockNotAvailable:
                    other.rollback()
                    return 'blocked'
                other.rollback()
                return 'unexpected-write'
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=1) as executor:
        for action in ('review', 'update', 'delete', 'insert'):
            assert executor.submit(concurrent_action, action).result(timeout=5) == 'blocked', action
    conn.rollback()
    with conn.cursor() as cur:
        assert review_item(cur, batch_id=batch_id, item_id=item_id, expected_revision=4,
                           parsed_fields={'amount': '20'}, review_status='confirmed') == 5
        cur.execute('DELETE FROM source_files WHERE file_id=%s', (str(spare_file_id),))
        assert cur.rowcount == 1
        add_item(cur, item_id=uuid4(), batch_id=batch_id, source_uid='new-row',
                 parse_method='manual', record_hash='2' * 64)
