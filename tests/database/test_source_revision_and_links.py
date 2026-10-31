"""Isolated source revision and published fact association contracts."""
# ruff: noqa: F811
import os
from pathlib import Path
from threading import Event, Thread
from uuid import uuid4

import psycopg2
import pytest
from psycopg2 import errors

from tests.database.test_unified_source_staging import isolated_database  # noqa: F401


def test_child_inserts_and_deletes_advance_revision_atomically(isolated_database):
    conn = isolated_database
    batch, file_id, item = (str(uuid4()) for _ in range(3))
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO source_batches(batch_id,account_code,business_type,institution,acquisition_method)
                       VALUES (%s,'synthetic-a','cash','synthetic','manual')""", (batch,))
        cur.execute("""INSERT INTO source_files(file_id,batch_id,filename,file_format,content_type,sha256,content)
                       VALUES (%s,%s,'a.csv','csv','text/csv',%s,%s)""", (file_id, batch, 'a' * 64, b'a'))
        cur.execute("""INSERT INTO source_items(source_item_id,batch_id,source_uid,parse_method,record_hash)
                       VALUES (%s,%s,'row','manual',%s)""", (item, batch, 'b' * 64))
        cur.execute('SELECT revision FROM source_batches WHERE batch_id=%s', (batch,))
        assert cur.fetchone()[0] == 3
        cur.execute('SAVEPOINT attempt')
        with pytest.raises(errors.UniqueViolation):
            cur.execute("""INSERT INTO source_items(source_item_id,batch_id,source_uid,parse_method,record_hash)
                           VALUES (%s,%s,'row','manual',%s)""", (str(uuid4()), batch, 'c' * 64))
        cur.execute('ROLLBACK TO SAVEPOINT attempt')
        cur.execute('SELECT revision FROM source_batches WHERE batch_id=%s', (batch,))
        assert cur.fetchone()[0] == 3
        cur.execute('DELETE FROM source_items WHERE source_item_id=%s', (item,))
        cur.execute('DELETE FROM source_files WHERE file_id=%s', (file_id,))
        cur.execute('SELECT revision FROM source_batches WHERE batch_id=%s', (batch,))
        assert cur.fetchone()[0] == 5


@pytest.mark.parametrize('table', ['pams_cashflow_fact', 'pams_investment_trade_fact', 'pams_holdings_snapshot'])
def test_published_associations_are_immutable_without_freezing_values(isolated_database, table):
    conn = isolated_database
    batch, item, another = (str(uuid4()) for _ in range(3))
    schema = Path(__file__).resolve().parents[2] / 'database/postgres/schema/081_unified_source_fact_links.sql'
    with conn.cursor() as cur:
        for name in ('pams_cashflow_fact', 'pams_investment_trade_fact', 'pams_holdings_snapshot'):
            cur.execute(f'CREATE TABLE {name} (id integer PRIMARY KEY, value integer NOT NULL)')
        cur.execute(schema.read_text(encoding='utf-8'))
        cur.execute("""INSERT INTO source_batches(batch_id,account_code,business_type,institution,acquisition_method)
                       VALUES (%s,'synthetic-a','cash','synthetic','manual')""", (batch,))
        cur.execute("""INSERT INTO source_items(source_item_id,batch_id,source_uid,parse_method,record_hash)
                       VALUES (%s,%s,'first','manual',%s),(%s,%s,'second','manual',%s)""",
                    (item, batch, 'a' * 64, another, batch, 'b' * 64))
        cur.execute(f'INSERT INTO {table}(id,value,source_item_id) VALUES (1,10,%s),(2,20,NULL)', (item,))
        cur.execute("UPDATE source_batches SET status='published',published_source_hash=%s WHERE batch_id=%s",
                    ('c' * 64, batch))
    conn.commit()

    for query, args in [
        (f'UPDATE {table} SET source_item_id=NULL WHERE id=1', ()),
        (f'UPDATE {table} SET source_item_id=%s WHERE id=1', (another,)),
        (f'UPDATE {table} SET source_item_id=%s WHERE id=2', (item,)),
        (f'INSERT INTO {table}(id,value,source_item_id) VALUES (3,30,%s)', (item,)),
        (f'DELETE FROM {table} WHERE id=1', ()),
    ]:
        with pytest.raises(errors.CheckViolation), conn.cursor() as cur:
            cur.execute(query, args)
        conn.rollback()

    with conn.cursor() as cur:
        cur.execute(f'UPDATE {table} SET value=11 WHERE id=1')
        cur.execute(f'DELETE FROM {table} WHERE id=2')
        cur.execute(f'SELECT id,value,source_item_id FROM {table}')
        assert cur.fetchall() == [(1, 11, item)]


def test_publication_serializes_concurrent_association_change(isolated_database):
    conn = isolated_database
    batch, item = str(uuid4()), str(uuid4())
    schema = Path(__file__).resolve().parents[2] / 'database/postgres/schema/081_unified_source_fact_links.sql'
    with conn.cursor() as cur:
        for name in ('pams_cashflow_fact', 'pams_investment_trade_fact', 'pams_holdings_snapshot'):
            cur.execute(f'CREATE TABLE {name} (id integer PRIMARY KEY)')
        cur.execute(schema.read_text(encoding='utf-8'))
        cur.execute("""INSERT INTO source_batches(batch_id,account_code,business_type,institution,acquisition_method)
                       VALUES (%s,'synthetic-a','cash','synthetic','manual')""", (batch,))
        cur.execute("""INSERT INTO source_items(source_item_id,batch_id,source_uid,parse_method,record_hash)
                       VALUES (%s,%s,'row','manual',%s)""", (item, batch, 'a' * 64))
        cur.execute('INSERT INTO pams_cashflow_fact(id,source_item_id) VALUES (1,%s)', (item,))
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("UPDATE source_batches SET status='published',published_source_hash=%s WHERE batch_id=%s",
                    ('c' * 64, batch))

    started, finished = Event(), Event()
    outcomes = []

    def change_link():
        other = None
        try:
            other = psycopg2.connect(os.environ['PAMS_SCHEMA_TEST_POSTGRES_DSN'],
                                     dbname=conn.get_dsn_parameters()['dbname'])
            with other, other.cursor() as cur:
                cur.execute("SET LOCAL lock_timeout='5s'")
                started.set()
                cur.execute('UPDATE pams_cashflow_fact SET source_item_id=NULL WHERE id=1')
                outcomes.append('updated')
        except psycopg2.Error as exc:
            outcomes.append(exc)
        finally:
            if other is not None:
                other.close()
            finished.set()

    thread = Thread(target=change_link, daemon=True)
    thread.start()
    try:
        assert started.wait(5)
        assert not finished.wait(0.2)
    finally:
        conn.commit()
        thread.join(6)
    assert finished.is_set()
    assert len(outcomes) == 1 and isinstance(outcomes[0], errors.CheckViolation)
    with conn.cursor() as cur:
        cur.execute('SELECT source_item_id FROM pams_cashflow_fact WHERE id=1')
        assert cur.fetchone()[0] == item
