"""Immutable account snapshot membership in a disposable empty schema."""
# ruff: noqa: F811
from datetime import date
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from time import monotonic, sleep
from uuid import uuid4
import psycopg2
from psycopg2.extras import RealDictCursor
import pytest

from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from scripts import bootstrap_isolated_sources as bootstrap
from domain.investments.account_snapshot_evidence import record_partial
from domain.investments.snapshot_coverage import load_account_snapshot_coverage


def test_partial_scope_is_idempotent_immutable_and_stale_after_change(empty_source_db):
    day = date(2026,10,22)
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO pams_holdings_snapshot(account_code,name,symbol,currency,major_category,
                minor_category,asset_liability,market_value,cost,quantity,current_price,as_of)
                VALUES ('tonghuashun','one','600000','CNY','A股','A股','资产',90,NULL,10,9,%s) RETURNING id""", (day,))
            ident = cur.fetchone()[0]
    with psycopg2.connect(**empty_source_db) as conn:
        first = record_partial(conn, account_code='tonghuashun', as_of=day, holding_ids=[ident],
                               reason='cash unknown', calculation_version='test-v1')
    with psycopg2.connect(**empty_source_db) as conn:
        assert record_partial(conn, account_code='tonghuashun', as_of=day, holding_ids=[ident],
                              reason='cash unknown', calculation_version='test-v1') == first
    with psycopg2.connect(**empty_source_db) as conn:
        with pytest.raises(ValueError, match='requested scope'):
            record_partial(conn, account_code='ibkr', as_of=day, holding_ids=[ident],
                           reason='scope mismatch', calculation_version='test-v1')
    with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
        fake = str(uuid4())
        cur.execute("""INSERT INTO account_snapshot_member(version_id,holding_id,holding_row_hash)
            SELECT %s,id,md5(to_jsonb(h)::text) FROM pams_holdings_snapshot h WHERE id=%s""", (fake,ident))
        with pytest.raises(psycopg2.errors.RaiseException, match='members are missing or changed'):
            cur.execute("""INSERT INTO account_snapshot_version
                (version_id,account_code,as_of,coverage_status,cash_status,calculation_version,reason)
                VALUES (%s,'ibkr',%s,'partial','unknown','test','mismatched')""", (fake,day))
        conn.rollback()
    with psycopg2.connect(**empty_source_db) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        coverage = load_account_snapshot_coverage(cur, {('tonghuashun',day)})[('tonghuashun',day)]
        assert coverage['status'] == 'partial' and coverage['member_ids'] == {ident}
    with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
        with pytest.raises(psycopg2.errors.RaiseException):
            cur.execute('INSERT INTO account_snapshot_member(version_id,holding_id,holding_row_hash) VALUES (%s,%s,%s)',
                        (first,ident+1,'f'*32))
        conn.rollback()
        with pytest.raises(psycopg2.errors.RaiseException):
            cur.execute('DELETE FROM account_snapshot_version WHERE version_id=%s', (first,))
        conn.rollback()
        cur.execute('UPDATE pams_holdings_snapshot SET current_price=10,market_value=100 WHERE id=%s', (ident,))
    with psycopg2.connect(**empty_source_db) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""SELECT m.holding_row_hash, md5(to_jsonb(h)::text) AS actual FROM account_snapshot_member m
                       JOIN pams_holdings_snapshot h ON h.id=m.holding_id WHERE m.version_id=%s""", (first,))
        pair = cur.fetchone()
        assert pair['holding_row_hash'] != pair['actual']
        coverage = load_account_snapshot_coverage(cur, {('tonghuashun',day)})[('tonghuashun',day)]
        assert coverage['status'] == 'unknown' and coverage['member_ids'] == set()
    with psycopg2.connect(**empty_source_db) as conn:
        second = record_partial(conn, account_code='tonghuashun', as_of=day, holding_ids=[ident],
                                reason='cash unknown', calculation_version='test-v1')
        assert first != second
    with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM account_snapshot_version")
        assert cur.fetchone()[0] == 2


def test_self_declared_complete_is_not_proven(empty_source_db):
    day = date(2026, 10, 22)
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""INSERT INTO pams_holdings_snapshot(account_code,name,symbol,currency,major_category,
                minor_category,asset_liability,market_value,cost,quantity,current_price,as_of)
                VALUES ('tonghuashun','one','600000','CNY','A股','A股','资产',90,80,10,9,%s)
                RETURNING id""", (day,))
            ident = cur.fetchone()['id']
            cur.execute('SELECT md5(to_jsonb(h)::text) AS digest FROM pams_holdings_snapshot h WHERE id=%s', (ident,))
            digest = cur.fetchone()['digest']
            version = str(uuid4())
            cur.execute('INSERT INTO account_snapshot_member(version_id,holding_id,holding_row_hash) VALUES (%s,%s,%s)',
                        (version,ident,digest))
            with pytest.raises(psycopg2.errors.CheckViolation):
                cur.execute("""INSERT INTO account_snapshot_version
                    (version_id,account_code,as_of,coverage_status,cash_status,cash_amount,calculation_version,reason)
                    VALUES (%s,'tonghuashun',%s,'complete','known',0,'untrusted','self declared')""", (version,day))
            conn.rollback()


def _source_member_fixture(params):
    """Two draft source rows and one holding without another source FK."""
    day = date(2026, 10, 22)
    batch, referenced, unreferenced = (str(uuid4()) for _ in range(3))
    with psycopg2.connect(**params) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO source_batches
                (batch_id,account_code,business_type,institution,acquisition_method)
                VALUES (%s,'tonghuashun','holding','synthetic','manual')""", (batch,))
            for item in (referenced, unreferenced):
                cur.execute("""INSERT INTO source_items
                    (source_item_id,batch_id,source_uid,parse_method,record_hash)
                    VALUES (%s,%s,%s,'manual',%s)""", (item,batch,item,'a'*64))
            cur.execute("""INSERT INTO pams_holdings_snapshot(account_code,name,symbol,currency,major_category,
                minor_category,asset_liability,market_value,cost,quantity,current_price,as_of)
                VALUES ('tonghuashun','FK test','600000','CNY','A股','A股','资产',90,80,10,9,%s)
                RETURNING id""", (day,))
            ident = cur.fetchone()[0]
            cur.execute('SELECT md5(to_jsonb(h)::text) FROM pams_holdings_snapshot h WHERE id=%s', (ident,))
            digest = cur.fetchone()[0]
    return day, ident, digest, referenced, unreferenced


def _insert_source_snapshot(cur, *, day, ident, digest, source_item):
    version = str(uuid4())
    cur.execute("""INSERT INTO account_snapshot_member(version_id,holding_id,holding_row_hash,source_item_id)
        VALUES (%s,%s,%s,%s)""", (version,ident,digest,source_item))
    cur.execute("""INSERT INTO account_snapshot_version
        (version_id,account_code,as_of,coverage_status,cash_status,calculation_version,reason)
        VALUES (%s,'tonghuashun',%s,'partial','unknown','test','source FK')""", (version,day))
    return version


def _wait_for_db_lock(params, pid):
    deadline = monotonic() + 5
    with psycopg2.connect(**params) as observer, observer.cursor() as cur:
        while monotonic() < deadline:
            cur.execute('SELECT pg_stat_clear_snapshot()')
            cur.execute('SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=%s AND NOT granted)', (pid,))
            if cur.fetchone()[0]:
                return
            sleep(.02)
    pytest.fail('concurrent source operation did not wait for the FK lock')


def test_source_member_fk_blocks_delete_but_allows_unreferenced_source(empty_source_db):
    day, ident, digest, referenced, unreferenced = _source_member_fixture(empty_source_db)
    with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
        _insert_source_snapshot(cur, day=day, ident=ident, digest=digest, source_item=referenced)
    with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
        with pytest.raises(psycopg2.errors.ForeignKeyViolation):
            cur.execute('DELETE FROM source_items WHERE source_item_id=%s', (referenced,))
        conn.rollback()
        cur.execute('DELETE FROM source_items WHERE source_item_id=%s', (unreferenced,))
        assert cur.rowcount == 1
    with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
        cur.execute('SELECT source_item_id FROM source_items WHERE source_item_id=%s', (referenced,))
        assert cur.fetchone() is not None


def test_published_snapshot_members_cannot_be_rewritten_or_truncated(empty_source_db):
    day, ident, digest, referenced, _ = _source_member_fixture(empty_source_db)
    with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
        version = _insert_source_snapshot(cur, day=day, ident=ident, digest=digest, source_item=referenced)
    with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
        statements = [
            ('UPDATE account_snapshot_member SET holding_row_hash=%s WHERE version_id=%s', ('f'*32,version)),
            ('DELETE FROM account_snapshot_member WHERE version_id=%s', (version,)),
            ('TRUNCATE account_snapshot_member', ()),
        ]
        for statement, values in statements:
            with pytest.raises(psycopg2.errors.RaiseException):
                cur.execute(statement, values)
            conn.rollback()


@pytest.mark.parametrize('isolation', ['READ COMMITTED', 'REPEATABLE READ'])
def test_source_delete_waits_for_uncommitted_member_and_then_fails(empty_source_db, isolation):
    day, ident, digest, referenced, _ = _source_member_fixture(empty_source_db)
    pids = Queue()
    def delete_source():
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
            conn.set_session(isolation_level=isolation)
            cur.execute("SET LOCAL statement_timeout='10s'")
            cur.execute('SELECT pg_backend_pid()')
            pids.put(cur.fetchone()[0])
            cur.execute('DELETE FROM source_items WHERE source_item_id=%s', (referenced,))
    with psycopg2.connect(**empty_source_db) as writer, writer.cursor() as cur:
        _insert_source_snapshot(cur, day=day, ident=ident, digest=digest, source_item=referenced)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(delete_source)
            try:
                _wait_for_db_lock(empty_source_db, pids.get(timeout=5))
                assert not future.done()
            finally:
                writer.commit()
            with pytest.raises((psycopg2.errors.ForeignKeyViolation, psycopg2.errors.SerializationFailure)):
                future.result(timeout=10)
    with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
        cur.execute('SELECT COUNT(*) FROM source_items WHERE source_item_id=%s', (referenced,))
        assert cur.fetchone()[0] == 1


@pytest.mark.parametrize('isolation', ['READ COMMITTED', 'REPEATABLE READ'])
def test_snapshot_rejects_source_deleted_concurrently_before_commit(empty_source_db, isolation):
    day, ident, digest, referenced, _ = _source_member_fixture(empty_source_db)
    pids = Queue()
    def insert_snapshot():
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
            conn.set_session(isolation_level=isolation)
            cur.execute("SET LOCAL statement_timeout='10s'")
            cur.execute('SELECT pg_backend_pid()')
            pids.put(cur.fetchone()[0])
            _insert_source_snapshot(cur, day=day, ident=ident, digest=digest, source_item=referenced)
    with psycopg2.connect(**empty_source_db) as deleter, deleter.cursor() as cur:
        cur.execute('DELETE FROM source_items WHERE source_item_id=%s', (referenced,))
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(insert_snapshot)
            try:
                _wait_for_db_lock(empty_source_db, pids.get(timeout=5))
                assert not future.done()
            finally:
                deleter.commit()
            with pytest.raises((psycopg2.errors.ForeignKeyViolation, psycopg2.errors.SerializationFailure)):
                future.result(timeout=10)
    with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
        cur.execute('SELECT COUNT(*) FROM account_snapshot_member WHERE source_item_id=%s', (referenced,))
        assert cur.fetchone()[0] == 0
