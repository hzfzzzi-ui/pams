"""Published source replay must verify financial facts in a disposable database."""
# ruff: noqa: F811
import threading
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

from tests.imports.test_account_import_publish import client, draft, preview, publish, sql, trade  # noqa: F401
from tests.database.test_native_manual_source import create_reviewed, isolated, publish as publish_native  # noqa: F401


@pytest.mark.parametrize('column,value', [
    ('net_cash', '-1004'), ('currency', 'USD'), ('fx_rate_to_cny', '2'),
    ('source_hash', '0' * 64),
])
def test_replay_refuses_changed_trade_fact(client, column, value):
    web, params = client
    batch = draft(web, [trade()])
    check = preview(web, batch)
    assert publish(web, batch, check).status_code == 200
    identifier = sql(params, 'SELECT trade_id FROM pams_investment_trade_fact')[0]['trade_id']
    sql(params, f'UPDATE pams_investment_trade_fact SET {column}=%s WHERE trade_id=%s', (value, identifier))
    response = publish(web, batch, check)
    assert response.status_code == 409, response.text
    assert response.json()['detail']['code'] == 'source_evidence_changed'
    actual = sql(params, f'SELECT {column} AS value FROM pams_investment_trade_fact WHERE trade_id=%s', (identifier,))[0]['value']
    assert actual == (Decimal(value) if column in {'net_cash', 'fx_rate_to_cny'} else value)


def test_replay_refuses_changed_derived_cash_leg(client):
    web, params = client
    from tests.imports.test_account_import_publish import DAY
    fields = {'kind': 'cash', 'business_date': DAY, 'currency': 'CNY', 'action': 'deposit',
              'net_cash': '500', 'counter_account_code': 'bank_test'}
    batch = draft(web, [fields])
    check = preview(web, batch)
    assert publish(web, batch, check).status_code == 200
    assert publish(web, batch, check).json()['replayed']
    sql(params, 'UPDATE pams_cashflow_fact SET amount=-501')
    response = publish(web, batch, check)
    assert response.status_code == 409, response.text
    assert sql(params, 'SELECT amount FROM pams_cashflow_fact')[0]['amount'] == -501


def test_replay_rechecks_after_savepoint_rollback(isolated):
    batch, _, revision, check = create_reviewed(isolated, trade())
    with psycopg2.connect(**isolated) as conn:
        conn.set_session(isolation_level='SERIALIZABLE')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            assert not publish_native(cur, batch, revision, check)['replayed']
    with psycopg2.connect(**isolated) as conn:
        conn.set_session(isolation_level='SERIALIZABLE')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SAVEPOINT audit_tamper')
            cur.execute('UPDATE pams_investment_trade_fact SET net_cash=-1004')
            with pytest.raises(ValueError, match='published financial fact evidence differs'):
                publish_native(cur, batch, revision, check)
            cur.execute('ROLLBACK TO SAVEPOINT audit_tamper')
            assert publish_native(cur, batch, revision, check)['replayed']
            cur.execute('SELECT net_cash FROM pams_investment_trade_fact')
            assert cur.fetchone()['net_cash'] == -1005


def test_replay_locks_facts_against_concurrent_direct_writes(isolated):
    batch, _, revision, check = create_reviewed(isolated, trade())
    with psycopg2.connect(**isolated) as conn:
        conn.set_session(isolation_level='SERIALIZABLE')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            assert not publish_native(cur, batch, revision, check)['replayed']
    writer_started = threading.Event()
    def writer():
        with psycopg2.connect(**isolated) as conn, conn.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout='250ms'")
            writer_started.set()
            with pytest.raises(psycopg2.errors.LockNotAvailable):
                cur.execute('UPDATE pams_investment_trade_fact SET net_cash=-1004')
    with psycopg2.connect(**isolated) as conn:
        conn.set_session(isolation_level='SERIALIZABLE')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            assert publish_native(cur, batch, revision, check)['replayed']
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(writer)
                assert writer_started.wait(5)
                future.result(timeout=5)
    with psycopg2.connect(**isolated) as conn, conn.cursor() as cur:
        cur.execute('SELECT net_cash FROM pams_investment_trade_fact')
        assert cur.fetchone()[0] == -1005


def test_replay_refuses_missing_fact_link_even_if_storage_is_corrupted(client):
    web, params = client
    batch = draft(web, [trade()])
    check = preview(web, batch)
    assert publish(web, batch, check).status_code == 200
    with psycopg2.connect(**params) as conn, conn.cursor() as cur:
        cur.execute('ALTER TABLE pams_investment_trade_fact DISABLE TRIGGER trade_source_fact_link_guard')
        cur.execute('UPDATE pams_investment_trade_fact SET source_item_id=NULL')
        cur.execute('SET CONSTRAINTS ALL IMMEDIATE')
        cur.execute('ALTER TABLE pams_investment_trade_fact ENABLE TRIGGER trade_source_fact_link_guard')
    response = publish(web, batch, check)
    assert response.status_code == 409, response.text
    assert sql(params, 'SELECT source_item_id FROM pams_investment_trade_fact')[0]['source_item_id'] is None


def test_replay_refuses_damaged_uploaded_original(client, monkeypatch):
    from domain.imports import account_import_statement as statement
    web, params = client
    monkeypatch.setattr(statement, 'config', lambda: {'profiles': {'test': {'account_code': 'bank_test',
        'profile_code': 'test', 'statement_template': 'manual_statement_csv', 'currency': 'CNY'}},
        'rules': [], 'amount_rules': []})
    response = web.post('/api/account-import/batches', json={'account_code':'bank_test','mode':'upload','kind':'cash'})
    assert response.status_code == 200, response.text
    batch = response.json()
    path = '/api/account-import/batches/' + batch['batch_id']
    original = b'transaction_date,amount,currency,balance,summary\n2026-10-29,100.05,CNY,200.05,pay\n'
    uploaded = web.post(path + '/files?filename=bank.csv', content=original)
    assert uploaded.status_code == 200, uploaded.text
    batch = uploaded.json()
    response = web.put(path, json={'revision':batch['revision'], 'rows':batch['rows']})
    assert response.status_code == 200, response.text
    batch = response.json()
    check = preview(web, batch)
    assert publish(web, batch, check).status_code == 200
    with psycopg2.connect(**params) as conn, conn.cursor() as cur:
        cur.execute('ALTER TABLE source_files DISABLE TRIGGER source_files_evidence_immutable')
        cur.execute("UPDATE source_files SET content='corrupted synthetic content'::bytea")
        cur.execute('ALTER TABLE source_files ENABLE TRIGGER source_files_evidence_immutable')
    response = publish(web, batch, check)
    assert response.status_code == 409, response.text
    assert sql(params, 'SELECT amount FROM pams_cashflow_fact')[0]['amount'] == Decimal('100.05')


def test_replay_revalidates_a_batch_once_and_detects_later_fact_drift(client, monkeypatch):
    from domain.imports import source_fact_evidence as evidence
    web, params = client
    fields = [trade(source_ref='synthetic-' + str(i), quantity=str(100 + i),
                    gross_amount=str((100 + i) * 10), net_cash=str(-((100 + i) * 10 + 5)))
              for i in range(40)]
    batch = draft(web, fields)
    check = preview(web, batch)
    assert check['can_publish'], check
    assert publish(web, batch, check).status_code == 200
    calls = {'snapshots': 0}
    digest = evidence._digest
    def count_snapshot(value):
        if isinstance(value, dict) and set(value) == {'batch', 'files', 'items', 'blockers'}:
            calls['snapshots'] += 1
        return digest(value)
    monkeypatch.setattr(evidence, '_digest', count_snapshot)
    assert publish(web, batch, check).json()['replayed']
    assert calls['snapshots'] <= 2
    sql(params, 'UPDATE pams_investment_trade_fact SET net_cash=-1004 WHERE trade_id=(SELECT min(trade_id) FROM pams_investment_trade_fact)')
    response = publish(web, batch, check)
    assert response.status_code == 409, response.text


def test_replay_rechecks_original_source_after_savepoint_rollback(isolated):
    batch, _, revision, check = create_reviewed(isolated, trade())
    with psycopg2.connect(**isolated) as conn:
        conn.set_session(isolation_level='SERIALIZABLE')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            assert not publish_native(cur, batch, revision, check)['replayed']
    with psycopg2.connect(**isolated) as conn:
        conn.set_session(isolation_level='SERIALIZABLE')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('ALTER TABLE source_items DISABLE TRIGGER source_items_evidence_immutable')
            cur.execute('SAVEPOINT corrupt_source')
            cur.execute("UPDATE source_items SET raw_fields=jsonb_set(raw_fields,'{fields,net_cash}',to_jsonb('-1'::text))")
            with pytest.raises(ValueError, match='published source evidence is unavailable'):
                publish_native(cur, batch, revision, check)
            cur.execute('ROLLBACK TO SAVEPOINT corrupt_source')
            cur.execute('ALTER TABLE source_items ENABLE TRIGGER source_items_evidence_immutable')
            assert publish_native(cur, batch, revision, check)['replayed']
