"""Explicit source/holding identity review using an isolated synthetic database."""
# ruff: noqa: F811
from datetime import date
from uuid import UUID, uuid4

import psycopg2
from psycopg2.extras import RealDictCursor
import pytest

from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from scripts.bootstrap_isolated_sources import initialize_isolated_sources
from domain.imports.ibkr_holding_source import stage_report
from domain.imports.unified_source import lock_source_review, review_item
from domain.brokers.ibkr.ibkr_holding_identity import review_holding_identity, verified_holding_identity

DAY=date(2026,10,22)
XML=b'''<FlexQueryResponse><FlexStatements><FlexStatement accountId="U_TEST" toDate="20261022"><OpenPositions>
<OpenPosition accountId="U_TEST" conid="1001" symbol="TEST" currency="USD" reportDate="20261022" position="10" costBasisMoney="80" marketValue="123" />
</OpenPositions></FlexStatement></FlexStatements></FlexQueryResponse>'''


def prepare(cur,content=XML,as_of=DAY):
    cur.execute("INSERT INTO pams_holdings_snapshot(account_code,name,symbol,currency,asset_liability,major_category,minor_category,as_of,quantity,cost,market_value) VALUES ('ibkr','Test','TEST','USD',%s,%s,'stock',%s,10,80,123) RETURNING id",('\u8d44\u4ea7','\u7f8e\u80a1',as_of))
    holding_id=cur.fetchone()['id']
    source=stage_report(cur,batch_id=uuid4(),account_code='ibkr',source_account_id='U_TEST',business_date=as_of,content=content)
    cur.execute('SELECT revision FROM source_batches WHERE batch_id=%s',(source['batch_id'],))
    rev=cur.fetchone()['revision']
    rev=review_item(cur,batch_id=UUID(source['batch_id']),item_id=UUID(source['scope_item_id']),expected_revision=rev,
                    parsed_fields={},review_status='confirmed',review_note='synthetic source review')
    return holding_id,source,rev


def test_explicit_identity_is_bound_to_full_holding_and_frozen_original(empty_source_db):
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            holding,source,rev=prepare(cur)
            args={'batch_id':source['batch_id'],'item_id':source['position_item_ids'][0],'holding_id':holding}
            with pytest.raises(ValueError,match='review note'):
                review_holding_identity(cur,**args,expected_revision=rev,review_note='')
            rev=review_holding_identity(cur,**args,expected_revision=rev,review_note='synthetic manual identity review')
            snap=lock_source_review(cur,batch_id=UUID(source['batch_id']),expected_revision=rev)
            cur.execute("UPDATE source_batches SET status='published',published_source_hash=%s WHERE batch_id=%s",
                        (snap['source_snapshot_sha256'],source['batch_id']))
            evidence=verified_holding_identity(cur,**args)
            assert evidence['conid']=='1001' and evidence['identity_authentication']=='unknown'
            from domain.investments.holding_write_evidence import reject_identity_conflicts
            row={'name':'Test','symbol':'TEST','currency':'USD','major_category':'美股',
                 'ibkr_conid':'1001','ibkr_report_account':'U_TEST'}
            reject_identity_conflicts(conn,account_code='ibkr',business_date=DAY,incoming=[row])
            for changed in ({'ibkr_conid':'2002'},{'ibkr_conid':None},{'ibkr_report_account':'OTHER'}):
                with pytest.raises(ValueError,match='contract conflicts'):
                    reject_identity_conflicts(conn,account_code='ibkr',business_date=DAY,incoming=[row | changed])
            cur.execute('UPDATE pams_holdings_snapshot SET market_value=124 WHERE id=%s',(holding,))
            with pytest.raises(ValueError,match='conflicts with holding'):
                verified_holding_identity(cur,**args)
            with pytest.raises(ValueError,match='identity review is stale'):
                reject_identity_conflicts(conn,account_code='ibkr',business_date=DAY,incoming=[row])


@pytest.mark.parametrize('content',[
    XML.replace(b'conid="1001"',b''),
    XML.replace(b'currency="USD"',b'currency="EUR"'),
    XML.replace(b'position="10"',b'position="11"'),
    XML.replace(b'conid="1001"',b'conid="1001" accountId="OTHER"'),
])
def test_source_conflicts_or_missing_contract_cannot_be_reviewed(empty_source_db,content):
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            try:
                holding,source,rev=prepare(cur,content)
                with pytest.raises(ValueError):
                    review_holding_identity(cur,batch_id=source['batch_id'],item_id=source['position_item_ids'][0],
                        holding_id=holding,expected_revision=rev,review_note='synthetic review')
            except ValueError as exc:
                assert 'report' in str(exc)


@pytest.mark.parametrize('old_day',[DAY,date(2026,10,21)],ids=['same_day','previous_day'])
def test_same_contract_explicit_zero_updates_fact_and_keeps_old_version(empty_source_db,monkeypatch,old_day):
    from hashlib import sha256
    from collectors.ibkr_flex import _write_holdings
    from domain.investments import holding_write_evidence
    monkeypatch.setenv('PAMS_HOLDING_EVIDENCE_ISOLATED','true')
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            old_xml=XML.replace(b'20261022',old_day.strftime('%Y%m%d').encode())
            holding,old,rev=prepare(cur,old_xml,old_day)
            rev=review_holding_identity(cur,batch_id=old['batch_id'],item_id=old['position_item_ids'][0],
                holding_id=holding,expected_revision=rev,review_note='synthetic explicit identity review')
            snap=lock_source_review(cur,batch_id=UUID(old['batch_id']),expected_revision=rev)
            cur.execute("UPDATE source_batches SET status='published',published_source_hash=%s WHERE batch_id=%s",(snap['source_snapshot_sha256'],old['batch_id']))
            zero=XML.replace(b'position="10"',b'position="0"').replace(b'costBasisMoney="80"',b'costBasisMoney="0"').replace(b'marketValue="123"',b'marketValue="0"')
            source=stage_report(cur,batch_id=uuid4(),account_code='ibkr',source_account_id='U_TEST',business_date=DAY,content=zero)
            cur.execute("UPDATE source_batches SET completeness='complete' WHERE batch_id=%s",(source['batch_id'],))
            cur.execute('SELECT revision FROM source_batches WHERE batch_id=%s',(source['batch_id'],)); rev=cur.fetchone()['revision']
            rev=review_item(cur,batch_id=UUID(source['batch_id']),item_id=UUID(source['scope_item_id']),expected_revision=rev,
                parsed_fields={'coverage':'all_positions','confirmed_complete':True},review_status='confirmed',review_note='synthetic complete scope')
            rev=review_item(cur,batch_id=UUID(source['batch_id']),item_id=UUID(source['position_item_ids'][0]),expected_revision=rev,
                parsed_fields={},review_status='confirmed',review_note='synthetic zero')
            snap=lock_source_review(cur,batch_id=UUID(source['batch_id']),expected_revision=rev)
            cur.execute("UPDATE source_batches SET status='published',published_source_hash=%s WHERE batch_id=%s",(snap['source_snapshot_sha256'],source['batch_id']))
            wrong=zero.replace(b'conid="1001"',b'conid="2002"')
            other=stage_report(cur,batch_id=uuid4(),account_code='ibkr',source_account_id='U_TEST',business_date=DAY,content=wrong)
            cur.execute("UPDATE source_batches SET completeness='complete' WHERE batch_id=%s",(other['batch_id'],))
            cur.execute('SELECT revision FROM source_batches WHERE batch_id=%s',(other['batch_id'],)); rev=cur.fetchone()['revision']
            rev=review_item(cur,batch_id=UUID(other['batch_id']),item_id=UUID(other['scope_item_id']),expected_revision=rev,
                parsed_fields={'coverage':'all_positions','confirmed_complete':True},review_status='confirmed',review_note='synthetic complete scope')
            rev=review_item(cur,batch_id=UUID(other['batch_id']),item_id=UUID(other['position_item_ids'][0]),expected_revision=rev,
                parsed_fields={},review_status='confirmed',review_note='synthetic zero')
            snap=lock_source_review(cur,batch_id=UUID(other['batch_id']),expected_revision=rev)
            cur.execute("UPDATE source_batches SET status='published',published_source_hash=%s WHERE batch_id=%s",(snap['source_snapshot_sha256'],other['batch_id']))
        conn.commit()
        ref={'Test':{'batch_id':source['batch_id'],'item_id':source['position_item_ids'][0],
                     'identity_batch_id':old['batch_id'],'identity_item_id':old['position_item_ids'][0]}}
        wrong_ref={'Test':dict(ref['Test'],batch_id=other['batch_id'],item_id=other['position_item_ids'][0])}
        with pytest.raises(ValueError,match='conid identity'):
            _write_holdings(conn,[],account_code='ibkr',as_of=DAY,retirement_sources=wrong_ref,expected_report_sha256=sha256(wrong).hexdigest())
        original_finish=holding_write_evidence.finish_ibkr_retirements
        def fail_after_update(*args,**kwargs): raise RuntimeError('synthetic rollback')
        monkeypatch.setattr(holding_write_evidence,'finish_ibkr_retirements',fail_after_update)
        with pytest.raises(RuntimeError,match='synthetic rollback'):
            _write_holdings(conn,[],account_code='ibkr',as_of=DAY,retirement_sources=ref,expected_report_sha256=sha256(zero).hexdigest())
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT quantity,cost,market_value FROM pams_holdings_snapshot WHERE id=%s',(holding,))
            assert tuple(cur.fetchone().values())==(10,80,123)
        monkeypatch.setattr(holding_write_evidence,'finish_ibkr_retirements',original_finish)
        conn.commit()
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        barrier=Barrier(2)
        def publish(_):
            with psycopg2.connect(**empty_source_db) as worker:
                barrier.wait(timeout=10)
                return _write_holdings(worker,[],account_code='ibkr',as_of=DAY,retirement_sources=ref,expected_report_sha256=sha256(zero).hexdigest())
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert list(pool.map(publish,range(2)))==[0,0]
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT quantity,cost,market_value FROM pams_holdings_snapshot WHERE account_code=%s AND name=%s AND as_of=%s',('ibkr','Test',DAY))
            assert tuple(cur.fetchone().values())==(0,0,0)
            if old_day!=DAY:
                cur.execute('SELECT quantity,cost,market_value,as_of FROM pams_holdings_snapshot WHERE id=%s',(holding,))
                assert tuple(cur.fetchone().values())==(10,80,123,old_day)
            cur.execute("SELECT evidence->'snapshot'->'content'->>'market_value' AS value FROM valuation_evidence v CROSS JOIN LATERAL jsonb_to_recordset(v.inputs) AS x(input_kind text,input_hash text,evidence jsonb) WHERE input_kind='holding'")
            assert {'123.0000','0.0000'}.issubset({row['value'] for row in cur.fetchall()})
        conn.commit()
        assert _write_holdings(conn,[],account_code='ibkr',as_of=DAY,retirement_sources=ref,expected_report_sha256=sha256(zero).hexdigest())==0
        with pytest.raises(ValueError):
            _write_holdings(conn,[],account_code='ibkr',as_of=DAY,retirement_sources=ref,expected_report_sha256='0'*64)
