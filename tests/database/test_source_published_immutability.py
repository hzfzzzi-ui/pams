"""Published source evidence cannot be reopened, expanded, or rewritten."""
# ruff: noqa: F811
from uuid import uuid4

from psycopg2 import errors
import pytest

from tests.database.test_unified_source_staging import isolated_database  # noqa: F401


def test_published_source_blocks_direct_sql_mutation(isolated_database):
    conn = isolated_database
    batch_id, item_id, file_id = str(uuid4()), str(uuid4()), str(uuid4())
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO source_batches(batch_id, account_code, business_type, institution, acquisition_method)
                       VALUES (%s,'synthetic-a','cash','synthetic-bank','manual')""", (batch_id,))
        cur.execute("""INSERT INTO source_items(source_item_id,batch_id,source_uid,parse_method,record_hash)
                       VALUES (%s,%s,'row-1','manual',%s)""", (item_id, batch_id, 'a' * 64))
        cur.execute("""INSERT INTO source_files(file_id,batch_id,filename,file_format,content_type,sha256,content)
                       VALUES (%s,%s,'original.csv','csv','text/csv',%s,%s)""",
                    (file_id, batch_id, 'f' * 64, b'synthetic'))
        cur.execute("UPDATE source_batches SET status='published', published_source_hash=%s WHERE batch_id=%s",
                    ('b' * 64, batch_id))
    conn.commit()

    operations = [
        ("UPDATE source_batches SET status='draft' WHERE batch_id=%s", (batch_id,)),
        ("UPDATE source_batches SET published_source_hash=%s WHERE batch_id=%s", ('c' * 64, batch_id)),
        ("UPDATE source_batches SET account_code='synthetic-b' WHERE batch_id=%s", (batch_id,)),
        ("UPDATE source_items SET parsed_fields='{}'::jsonb WHERE source_item_id=%s", (item_id,)),
        ("DELETE FROM source_items WHERE source_item_id=%s", (item_id,)),
        ("DELETE FROM source_files WHERE file_id=%s", (file_id,)),
        ("""INSERT INTO source_items(source_item_id,batch_id,source_uid,parse_method,record_hash)
            VALUES (%s,%s,'late','manual',%s)""", (str(uuid4()), batch_id, 'd' * 64)),
        ("""INSERT INTO source_files(file_id,batch_id,filename,file_format,content_type,sha256,content)
            VALUES (%s,%s,'late.csv','csv','text/csv',%s,%s)""", (str(uuid4()), batch_id, 'e' * 64, b'synthetic')),
    ]
    for statement, args in operations:
        with pytest.raises(errors.CheckViolation):
            with conn.cursor() as cur:
                cur.execute(statement, args)
        conn.rollback()

    with conn.cursor() as cur:
        cur.execute('SELECT status,published_source_hash,account_code FROM source_batches WHERE batch_id=%s', (batch_id,))
        assert cur.fetchone() == ('published', 'b' * 64, 'synthetic-a')
        cur.execute('SELECT count(*) FROM source_items WHERE batch_id=%s', (batch_id,))
        assert cur.fetchone()[0] == 1
