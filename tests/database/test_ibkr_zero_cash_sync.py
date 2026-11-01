"""Explicit zero cash is a source fact; absence is never treated as zero."""
# ruff: noqa: F811
import re
import pytest
from tests.brokers.ibkr.test_ibkr_sync_transaction import database as database, sync_case as sync_case, rows, business_state
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401


def change_cash(xml, transform):
    item=re.search(r'<CashReportCurrency accountId="U_TEST" currency="USD"[^>]*/>',xml).group()
    return xml.replace(item,transform(item))


def test_real_flex_sync_writes_explicit_zero_and_replays_idempotently(sync_case,database):
    client,_,report=sync_case
    assert next(r for r in rows(database,'pams_holdings_snapshot') if r['symbol']=='CASH_USD')['market_value']==-500
    report['xml']=change_cash(report['xml'],lambda item:item.replace('endingCash="-500"','endingCash="0"'))
    response=client.post('/api/ibkr/flex/sync',json={})
    assert response.status_code==200 and response.json()['success'],response.text
    cash=next(r for r in rows(database,'pams_holdings_snapshot') if r['symbol']=='CASH_USD')
    assert (cash['market_value'],cash['quantity'],cash['cost'],cash['asset_liability'])==(0,0,0,'资产')
    before=business_state(database)
    replay=client.post('/api/ibkr/flex/sync',json={})
    assert replay.status_code==200 and replay.json()['success'],replay.text
    assert business_state(database)==before


@pytest.mark.parametrize('kind',['missing_amount','missing_currency','missing_date','duplicate_currency','missing_row','precision'])
def test_missing_or_ambiguous_cash_never_becomes_zero_and_rolls_back(sync_case,database,kind):
    client,_,report=sync_case
    def alter(item):
        if kind=='missing_amount': return item.replace('endingCash="-500"','')
        if kind=='missing_currency': return item.replace('currency="USD"','')
        if kind=='missing_date': return item.replace('toDate="20261022"','')
        if kind=='duplicate_currency': return item+item
        if kind=='missing_row': return ''
        return item.replace('endingCash="-500"','endingCash="0.00001"')
    report['xml']=change_cash(report['xml'],alter)
    before=business_state(database)
    response=client.post('/api/ibkr/flex/sync',json={})
    assert response.status_code>=400 or not response.json().get('success'),response.text
    assert business_state(database)==before


def test_two_zero_cash_syncs_serialize_and_replay_without_duplicate_facts(sync_case,database,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from endpoints import ibkr
    client,_,report=sync_case
    report['xml']=change_cash(report['xml'],lambda item:item.replace('endingCash="-500"','endingCash="0"'))
    monkeypatch.setattr(ibkr,'get_conn',lambda:database())
    with ThreadPoolExecutor(max_workers=2) as executor:
        results=list(executor.map(lambda _:client.post('/api/ibkr/flex/sync',json={}),range(2)))
    assert all(r.status_code==200 and r.json()['success'] for r in results),[(r.status_code,r.text) for r in results]
    cash=[r for r in rows(database,'pams_holdings_snapshot') if r['symbol']=='CASH_USD']
    assert len(cash)==1 and cash[0]['market_value']==0
    before=business_state(database)
    replay=client.post('/api/ibkr/flex/sync',json={})
    assert replay.status_code==200 and replay.json()['success']
    assert business_state(database)==before


def test_explicit_zero_cash_is_a_reviewed_complete_scope_member(empty_source_db,monkeypatch):
    from datetime import timedelta
    from uuid import uuid4
    import psycopg2
    from psycopg2.extras import RealDictCursor
    from collectors import ibkr_flex
    from domain.imports.ibkr_holding_source import stage_report
    from domain.brokers.ibkr.ibkr_holding_review import review,review_identity,review_account_binding,review_account_scope,publish
    from domain.investments.snapshot_coverage import load_account_snapshot_coverage
    from tests.database.test_ibkr_account_complete_scope import scope_report,DAY
    from tests.database.test_ibkr_holding_review_http import setup
    report=scope_report().replace(b'symbol="TEST"',b'symbol="TEST" description="Test"')
    report=report.replace(b'currency="USD" toDate="20261022" endingCash="50"',
                          b'currency="USD" toDate="20261022" endingCash="0"')
    report=report.replace(b' cash="110"',b' cash="60"').replace(b'total="233"',b'total="183"')
    report=report.replace(b'cashLong="110"',b'cashLong="60"').replace(b'totalLong="233"',b'totalLong="183"')
    with psycopg2.connect(**empty_source_db) as conn:
        holding=setup(conn,monkeypatch,empty_source_db)
        monkeypatch.setenv('PAMS_HOLDING_EVIDENCE_ISOLATED','true')
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO pams_holdings_snapshot(account_code,name,symbol,currency,asset_liability,
                major_category,minor_category,as_of,quantity,cost,market_value) VALUES
                ('ibkr','Cash USD','CASH_USD','USD','资产','现金','现金/活期',%s,50,50,50)""",(DAY-timedelta(days=1),))
        conn.commit()
        parsed=ibkr_flex.parse_flex_report(report.decode())
        assert next(row for row in parsed.rows if row['symbol']=='CASH_USD')['market_value']==0
        assert ibkr_flex._write_holdings(conn,parsed.rows,account_code='ibkr',as_of=DAY)==len(parsed.rows)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            source=stage_report(cur,batch_id=uuid4(),account_code='ibkr',source_account_id='U_TEST',business_date=DAY,content=report)
            cur.execute('SELECT revision FROM source_batches WHERE batch_id=%s',(source['batch_id'],)); rev=cur.fetchone()['revision']
            rev=review(cur,batch_id=source['batch_id'],item_id=source['position_item_ids'][0],expected_revision=rev,note='row')['revision']
            rev=review_identity(cur,batch_id=source['batch_id'],item_id=source['position_item_ids'][0],holding_id=holding,expected_revision=rev,note='contract')['revision']
            rev=review(cur,batch_id=source['batch_id'],item_id=source['scope_item_id'],expected_revision=rev,note='positions',scope_complete=True)['revision']
            rev=review_account_binding(cur,batch_id=source['batch_id'],source_account_id='U_TEST',expected_revision=rev,note='account')['revision']
            rev=review_account_scope(cur,batch_id=source['batch_id'],expected_revision=rev,note='zero cash reviewed',confirmed=True)['revision']
            result=publish(cur,batch_id=source['batch_id'],expected_revision=rev)
        conn.commit()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            assert load_account_snapshot_coverage(cur,{('ibkr',DAY)})[('ibkr',DAY)]['status']=='complete'
            cur.execute('SELECT scope_attestation FROM account_snapshot_version WHERE version_id=%s',(result['account_scope_version_id'],))
            assert cur.fetchone()['scope_attestation']['cash_amounts']['USD']=='0'
            cur.execute("SELECT market_value FROM pams_holdings_snapshot WHERE symbol='CASH_USD' AND as_of=%s",(DAY,))
            assert cur.fetchone()['market_value']==0
