"""TradingView publication shares the account-before-source lock order."""
# ruff: noqa: F811
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import psycopg2
from tests.imports.test_account_import_publish import client  # noqa: F401
from tests.database.test_tradingview_source_publish import isolated,stage,run_publish,review_batch,review_all  # noqa: F401
from tests.database.test_tradingview_source import HEADER,ROW1
from domain.investments import holding_evidence_writer


def test_tradingview_waits_for_account_before_source_batch(isolated,monkeypatch):
    batch_id=stage(isolated,(HEADER+ROW1).encode())
    review_batch(isolated,batch_id)
    entered=Event()
    real_lock=holding_evidence_writer.lock_holding_accounts
    def observed(cur,accounts):
        entered.set()
        return real_lock(cur,accounts)
    monkeypatch.setattr(holding_evidence_writer,'lock_holding_accounts',observed)
    blocker=psycopg2.connect(**isolated)
    try:
        with blocker.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",('account-import:tonghuashun',))
            cur.execute("SET LOCAL lock_timeout='2s'")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(run_publish,isolated,batch_id,confirm=True)
            assert entered.wait(5)
            with blocker.cursor() as cur:
                cur.execute('SELECT batch_id FROM source_batches WHERE batch_id=%s FOR UPDATE',(batch_id,))
                assert cur.fetchone()
            blocker.commit()
            assert future.result(timeout=8)['published_count']==1
    finally:
        blocker.rollback()
        blocker.close()


def test_tradingview_stage_and_publish_same_transaction_lock_account_first(isolated,monkeypatch):
    from uuid import uuid4
    from psycopg2.extras import RealDictCursor
    from domain.imports.tradingview_source import stage_csvs
    from domain.imports.tradingview_source_publish import publish
    data=(HEADER+ROW1).encode()
    entered=Event()
    real_lock=holding_evidence_writer.lock_holding_accounts
    def observed(cur,accounts):
        entered.set()
        return real_lock(cur,accounts)
    monkeypatch.setattr(holding_evidence_writer,'lock_holding_accounts',observed)
    blocker=psycopg2.connect(**isolated)
    try:
        with blocker.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",('account-import:tonghuashun',))
            cur.execute("SET LOCAL lock_timeout='2s'")
        def run():
            with psycopg2.connect(**isolated) as conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
                staged=stage_csvs(cur,batch_id=uuid4(),account_code='tonghuashun',files=[('input.csv',data)])
                review_all(cur,staged['batch_id'])
                return publish(cur,batch_id=staged['batch_id'],confirm=True)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(run)
            assert entered.wait(5)
            with blocker.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0))", ('tradingview-source:tonghuashun',))
                assert cur.fetchone()[0]
            blocker.commit()
            assert future.result(timeout=8)['published_count']==1
    finally:
        blocker.rollback()
        blocker.close()
