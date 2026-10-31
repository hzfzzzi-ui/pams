"""Isolated tests of draft projection; never read a real account or import."""
# ruff: noqa: F811
from hashlib import sha256
from uuid import uuid4

import pytest
from psycopg2 import errors

from tests.database.test_unified_source_staging import isolated_database  # noqa: F401
from domain.imports.source_import_adapter import stage_legacy_draft


def sample():
    batch_id, file_id, row_id = [str(uuid4()) for _ in range(3)]
    content = b'synthetic,80.00\n'
    digest = sha256(content).hexdigest()
    batch = {'batch_id': batch_id, 'account_code': 'synthetic-a', 'mode': 'upload',
             'kind': 'cash', 'status': 'draft', 'revision': 7, 'completeness': 'partial',
             'rows': [{'row_id': row_id, 'fields': {'amount': '80.00'},
                       'source': {'file_id': file_id, 'file_sha256': digest, 'origin': 'file',
                                  'row': 2, 'raw_text': 'synthetic,80.00'},
                       'issues': [{'code': 'review'}], 'acknowledged': True, 'excluded': False}]}
    file = {'file_id': file_id, 'batch_id': batch_id, 'filename': 'fixture.csv',
            'sha256': digest, 'content_type': 'text/csv', 'content': content,
            'metadata': {'mapping': {'amount': 1}}}
    return batch, file


def test_draft_projection_preserves_evidence_without_approving(isolated_database):
    conn = isolated_database
    batch, file = sample()
    with conn.cursor() as cur:
        stage_legacy_draft(cur, batch, [file], institution='synthetic-bank')
        cur.execute('SELECT account_code,business_type,institution,revision,completeness FROM source_batches')
        assert cur.fetchone() == ('synthetic-a', 'cash', 'synthetic-bank', 7, 'partial')
        cur.execute('SELECT raw_fields,parsed_fields,review_status FROM source_items')
        raw, candidate, status = cur.fetchone()
        assert raw['source']['raw_text'] == 'synthetic,80.00'
        assert raw['legacy_file_metadata']['mapping']['amount'] == 1
        assert candidate['fields']['amount'] == '80.00'
        assert candidate['issues'] == [{'code': 'review'}]
        assert candidate['acknowledged'] is True
        assert status == 'pending'
        cur.execute('SELECT content,sha256 FROM source_files')
        content, digest = cur.fetchone()
        assert bytes(content) == file['content'] and digest == file['sha256']
    conn.rollback()


def test_bad_evidence_rejected_before_writes(isolated_database):
    conn = isolated_database
    batch, file = sample()
    file['content'] = b'tampered'
    with conn.cursor() as cur:
        with pytest.raises(ValueError, match='hash mismatch'):
            stage_legacy_draft(cur, batch, [file], institution='synthetic-bank')
        cur.execute('SELECT count(*) FROM source_batches')
        assert cur.fetchone()[0] == 0
    conn.rollback()


def test_manual_and_excluded_items_keep_user_decisions(isolated_database):
    conn = isolated_database
    batch_id, first_id, excluded_id = [str(uuid4()) for _ in range(3)]
    batch = {'batch_id': batch_id, 'account_code': 'synthetic-a', 'mode': 'manual',
             'kind': 'trade', 'status': 'draft', 'revision': 3, 'completeness': 'partial',
             'rows': [{'row_id': first_id, 'fields': {'quantity': '1.25'}, 'source': {'origin': 'manual'},
                       'acknowledged': False, 'excluded': False},
                      {'row_id': excluded_id, 'fields': {'quantity': '2.00'}, 'source': {'origin': 'manual'},
                       'acknowledged': True, 'excluded': True}]}
    with conn.cursor() as cur:
        stage_legacy_draft(cur, batch, [], institution='synthetic-broker')
        cur.execute('SELECT source_item_id,raw_fields,parsed_fields,review_status FROM source_items')
        rows = {str(item): (raw, parsed, status) for item, raw, parsed, status in cur.fetchall()}
        assert rows[first_id][0]['manual_draft_fields']['quantity'] == '1.25'
        assert rows[first_id][2] == 'pending'
        assert rows[excluded_id][1]['acknowledged'] is True
        assert rows[excluded_id][1]['excluded'] is True and rows[excluded_id][2] == 'excluded'
    conn.rollback()


def test_duplicate_projection_rolls_back_entire_transaction(isolated_database):
    conn = isolated_database
    batch, file = sample()
    with conn.cursor() as cur:
        stage_legacy_draft(cur, batch, [file], institution='synthetic-bank')
        with pytest.raises(errors.UniqueViolation):
            stage_legacy_draft(cur, batch, [file], institution='synthetic-bank')
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute('SELECT count(*) FROM source_batches')
        assert cur.fetchone()[0] == 0
