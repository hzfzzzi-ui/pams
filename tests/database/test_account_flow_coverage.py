"""Source-bound complete external-flow periods: no production access."""
# ruff: noqa: F811
import csv
import io
from datetime import date, timedelta

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor
from tests.imports.test_account_import_publish import client  # noqa: F401
from domain.accounts import flow_coverage


def _csv(day, *, account='tonghuashun', events=None, first=None, last=None):
    first=first or day; last=last or day
    records=events if events is not None else [{'side':'none'}]
    output=io.StringIO(newline=''); writer=csv.writer(output,lineterminator='\n')
    writer.writerow(flow_coverage.FIELDS)
    for event in records:
        writer.writerow([account,first.isoformat(),last.isoformat(),event.get('date',''),event['side'],
                         event.get('currency',''),event.get('amount',''),event.get('id','')])
    return output.getvalue().encode()


def test_report_template_preserves_explicit_empty_period_and_rejects_invalid_rows(monkeypatch):
    # Fix the business date independently of the host/CI system timezone.
    monkeypatch.setattr(flow_coverage, '_view_date', lambda: date(2026, 1, 1))
    day=date(2025, 12, 31)
    parsed=flow_coverage.parse_report(_csv(day),account_code='tonghuashun')
    assert parsed['events']==[] and parsed['period_start']==parsed['period_end']==day.isoformat()
    duplicate=[{'side':'deposit','date':day.isoformat(),'currency':'CNY','amount':'1','id':'same'}]*2
    invalid=[_csv(day,events=duplicate),_csv(day,account='other'),
             _csv(day,events=[{'side':'deposit','date':day.isoformat(),'currency':'CNY','amount':'0','id':'a'}]),
             _csv(day,events=[{'side':'withdraw','date':day.isoformat(),'currency':'CNY','amount':'1','id':'a'}]),
             _csv(day,events=[{'side':'deposit','date':day.isoformat(),'currency':'CNY','amount':'NaN','id':'a'}]),
             _csv(day,events=[{'side':'none'},{'side':'deposit','date':day.isoformat(),'currency':'CNY','amount':'1','id':'a'}]),
             _csv(day,first=day+timedelta(days=2),last=day),
             _csv(day+timedelta(days=2))]
    for content in invalid:
        with pytest.raises(ValueError):
            flow_coverage.parse_report(content,account_code='tonghuashun')


def test_empty_report_is_explicitly_reviewed_published_and_idempotent(client):
    web,params=client; day=date.today()-timedelta(days=1); content=_csv(day)
    url='/api/account-import/flow-coverage/stage?account_code=tonghuashun&filename=scope.csv'
    stage=web.post(url,content=content,headers={'Content-Type':'text/csv'})
    assert stage.status_code==200,stage.text
    draft=stage.json(); path='/api/account-import/flow-coverage/'+draft['batch_id']+'/publish'
    body={'revision':draft['revision'],'mappings':{},'note':'reviewed complete period','confirmed':True}
    assert web.post(path,json={**body,'confirmed':False}).status_code==400
    assert web.post(path,json={**body,'mappings':{'fake':99}}).status_code==400
    result=web.post(path,json=body)
    assert result.status_code==200,result.text
    published=result.json(); assert published['status']=='published' and not published['replayed']
    assert published['review_authentication']=='unknown'
    again=web.post(url,content=content,headers={'Content-Type':'text/csv'})
    assert again.status_code==200 and again.json()['batch_id']==draft['batch_id']
    replay=web.post(path,json={**body,'revision':published['revision']})
    assert replay.status_code==200 and replay.json()['replayed'],replay.text
    assert web.post(path,json={**body,'revision':published['revision'],'note':'different'}).status_code==400
    with psycopg2.connect(**params) as conn:
        conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT * FROM source_batches WHERE batch_id=%s',(published['batch_id'],))
            report,records,digest=flow_coverage._scope(cur,cur.fetchone())
            assert report['events']==[] and records==[] and len(digest)==64
    # A changed original must make the previously published proof unusable.
    with psycopg2.connect(**params) as conn,conn.cursor() as cur:
        cur.execute("ALTER TABLE source_files DISABLE TRIGGER source_files_evidence_immutable")
        cur.execute("UPDATE source_files SET content=content || %s::bytea WHERE batch_id=%s",(b'x',published['batch_id']))
        cur.execute("ALTER TABLE source_files ENABLE TRIGGER source_files_evidence_immutable")
    with psycopg2.connect(**params) as conn:
        conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT * FROM source_batches WHERE batch_id=%s',(published['batch_id'],))
            with pytest.raises(ValueError):
                flow_coverage._scope(cur,cur.fetchone())


def test_coverage_needs_same_origin_matching_account_and_isolation(client,monkeypatch):
    web,_=client; day=date.today()-timedelta(days=1); content=_csv(day)
    url='/api/account-import/flow-coverage/stage?account_code=tonghuashun&filename=scope.csv'
    response=web.post(url,content=content,headers={'origin':'http://other','Content-Type':'text/csv'})
    assert response.status_code==403
    response=web.post('/api/account-import/flow-coverage/stage?account_code=bank_test&filename=scope.csv',
                      content=content,headers={'Content-Type':'text/csv'})
    assert response.status_code==400
    monkeypatch.delenv('PAMS_UNIFIED_SOURCE_ISOLATED')
    response=web.post(url,content=content,headers={'Content-Type':'text/csv'})
    assert response.status_code==403
