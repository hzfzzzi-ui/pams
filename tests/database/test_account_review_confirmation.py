"""Account review decisions in a marked disposable PostgreSQL database."""
# ruff: noqa: F811
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal

import psycopg2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg2.extras import RealDictCursor

from scripts.bootstrap_isolated_sources import initialize_isolated_sources
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401


def test_review_fingerprint_revocation_and_concurrent_decisions(empty_source_db, monkeypatch):
    from endpoints import accounts
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO pams_accounts(account_code,display_name) VALUES ('review_account','Synthetic')")
            cur.execute("SELECT to_regclass('public.pams_reconciliation_case')")
            assert cur.fetchone()[0] is None
    monkeypatch.setattr(accounts, 'get_conn', lambda: psycopg2.connect(**empty_source_db))
    value = {'amount': Decimal('0')}
    def overview(cur=None):
        if cur is None:
            with psycopg2.connect(**empty_source_db) as conn, conn.cursor(cursor_factory=RealDictCursor) as cursor:
                return overview(cursor)
        account = {'account_code':'review_account', 'status':'ready_for_review', 'base_currency':'CNY',
                   'primary_balance':{'amount':value['amount'], 'currency':'CNY', 'as_of':date(2026, 10, 1),
                                      'source':'manual'}}
        accounts._apply_review(account, accounts._fetch_latest_reviews(cur).get('review_account'))
        return {'accounts':[account]}
    monkeypatch.setattr(accounts, '_overview_payload', overview)
    app=FastAPI(); app.include_router(accounts.router, prefix='/api')
    with TestClient(app) as client:
        client.headers['Origin']='http://testserver'
        value['amount']=None
        assert client.post('/api/accounts/review_account/review', json={'action':'approve'}).status_code == 400
        value['amount']=Decimal('0')
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: client.post('/api/accounts/review_account/review',
                json={'action':'approve','note':'zero explicitly reviewed'}), range(2)))
        assert sorted(response.status_code for response in responses) == [200,400]
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM evidence_confirmation WHERE confirmation_type='account_review'")
            rows=cur.fetchall(); assert len(rows)==1
            assert rows[0]['evidence']['reviewed_balance']=='0' and rows[0]['note']=='zero explicitly reviewed'
            with pytest.raises(psycopg2.errors.RaiseException):
                cur.execute("UPDATE evidence_confirmation SET note='rewrite' WHERE confirmation_id=%s", (str(rows[0]['confirmation_id']),))
            conn.rollback()
        value['amount']=Decimal('1')
        assert overview()['accounts'][0]['review_status']=='stale'
        result=client.post('/api/accounts/review_account/review', json={'action':'revoke','note':'changed balance'})
        assert result.status_code == 200, result.text
        assert result.json()['accounts'][0]['review_status']=='unreviewed'
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM evidence_confirmation WHERE revoked_at IS NOT NULL")
            assert cur.fetchone()[0]==1
            cur.execute("SELECT note,revocation_note FROM evidence_confirmation WHERE revoked_at IS NOT NULL")
            assert cur.fetchone() == ('zero explicitly reviewed', 'changed balance')
            with pytest.raises(psycopg2.errors.RaiseException):
                cur.execute("UPDATE evidence_confirmation SET revocation_note='rewritten' WHERE revoked_at IS NOT NULL")
            conn.rollback()


def test_review_order_follows_account_lock_not_clock_or_transaction_start(empty_source_db):
    """An older transaction waiting for the account must become the newest decision."""
    from threading import Event
    from time import monotonic, sleep
    from uuid import uuid4
    from endpoints import accounts
    from psycopg2.extras import Json

    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO pams_accounts(account_code,display_name) VALUES ('a','A'),('b','B')")

    older = psycopg2.connect(**empty_source_db)
    holder = psycopg2.connect(**empty_source_db)
    started, finished = Event(), Event()
    def insert(cur, account, action, timestamp):
        cur.execute("""INSERT INTO evidence_confirmation
            (confirmation_id,confirmation_type,from_account_code,evidence,note,confirmed_at)
            VALUES (%s,'account_review',%s,%s,'test',%s) RETURNING confirmation_order""",
            (str(uuid4()), account, Json({'review_action':action,'source_status':'ready_for_review',
                 'review_fingerprint':'synthetic'}), timestamp))
        row = cur.fetchone()
        return row['confirmation_order'] if isinstance(row, dict) else row[0]
    try:
        with older.cursor() as cur:
            cur.execute("SELECT pg_backend_pid(), now()")  # Start before the lock holder.
            older_pid = cur.fetchone()[0]
        with holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended('account-import:a',0))")
        def wait_and_write():
            with older.cursor() as cur:
                started.set()
                cur.execute("SELECT pg_advisory_xact_lock(hashtextextended('account-import:a',0))")
                order = insert(cur, 'a', 'defer_missing', '2000-01-01 00:00:00+00')
            older.commit()
            finished.set()
            return order
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(wait_and_write)
            assert started.wait(5)
            deadline = monotonic() + 5
            with psycopg2.connect(**empty_source_db) as observer, observer.cursor() as cur:
                while monotonic() < deadline:
                    cur.execute("SELECT pg_stat_clear_snapshot()")
                    cur.execute("SELECT wait_event FROM pg_stat_activity WHERE pid=%s", (older_pid,))
                    if cur.fetchone()[0] == 'advisory':
                        break
                    sleep(.02)
                else:
                    pytest.fail('older transaction did not wait on the account lock')
            with holder.cursor() as cur:
                first_order = insert(cur, 'a', 'approve', '2099-01-01 00:00:00+00')
            assert not finished.is_set()
            holder.commit()
            second_order = future.result(timeout=10)
        assert second_order > first_order
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            latest = accounts._fetch_latest_reviews(cur)
            assert latest['a']['review_action'] == 'defer_missing'
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended('account-import:a',0))")
            insert(cur, 'a', 'approve', '2000-01-01 00:00:00+00')
            conn.rollback()  # A rolled back, later order cannot become the decision.
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended('account-import:b',0))")
            insert(cur, 'b', 'approve', '2000-01-01 00:00:00+00')
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended('account-import:a',0))")
            insert(cur, 'a', 'approve', '2000-01-01 00:00:00+00')
            reviews = accounts._fetch_latest_reviews(cur)
            assert reviews['a']['review_action'] == 'approve'  # Same timestamp, later insertion.
            assert reviews['b']['review_action'] == 'approve'
    finally:
        older.close()
        holder.close()
