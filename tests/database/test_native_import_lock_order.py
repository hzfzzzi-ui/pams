"""Cross-writer account/source lock order on a disposable database."""
# ruff: noqa: F811
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import psycopg2
import pytest
from tests.database.test_native_holding_versions import isolated  # noqa: F401
from tests.imports.test_account_import_publish import client, draft, holding, preview  # noqa: F401
from domain.imports import native_source


def test_http_publication_waits_for_account_before_locking_source(isolated,monkeypatch):
    web,params=isolated
    batch=draft(web,[holding()])
    checked=preview(web,batch)
    assert checked['can_publish']
    entered=Event()
    real_lock=native_source.lock_publication_accounts
    def observed(cur,batch_id):
        entered.set()
        return real_lock(cur,batch_id)
    monkeypatch.setattr(native_source,'lock_publication_accounts',observed)
    blocker=psycopg2.connect(**params)
    try:
        with blocker.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",('account-import:tonghuashun',))
            cur.execute("SET LOCAL lock_timeout='2s'")
        def publish():
            return web.post('/api/account-import/batches/'+batch['batch_id']+'/publish',
                            json={'revision':batch['revision'],'preview_token':checked['preview_token']})
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(publish)
            assert entered.wait(5), 'HTTP publisher did not reach the account lock'
            # IBKR-like writer holds the account first and must still take batch.
            with blocker.cursor() as cur:
                cur.execute('SELECT batch_id FROM source_batches WHERE batch_id=%s FOR UPDATE',(batch['batch_id'],))
                assert cur.fetchone()
            blocker.commit()
            response=future.result(timeout=8)
            assert response.status_code==200,response.text
    finally:
        blocker.rollback()
        blocker.close()


def test_concurrent_draft_account_change_never_escapes_locked_scope(isolated,monkeypatch):
    web,params=isolated
    batch=draft(web,[holding()])
    checked=preview(web,batch)
    entered=Event()
    real_lock=native_source.lock_holding_accounts
    def observed(cur,accounts):
        entered.set()
        return real_lock(cur,accounts)
    monkeypatch.setattr(native_source,'lock_holding_accounts',observed)
    blocker=psycopg2.connect(**params)
    try:
        with blocker.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",('account-import:tonghuashun',))
        def publish():
            return web.post('/api/account-import/batches/'+batch['batch_id']+'/publish',
                            json={'revision':batch['revision'],'preview_token':checked['preview_token']})
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(publish)
            assert entered.wait(5)
            with blocker.cursor() as cur:
                cur.execute("""UPDATE source_items SET parsed_fields=jsonb_set(parsed_fields,
                    '{fields,counter_account_code}',to_jsonb('other_account'::text),true)
                    WHERE batch_id=%s""",(batch['batch_id'],))
            blocker.commit()
            response=future.result(timeout=8)
            assert response.status_code==409,response.text
        with psycopg2.connect(**params) as conn,conn.cursor() as cur:
            cur.execute('SELECT count(*) FROM pams_holdings_snapshot')
            assert cur.fetchone()[0]==0
    finally:
        blocker.rollback()
        blocker.close()


def test_inherited_account_lock_set_must_really_be_held(isolated):
    from uuid import UUID
    from psycopg2.extras import RealDictCursor
    from domain.investments.holding_evidence_writer import lock_holding_accounts,assert_holding_accounts_locked
    web,params=isolated
    batch=draft(web,[holding()])
    with psycopg2.connect(**params) as conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
        with pytest.raises(ValueError,match='not owned'):
            native_source.preview_native(cur,batch_id=UUID(batch['batch_id']),revision=batch['revision'],locked_accounts={'tonghuashun'})
        lock_holding_accounts(cur,['tonghuashun'])
        assert_holding_accounts_locked(cur,['tonghuashun'])
        with pytest.raises(ValueError,match='not owned'):
            assert_holding_accounts_locked(cur,['tonghuashun','bank_test'])
        assert native_source.preview_native(cur,batch_id=UUID(batch['batch_id']),revision=batch['revision'],locked_accounts={'tonghuashun'})['preview']['can_publish']


def test_inherited_lock_rejects_shared_and_promotes_session_lock(isolated):
    from psycopg2.extras import RealDictCursor
    from domain.investments.holding_evidence_writer import assert_holding_accounts_locked
    _,params=isolated
    with psycopg2.connect(**params) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT pg_advisory_xact_lock_shared(hashtextextended(%s,0))",('account-import:tonghuashun',))
            with pytest.raises(ValueError,match='not owned'):
                assert_holding_accounts_locked(cur,['tonghuashun'])
        conn.rollback()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT pg_advisory_lock(hashtextextended(%s,0))",('account-import:tonghuashun',))
            assert_holding_accounts_locked(cur,['tonghuashun'])
            cur.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))",('account-import:tonghuashun',))
            assert cur.fetchone()['pg_advisory_unlock']
            # The transaction-level lock remains even after session unlock.
            with psycopg2.connect(**params) as other,other.cursor() as probe:
                probe.execute("SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0))",('account-import:tonghuashun',))
                assert not probe.fetchone()[0]
        conn.commit()
    with psycopg2.connect(**params) as other,other.cursor() as probe:
        probe.execute("SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0))",('account-import:tonghuashun',))
        assert probe.fetchone()[0]
