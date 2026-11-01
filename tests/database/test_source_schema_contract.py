"""Verify the formal source schema in a newly created disposable database."""
import os
from hashlib import sha256
from pathlib import Path
import sys
from uuid import uuid4

import psycopg2
from psycopg2 import errors, sql
from psycopg2.extensions import parse_dsn
import pytest


SCHEMA = Path(__file__).resolve().parents[2] / 'database/postgres/schema/074_unified_source.sql'
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend/api'))
from domain.imports.unified_source import add_item, attach_file, create_batch, review_item, lock_source_review


@pytest.fixture
def isolated_database():
    dsn = os.environ.get('PAMS_SCHEMA_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('Explicit disposable PostgreSQL DSN required')
    params = parse_dsn(dsn)
    assert params.get('host') in {'127.0.0.1', 'localhost', '::1'}
    admin = psycopg2.connect(dsn, connect_timeout=5)
    admin.autocommit = True
    assert admin.server_version // 10000 == 16
    with admin.cursor() as cur:
        cur.execute("SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = current_database()")
        assert cur.fetchone()[0] == 'pams-disposable-schema-test'
    name = 'pams_import_test_' + uuid4().hex
    with admin.cursor() as cur:
        cur.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        cur.execute(sql.SQL("COMMENT ON DATABASE {} IS 'pams-isolated-account-import'").format(sql.Identifier(name)))
    conn = None
    try:
        conn = psycopg2.connect(**{**params, 'dbname': name})
        with conn.cursor() as cur:
            cur.execute('CREATE TABLE public.pams_accounts (account_code varchar(50) PRIMARY KEY)')
            cur.execute("INSERT INTO public.pams_accounts VALUES ('synthetic-a'), ('synthetic-b')")
            cur.execute(SCHEMA.read_text(encoding='utf-8'))
        conn.commit()
        yield conn
    finally:
        if conn is not None:
            conn.close()
        with admin.cursor() as cur:
            cur.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))
        admin.close()


def test_batch_file_item_constraints_and_rollback(isolated_database):
    conn = isolated_database
    batch_a, batch_b, file_a, item_a = [str(uuid4()) for _ in range(4)]
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO source_batches(batch_id,account_code,business_type,institution,acquisition_method)
                       VALUES (%s,'synthetic-a','cash','synthetic-bank','upload'),
                              (%s,'synthetic-b','trade','synthetic-broker','manual')""", (batch_a, batch_b))
        cur.execute("""INSERT INTO source_files(file_id,batch_id,filename,file_format,content_type,sha256,content)
                       VALUES (%s,%s,'sample.csv','csv','text/csv',%s,%s)""", (file_a, batch_a, 'a' * 64, b'fixture'))
        cur.execute("""INSERT INTO source_items(source_item_id,batch_id,file_id,source_uid,parse_method,record_hash)
                       VALUES (%s,%s,%s,'row-1','template',%s)""", (item_a, batch_a, file_a, 'b' * 64))
    conn.commit()

    with pytest.raises(errors.ForeignKeyViolation):
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO source_items(source_item_id,batch_id,file_id,source_uid,parse_method,record_hash)
                           VALUES (%s,%s,%s,'cross-batch','ocr',%s)""", (str(uuid4()), batch_b, file_a, 'c' * 64))
    conn.rollback()

    with pytest.raises(errors.UniqueViolation):
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO source_items(source_item_id,batch_id,source_uid,parse_method,record_hash)
                           VALUES (%s,%s,'row-1','manual',%s)""", (str(uuid4()), batch_a, 'd' * 64))
    conn.rollback()

    with pytest.raises(errors.CheckViolation):
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO source_items(source_item_id,batch_id,source_uid,parse_method,record_hash)
                           VALUES (%s,%s,'no-file','ocr',%s)""", (str(uuid4()), batch_a, 'e' * 64))
    conn.rollback()

    with conn.cursor() as cur:
        cur.execute('SELECT count(*) FROM source_items WHERE batch_id = %s', (batch_a,))
        assert cur.fetchone()[0] == 1
        cur.execute("""INSERT INTO source_items(source_item_id,batch_id,source_uid,parse_method,record_hash)
                       VALUES (%s,%s,'manual-1','manual',%s)""", (str(uuid4()), batch_b, 'f' * 64))
        cur.execute('SELECT count(*) FROM source_items WHERE batch_id = %s', (batch_b,))
        assert cur.fetchone()[0] == 1


def test_unknown_account_and_reversed_dates_rejected(isolated_database):
    conn = isolated_database
    with pytest.raises(errors.ForeignKeyViolation):
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO source_batches(batch_id,account_code,business_type,institution,acquisition_method)
                           VALUES (%s,'unknown','cash','synthetic-bank','upload')""", (str(uuid4()),))
    conn.rollback()
    with pytest.raises(errors.CheckViolation):
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO source_batches(batch_id,account_code,business_type,institution,acquisition_method,date_from,date_to)
                           VALUES (%s,'synthetic-a','cash','synthetic-bank','upload','2026-02-01','2026-01-01')""", (str(uuid4()),))
    conn.rollback()


def test_repository_keeps_raw_and_corrected_values_and_is_atomic(isolated_database):
    conn = isolated_database
    batch, item, file_id = uuid4(), uuid4(), uuid4()
    with conn.cursor() as cur:
        create_batch(cur, batch_id=batch, account_code='synthetic-a', business_type='cash',
                     institution='synthetic-bank', acquisition_method='upload')
        digest = attach_file(cur, file_id=file_id, batch_id=batch, filename='fixture.png',
                             file_format='png', content_type='image/png', content=b'synthetic')
        assert digest == sha256(b'synthetic').hexdigest()
        add_item(cur, item_id=item, batch_id=batch, source_uid='row-1', parse_method='ocr',
                 record_hash='1' * 64, file_id=file_id, location='image:1',
                 raw_fields={'amount': '800.00'}, parsed_fields={'amount': '80.00'})
        cur.execute('SELECT raw_fields, parsed_fields, location FROM source_items WHERE source_item_id = %s', (str(item),))
        raw, parsed, location = cur.fetchone()
        assert raw['amount'] == '800.00' and parsed['amount'] == '80.00' and location == 'image:1'
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute('SELECT count(*) FROM source_batches WHERE batch_id = %s', (str(batch),))
        assert cur.fetchone()[0] == 0


def test_original_evidence_is_immutable_but_candidate_is_editable(isolated_database):
    conn = isolated_database
    batch, item, file_id = uuid4(), uuid4(), uuid4()
    with conn.cursor() as cur:
        create_batch(cur, batch_id=batch, account_code='synthetic-a', business_type='cash',
                     institution='synthetic-bank', acquisition_method='upload')
        attach_file(cur, file_id=file_id, batch_id=batch, filename='x.png', file_format='png',
                    content_type='image/png', content=b'original')
        add_item(cur, item_id=item, batch_id=batch, source_uid='row-1', parse_method='ocr',
                 record_hash='1' * 64, file_id=file_id, raw_fields={'amount': '800'})
    conn.commit()
    with pytest.raises(errors.CheckViolation):
        with conn.cursor() as cur:
            cur.execute("UPDATE source_items SET raw_fields = '{\"amount\":\"80\"}'::jsonb WHERE source_item_id=%s", (str(item),))
    conn.rollback()
    with pytest.raises(errors.CheckViolation):
        with conn.cursor() as cur:
            cur.execute("UPDATE source_files SET content=%s WHERE file_id=%s", (b'changed', str(file_id)))
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute("UPDATE source_items SET parsed_fields = '{\"amount\":\"80\"}'::jsonb, review_status='confirmed' WHERE source_item_id=%s", (str(item),))
        cur.execute('SELECT raw_fields, parsed_fields, review_status FROM source_items WHERE source_item_id=%s', (str(item),))
        raw, parsed, status = cur.fetchone()
        assert raw['amount'] == '800' and parsed['amount'] == '80' and status == 'confirmed'


def test_candidate_review_rejects_stale_or_published_batch(isolated_database):
    conn = isolated_database
    batch, item = uuid4(), uuid4()
    with conn.cursor() as cur:
        create_batch(cur, batch_id=batch, account_code='synthetic-a', business_type='cash',
                     institution='synthetic-bank', acquisition_method='manual')
        add_item(cur, item_id=item, batch_id=batch, source_uid='row-1', parse_method='manual',
                 record_hash='1' * 64, raw_fields={'amount': '800'})
        assert review_item(cur, batch_id=batch, item_id=item, expected_revision=2,
                           parsed_fields={'amount': '80'}, review_status='confirmed', review_note='checked') == 3
        with pytest.raises(ValueError):
            review_item(cur, batch_id=batch, item_id=item, expected_revision=1,
                        parsed_fields={'amount': '1'}, review_status='confirmed')
        cur.execute('SELECT raw_fields, parsed_fields, review_note FROM source_items WHERE source_item_id=%s', (str(item),))
        assert cur.fetchone() == ({'amount': '800'}, {'amount': '80'}, 'checked')
        cur.execute("UPDATE source_batches SET status='published', published_source_hash=%s WHERE batch_id=%s", ('a' * 64, str(batch)))
        with pytest.raises(ValueError):
            review_item(cur, batch_id=batch, item_id=item, expected_revision=3,
                        parsed_fields={'amount': '2'}, review_status='confirmed')
        cur.execute('SELECT revision FROM source_batches WHERE batch_id=%s', (str(batch),))
        assert cur.fetchone()[0] == 3


def test_direct_candidate_edit_advances_revision_and_published_is_read_only(isolated_database):
    conn = isolated_database
    batch, item = uuid4(), uuid4()
    with conn.cursor() as cur:
        create_batch(cur, batch_id=batch, account_code='synthetic-a', business_type='cash',
                     institution='synthetic-bank', acquisition_method='manual')
        add_item(cur, item_id=item, batch_id=batch, source_uid='row-1', parse_method='manual',
                 record_hash='1' * 64, raw_fields={'amount': '800'})
        cur.execute("UPDATE source_items SET parsed_fields='{}'::jsonb WHERE source_item_id=%s", (str(item),))
        cur.execute('SELECT revision FROM source_batches WHERE batch_id=%s', (str(batch),))
        assert cur.fetchone()[0] == 3
        with pytest.raises(ValueError):
            review_item(cur, batch_id=batch, item_id=item, expected_revision=1,
                        parsed_fields={'amount': '80'}, review_status='confirmed')
        cur.execute("UPDATE source_batches SET status='published', published_source_hash=%s WHERE batch_id=%s", ('a' * 64, str(batch)))
    conn.commit()
    with pytest.raises(errors.CheckViolation):
        with conn.cursor() as cur:
            cur.execute("UPDATE source_items SET review_note='changed' WHERE source_item_id=%s", (str(item),))
    conn.rollback()


def test_locked_review_reports_pending_and_binds_revision(isolated_database):
    conn = isolated_database
    batch, item = uuid4(), uuid4()
    with conn.cursor() as cur:
        create_batch(cur, batch_id=batch, account_code='synthetic-a', business_type='cash',
                     institution='synthetic-bank', acquisition_method='manual')
        add_item(cur, item_id=item, batch_id=batch, source_uid='row-1', parse_method='manual',
                 record_hash='1' * 64, raw_fields={'amount': '800'})
        pending = lock_source_review(cur, batch_id=batch, expected_revision=2)
        assert 'pending_items' in pending['snapshot']['blockers']
        review_item(cur, batch_id=batch, item_id=item, expected_revision=2,
                    parsed_fields={'amount': '80'}, review_status='confirmed')
        ready = lock_source_review(cur, batch_id=batch, expected_revision=3)
        assert ready['snapshot']['blockers'] == []
        assert ready['source_snapshot_sha256'] != pending['source_snapshot_sha256']
        with pytest.raises(ValueError):
            lock_source_review(cur, batch_id=batch, expected_revision=1)


def test_locked_review_detects_inconsistent_file_hash(isolated_database):
    conn = isolated_database
    batch, item, file_id = uuid4(), uuid4(), uuid4()
    with conn.cursor() as cur:
        create_batch(cur, batch_id=batch, account_code='synthetic-a', business_type='cash',
                     institution='synthetic-bank', acquisition_method='upload')
        cur.execute("""INSERT INTO source_files(file_id,batch_id,filename,file_format,content_type,sha256,content)
                       VALUES (%s,%s,'x.png','png','image/png',%s,%s)""",
                    (str(file_id), str(batch), 'a' * 64, b'synthetic'))
        add_item(cur, item_id=item, batch_id=batch, source_uid='row-1', parse_method='ocr',
                 record_hash='1' * 64, file_id=file_id, raw_fields={'amount': '80'})
        review_item(cur, batch_id=batch, item_id=item, expected_revision=3,
                    parsed_fields={'amount': '80'}, review_status='confirmed')
        result = lock_source_review(cur, batch_id=batch, expected_revision=4)
        assert 'file_hash_mismatch' in result['snapshot']['blockers']
