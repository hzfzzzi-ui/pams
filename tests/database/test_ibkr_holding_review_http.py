"""Real HTTP against a freshly bootstrapped disposable PostgreSQL database."""
# ruff: noqa: F811
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import re

import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.extensions import make_dsn
from fastapi.testclient import TestClient

from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from tests.database.test_ibkr_holding_identity_review import XML
from tests.database.test_ibkr_report_batch import report, execute
from scripts.bootstrap_isolated_sources import initialize_isolated_sources

DAY=date(2026,10,22)
HEADERS={'origin':'http://pams.local','x-pams-isolated-review':'1'}


def setup(conn,monkeypatch,params,holding_day=DAY):
    initialize_isolated_sources(conn)
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO pams_holdings_snapshot(account_code,name,symbol,currency,asset_liability,
            major_category,minor_category,as_of,quantity,cost,market_value) VALUES
            ('ibkr','Test','TEST','USD','资产','美股','stock',%s,10,80,123) RETURNING id""",(holding_day,))
        holding=cur.fetchone()[0]
    conn.commit()
    monkeypatch.setenv('PAMS_HOLDING_REVIEW_TEST_DSN',make_dsn(**params))
    return holding


def test_http_requires_independent_scope_row_and_identity_review(empty_source_db,monkeypatch):
    import main
    with psycopg2.connect(**empty_source_db) as conn:
        holding=setup(conn,monkeypatch,empty_source_db)
        with TestClient(main.app,base_url='http://pams.local') as web:
            payload={'source_account_id':'U_TEST','business_date':DAY.isoformat(),'xml_text':XML.decode()}
            assert web.post('/api/ibkr/holding-review/stage',json=payload).status_code==403
            stage=web.post('/api/ibkr/holding-review/stage',json=payload,headers=HEADERS)
            assert stage.status_code==200,stage.text
            assert stage.headers['cache-control']=='no-store'
            source=stage.json(); batch=source['batch_id']; item=source['position_item_ids'][0]
            base=f'/api/ibkr/holding-review/{batch}'
            state=web.get(base,headers=HEADERS).json(); revision=state['revision']
            assert not state['scope_reviewed'] and state['identity_authentication']=='unknown'
            assert web.post(base+'/publish',json={'expected_revision':revision},headers=HEADERS).status_code==409
            identity={'item_id':item,'holding_id':holding,'expected_revision':revision,'note':'synthetic identity'}
            assert web.post(base+'/identity',json=identity,headers=HEADERS).status_code==409
            row=web.post(base+'/review',json={'item_id':item,'expected_revision':revision,'note':'synthetic row'},headers=HEADERS)
            assert row.status_code==200,row.text
            revision=row.json()['revision']
            stale=web.post(base+'/review',json={'item_id':item,'expected_revision':revision-1,'note':'stale'},headers=HEADERS)
            assert stale.status_code==409
            identity['expected_revision']=revision
            result=web.post(base+'/identity',json=identity,headers=HEADERS)
            assert result.status_code==200,result.text
            assert result.json()['identity_authentication']=='unknown'
            revision=result.json()['revision']
            scope={'item_id':source['scope_item_id'],'expected_revision':revision,'note':'synthetic scope'}
            assert web.post(base+'/review',json=scope,headers=HEADERS).status_code==409
            scope['scope_complete']=True
            reviewed=web.post(base+'/review',json=scope,headers=HEADERS)
            assert reviewed.status_code==200,reviewed.text
            published=web.post(base+'/publish',json={'expected_revision':reviewed.json()['revision']},headers=HEADERS)
            assert published.status_code==200,published.text
            assert published.json()['scope']=='source_review_only' and published.json()['review_authentication']=='unknown'
            again=web.post('/api/ibkr/holding-review/stage',json=payload,headers=HEADERS)
            assert again.status_code==200 and again.json()['origin_reused'] is True
        with conn.cursor() as cur:
            cur.execute('SELECT count(*) FROM source_files'); assert cur.fetchone()[0]==1
            cur.execute("SELECT count(*) FROM source_items WHERE raw_fields ? 'raw_xml'"); assert cur.fetchone()[0]==0
            cur.execute('SELECT quantity,cost,market_value FROM pams_holdings_snapshot WHERE id=%s',(holding,))
            assert cur.fetchone()==(10,80,123)


def test_http_reuses_published_original_without_duplicate_bytes(empty_source_db,monkeypatch):
    import main
    content=report()
    position=re.search(rb'<OpenPosition\b[^>]*/>',content).group()
    position=position.replace(b'symbol="TEST"',b'symbol="TEST" conid="1001"')
    content=re.sub(rb'<OpenPosition\b[^>]*/>',b'<OpenPositions>'+position+b'</OpenPositions>',content,count=1)
    with psycopg2.connect(**empty_source_db) as conn:
        setup(conn,monkeypatch,empty_source_db)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(base_currency,quote_currency,as_of,rate) VALUES ('CNY','USD',%s,7)",(DAY,))
            execute(cur,content)
        conn.commit()
        with TestClient(main.app,base_url='http://pams.local') as web:
            stage=web.post('/api/ibkr/holding-review/stage',headers=HEADERS,json={
                'source_account_id':'U_TEST','business_date':DAY.isoformat(),'xml_text':content.decode()})
            assert stage.status_code==200,stage.text
            assert stage.json()['origin_reused'] is True
            source=stage.json(); base='/api/ibkr/holding-review/'+source['batch_id']
            revision=web.get(base,headers=HEADERS).json()['revision']
            row=web.post(base+'/review',headers=HEADERS,json={'item_id':source['position_item_ids'][0],
                'expected_revision':revision,'note':'synthetic row'})
            assert row.status_code==200,row.text
            scope=web.post(base+'/review',headers=HEADERS,json={'item_id':source['scope_item_id'],
                'expected_revision':row.json()['revision'],'note':'synthetic scope','scope_complete':True})
            assert scope.status_code==200,scope.text
            published=web.post(base+'/publish',headers=HEADERS,json={'expected_revision':scope.json()['revision']})
            assert published.status_code==200,published.text
            assert web.get(base,headers=HEADERS).json()['status']=='published'
        with conn.cursor() as cur:
            cur.execute('SELECT count(*) FROM source_files'); assert cur.fetchone()[0]==1
            cur.execute("SELECT count(*) FROM source_items i JOIN source_batches b ON b.batch_id=i.batch_id WHERE b.workflow='ibkr_holding_review' AND i.origin_file_id IS NOT NULL AND i.file_id IS NULL")
            assert cur.fetchone()[0]==2
            cur.execute("SELECT count(*) FROM source_items WHERE raw_fields ? 'raw_xml'"); assert cur.fetchone()[0]==0


def test_http_failure_rolls_back_stage_and_rejects_non_disposable_dsn(empty_source_db,monkeypatch):
    import main
    from domain.imports import ibkr_holding_source as source
    with psycopg2.connect(**empty_source_db) as conn:
        setup(conn,monkeypatch,empty_source_db)
        original_add=source.add_item
        def fail_after_scope(*args,**kwargs):
            if kwargs.get('source_uid','').startswith('position:'):
                raise ValueError('synthetic failure')
            return original_add(*args,**kwargs)
        monkeypatch.setattr(source,'add_item',fail_after_scope)
        with TestClient(main.app,base_url='http://pams.local') as web:
            body={'source_account_id':'U_TEST','business_date':DAY.isoformat(),'xml_text':XML.decode()}
            assert web.post('/api/ibkr/holding-review/stage',json=body,headers=HEADERS).status_code==409
            monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED','false')
            assert web.post('/api/ibkr/holding-review/stage',json=body,headers=HEADERS).status_code==404
            monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED','true')
            monkeypatch.setenv('PAMS_HOLDING_REVIEW_TEST_DSN','host=example.invalid dbname=production')
            assert web.post('/api/ibkr/holding-review/stage',json=body,headers=HEADERS).status_code==409
            monkeypatch.setenv('PAMS_HOLDING_REVIEW_TEST_DSN',make_dsn(**empty_source_db)+' hostaddr=192.0.2.1')
            assert web.post('/api/ibkr/holding-review/stage',json=body,headers=HEADERS).status_code==409
        with conn.cursor() as cur:
            for table in ('source_batches','source_items','source_files'):
                cur.execute('SELECT count(*) FROM '+table); assert cur.fetchone()[0]==0


def test_identity_review_stale_holding_cannot_publish(empty_source_db,monkeypatch):
    import main
    with psycopg2.connect(**empty_source_db) as conn:
        holding=setup(conn,monkeypatch,empty_source_db)
        with TestClient(main.app,base_url='http://pams.local') as web:
            response=web.post('/api/ibkr/holding-review/stage',headers=HEADERS,json={
                'source_account_id':'U_TEST','business_date':DAY.isoformat(),'xml_text':XML.decode()})
            assert response.status_code==200,response.text
            stage=response.json()
            base='/api/ibkr/holding-review/'+stage['batch_id']
            rev=web.get(base,headers=HEADERS).json()['revision']
            row=web.post(base+'/review',headers=HEADERS,json={'item_id':stage['position_item_ids'][0],
                'expected_revision':rev,'note':'synthetic row'}).json()
            identity=web.post(base+'/identity',headers=HEADERS,json={'item_id':stage['position_item_ids'][0],
                'holding_id':holding,'expected_revision':row['revision'],'note':'synthetic identity'}).json()
            scope=web.post(base+'/review',headers=HEADERS,json={'item_id':stage['scope_item_id'],
                'expected_revision':identity['revision'],'note':'synthetic scope','scope_complete':True}).json()
            with conn.cursor() as cur:
                cur.execute('UPDATE pams_holdings_snapshot SET market_value=124 WHERE id=%s',(holding,))
            conn.commit()
            failed=web.post(base+'/publish',headers=HEADERS,json={'expected_revision':scope['revision']})
            assert failed.status_code==409
            assert web.get(base,headers=HEADERS).json()['status']=='draft'


def test_corrupt_published_original_cannot_be_reused(empty_source_db,monkeypatch):
    import main
    content=report()
    position=re.search(rb'<OpenPosition\b[^>]*/>',content).group()
    content=re.sub(rb'<OpenPosition\b[^>]*/>',b'<OpenPositions>'+position+b'</OpenPositions>',content,count=1)
    with psycopg2.connect(**empty_source_db) as conn:
        setup(conn,monkeypatch,empty_source_db)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(base_currency,quote_currency,as_of,rate) VALUES ('CNY','USD',%s,7)",(DAY,))
            execute(cur,content)
        conn.commit()
        with conn.cursor() as cur:
            cur.execute('ALTER TABLE source_files DISABLE TRIGGER USER')
            cur.execute("UPDATE source_files SET content=decode('00','hex')")
            cur.execute('ALTER TABLE source_files ENABLE TRIGGER USER')
        conn.commit()
        with TestClient(main.app,base_url='http://pams.local') as web:
            response=web.post('/api/ibkr/holding-review/stage',headers=HEADERS,json={
                'source_account_id':'U_TEST','business_date':DAY.isoformat(),'xml_text':content.decode()})
            assert response.status_code==409
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM source_batches WHERE workflow='ibkr_holding_review'")
            assert cur.fetchone()[0]==0


def test_identity_review_uses_published_reused_original(empty_source_db,monkeypatch):
    import main
    from domain.brokers.ibkr.ibkr_holding_identity import verified_holding_identity
    with psycopg2.connect(**empty_source_db) as conn:
        holding=setup(conn,monkeypatch,empty_source_db)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            execute(cur,XML)
        conn.commit()
        with TestClient(main.app,base_url='http://pams.local') as web:
            response=web.post('/api/ibkr/holding-review/stage',headers=HEADERS,json={
                'source_account_id':'U_TEST','business_date':DAY.isoformat(),'xml_text':XML.decode()})
            assert response.status_code==200,response.text
            source=response.json(); assert source['origin_reused']
            base='/api/ibkr/holding-review/'+source['batch_id']
            rev=web.get(base,headers=HEADERS).json()['revision']
            row=web.post(base+'/review',headers=HEADERS,json={'item_id':source['position_item_ids'][0],
                'expected_revision':rev,'note':'synthetic row'}).json()
            identity=web.post(base+'/identity',headers=HEADERS,json={'item_id':source['position_item_ids'][0],
                'holding_id':holding,'expected_revision':row['revision'],'note':'synthetic identity'}).json()
            scope=web.post(base+'/review',headers=HEADERS,json={'item_id':source['scope_item_id'],
                'expected_revision':identity['revision'],'note':'synthetic scope','scope_complete':True}).json()
            final=web.post(base+'/publish',headers=HEADERS,json={'expected_revision':scope['revision']})
            assert final.status_code==200,final.text
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            proof=verified_holding_identity(cur,batch_id=source['batch_id'],
                item_id=source['position_item_ids'][0],holding_id=holding)
            assert proof['conid']=='1001' and proof['identity_authentication']=='unknown'
            cur.execute('SELECT count(*) AS n FROM source_files'); assert cur.fetchone()['n']==1


def test_full_http_cross_day_review_retirement_and_concurrent_replay(empty_source_db,monkeypatch,tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    import main
    from collectors import ibkr_flex
    from domain.brokers.ibkr import ibkr_report_state
    from endpoints import ibkr

    previous=DAY-timedelta(days=1)
    old_xml=XML.replace(b'20261022',b'20261021')
    zero=XML.replace(b'position="10"',b'position="0"').replace(b'costBasisMoney="80"',b'costBasisMoney="0"').replace(b'marketValue="123"',b'marketValue="0" positionValue="0"')
    extra=(b'<CashReportCurrency currency="BASE_SUMMARY" toDate="20261022" endingCash="10" depositWithdrawalsYTD="0" />'
        b'<CashReportCurrency accountId="U_TEST" currency="USD" toDate="20261022" endingCash="10" />'
        b'<Trade accountId="U_TEST" symbol="OTHER" description="Other" assetCategory="STK" currency="USD" '
        b'tradeID="T1" tradeDate="20261022" buySell="BUY" quantity="1" tradePrice="10" tradeMoney="10" '
        b'ibCommission="0" ibCommissionCurrency="USD" taxAmount="0" netCash="-10" />')
    zero=zero.replace(b'</FlexStatement>',extra+b'</FlexStatement>')
    with psycopg2.connect(**empty_source_db) as conn:
        holding=setup(conn,monkeypatch,empty_source_db,holding_day=previous)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(base_currency,quote_currency,as_of,rate) VALUES ('CNY','USD',%s,7)",(DAY,))
        conn.commit()
        monkeypatch.setenv('PAMS_HOLDING_EVIDENCE_ISOLATED','true')
        monkeypatch.setenv('PAMS_DATA_DIR',str(tmp_path))
        monkeypatch.delenv('PAMS_IBKR_CASH_EVENTS_AFTER',raising=False)
        monkeypatch.setattr(ibkr,'get_conn',lambda:psycopg2.connect(**empty_source_db))
        monkeypatch.setattr(ibkr,'_credentials',lambda:('synthetic-token','synthetic-query'))
        monkeypatch.setattr(ibkr_flex,'request_flex_reference',lambda *a,**k:{'reference_code':'TEST'})
        monkeypatch.setattr(ibkr_flex,'fetch_flex_statement',lambda *a,**k:zero.decode())
        monkeypatch.setattr(ibkr_report_state,'utc_now',lambda:datetime(2026,10,22,22,tzinfo=timezone.utc))
        with TestClient(main.app,base_url='http://pams.local') as web:
            def reviewed(content,day,identity_holding=None):
                stage=web.post('/api/ibkr/holding-review/stage',headers=HEADERS,json={
                    'source_account_id':'U_TEST','business_date':day.isoformat(),'xml_text':content.decode()})
                assert stage.status_code==200,stage.text
                source=stage.json(); base='/api/ibkr/holding-review/'+source['batch_id']
                rev=web.get(base,headers=HEADERS).json()['revision']
                row=web.post(base+'/review',headers=HEADERS,json={'item_id':source['position_item_ids'][0],
                    'expected_revision':rev,'note':'synthetic row'})
                assert row.status_code==200,row.text
                rev=row.json()['revision']
                if identity_holding is not None:
                    identity=web.post(base+'/identity',headers=HEADERS,json={'item_id':source['position_item_ids'][0],
                        'holding_id':identity_holding,'expected_revision':rev,'note':'synthetic identity'})
                    assert identity.status_code==200,identity.text
                    rev=identity.json()['revision']
                scope=web.post(base+'/review',headers=HEADERS,json={'item_id':source['scope_item_id'],
                    'expected_revision':rev,'note':'synthetic scope','scope_complete':True})
                assert scope.status_code==200,scope.text
                final=web.post(base+'/publish',headers=HEADERS,json={'expected_revision':scope.json()['revision']})
                assert final.status_code==200,final.text
                return source
            old=reviewed(old_xml,previous,holding)
            exit_source=reviewed(zero,DAY)
            ref={'Test':{'batch_id':exit_source['batch_id'],'item_id':exit_source['position_item_ids'][0],
                         'identity_batch_id':old['batch_id'],'identity_item_id':old['position_item_ids'][0]}}
            barrier=Barrier(2)
            def sync(_):
                with TestClient(main.app,base_url='http://pams.local') as client:
                    barrier.wait(timeout=10)
                    response=client.post('/api/ibkr/flex/sync',json={'holding_retirement_sources':ref})
                    return response.status_code,response.json()
            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes=list(pool.map(sync,range(2)))
            assert all(code==200 and payload.get('success') for code,payload in outcomes),outcomes
            replay=web.post('/api/ibkr/flex/sync',json={'holding_retirement_sources':ref})
            assert replay.status_code==200 and replay.json().get('success'),replay.text
            assert not list(tmp_path.rglob('*.xml'))
            with monkeypatch.context() as disabled:
                disabled.setenv('PAMS_UNIFIED_SOURCE_ISOLATED','false')
                with psycopg2.connect(**empty_source_db) as read_conn:
                    read_conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
                    selected,_=ibkr_report_state.select_report(read_conn,tmp_path)
                    assert selected and selected.path.read_bytes()==zero
                blocked=web.post('/api/ibkr/flex/sync',json={'holding_retirement_sources':ref})
                assert blocked.status_code>=400
        with conn.cursor() as cur:
            cur.execute("SELECT as_of,quantity,cost,market_value FROM pams_holdings_snapshot WHERE account_code='ibkr' AND name='Test' ORDER BY as_of")
            rows=cur.fetchall()
            assert rows==[(previous,10,80,123),(DAY,0,0,0)]
            assert old['batch_id'] != exit_source['batch_id']
            cur.execute("""SELECT version_key,evidence->>'role',evidence->'source_ref'->'key'->>'source_item_id'
                FROM valuation_evidence v CROSS JOIN LATERAL jsonb_to_recordset(v.inputs) AS x(input_kind text,input_hash text,evidence jsonb) WHERE input_kind='source_item'
                  AND evidence->'source_ref'->'key'->>'source_item_id'=ANY(%s)""",
                ([old['position_item_ids'][0], exit_source['position_item_ids'][0]],))
            source_refs=cur.fetchall()
            identity_keys={row[0] for row in source_refs if row[1]=='ibkr_reviewed_contract_identity'
                           and row[2]==old['position_item_ids'][0]}
            exit_keys={row[0] for row in source_refs if row[1]=='ibkr_zero_position'
                       and row[2]==exit_source['position_item_ids'][0]}
            common=identity_keys & exit_keys
            assert len(common)==1
            cur.execute("""SELECT evidence->'snapshot'->'content' FROM valuation_evidence v CROSS JOIN LATERAL jsonb_to_recordset(v.inputs) AS x(input_kind text,input_hash text,evidence jsonb)
                WHERE version_key=%s AND input_kind='holding'""", (next(iter(common)),))
            snapshot=cur.fetchone()[0]
            assert snapshot['as_of']==DAY.isoformat() and snapshot['name']=='Test'
            assert all(Decimal(str(snapshot[field]))==0 for field in ('quantity','cost','market_value'))
            cur.execute("SELECT count(*),min(trade_id) FROM pams_investment_trade_fact WHERE source_table='ibkr_flex_trade'")
            count,trade_id=cur.fetchone(); assert count==1
            cur.execute('SELECT count(*) FROM source_files')
            assert cur.fetchone()[0]==2  # Previous report and current report, once each.
            cur.execute("""SELECT count(*) FROM source_items i JOIN source_batches b ON b.batch_id=i.batch_id
                WHERE b.workflow='ibkr_report' AND i.file_id IS NULL AND i.origin_file_id IS NOT NULL""")
            assert cur.fetchone()[0]==2
        from domain.brokers.ibkr.ibkr_fact_source_verification import EvidenceCache, verify_ibkr_fact
        conn.rollback()
        conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with conn.cursor() as cur:
            proof=verify_ibkr_fact(cur,trade_id=trade_id,cache=EvidenceCache())
            assert proof['status']=='partial' and not proof['source_authenticated'],proof


def test_large_bounded_xml_can_be_reviewed_and_reused(empty_source_db,monkeypatch):
    import main
    content=XML.replace(b'<OpenPositions>',b' '*(11*1024*1024)+b'<OpenPositions>')
    assert 10*1024*1024 < len(content) < 32*1024*1024
    with psycopg2.connect(**empty_source_db) as conn:
        setup(conn,monkeypatch,empty_source_db)
        with TestClient(main.app,base_url='http://pams.local') as web:
            body={'source_account_id':'U_TEST','business_date':DAY.isoformat(),'xml_text':content.decode()}
            response=web.post('/api/ibkr/holding-review/stage',headers=HEADERS,json=body)
            assert response.status_code==200,response.text
            source=response.json(); base='/api/ibkr/holding-review/'+source['batch_id']
            revision=web.get(base,headers=HEADERS).json()['revision']
            row=web.post(base+'/review',headers=HEADERS,json={'item_id':source['position_item_ids'][0],
                'expected_revision':revision,'note':'synthetic row'})
            assert row.status_code==200,row.text
            scope=web.post(base+'/review',headers=HEADERS,json={'item_id':source['scope_item_id'],
                'expected_revision':row.json()['revision'],'note':'synthetic scope','scope_complete':True})
            assert scope.status_code==200,scope.text
            assert web.post(base+'/publish',headers=HEADERS,json={'expected_revision':scope.json()['revision']}).status_code==200
            repeat=web.post('/api/ibkr/holding-review/stage',headers=HEADERS,json=body)
            assert repeat.status_code==200 and repeat.json()['origin_reused']
            oversized={'source_account_id':'U_TEST','business_date':DAY.isoformat(),'xml_text':' '*(32*1024*1024+1)}
            assert web.post('/api/ibkr/holding-review/stage',headers=HEADERS,json=oversized).status_code==422
        with conn.cursor() as cur:
            cur.execute('SELECT count(*),max(octet_length(content)) FROM source_files')
            count,size=cur.fetchone(); assert count==1 and size==len(content)


def test_reused_holding_original_supports_trade_cash_and_late_fx(empty_source_db,monkeypatch):
    import hashlib
    import main
    from collectors.ibkr_flex import parse_flex_report
    from domain.brokers.ibkr.ibkr_cash_events import import_cash_events
    from domain.brokers.ibkr.ibkr_report_batch import publish_report_batch
    from domain.brokers.ibkr.ibkr_fact_source_verification import EvidenceCache,verify_ibkr_fact
    from tests.database.test_ibkr_fact_source_verification import cash_report

    content=cash_report()
    position=re.search(rb'<OpenPosition\b[^>]*/>',content).group()
    content=content.replace(position,b'<OpenPositions>'+position+b'</OpenPositions>')
    digest=hashlib.sha256(content).hexdigest()
    with psycopg2.connect(**empty_source_db) as conn:
        setup(conn,monkeypatch,empty_source_db)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(base_currency,quote_currency,as_of,rate) VALUES ('CNY','USD',%s,7)",(DAY,))
        conn.commit()
        with TestClient(main.app,base_url='http://pams.local') as web:
            stage=web.post('/api/ibkr/holding-review/stage',headers=HEADERS,json={
                'source_account_id':'U_TEST','business_date':DAY.isoformat(),'xml_text':content.decode()}).json()
            base='/api/ibkr/holding-review/'+stage['batch_id']
            rev=web.get(base,headers=HEADERS).json()['revision']
            row=web.post(base+'/review',headers=HEADERS,json={'item_id':stage['position_item_ids'][0],
                'expected_revision':rev,'note':'synthetic row'}).json()
            scope=web.post(base+'/review',headers=HEADERS,json={'item_id':stage['scope_item_id'],
                'expected_revision':row['revision'],'note':'synthetic scope','scope_complete':True}).json()
            assert web.post(base+'/publish',headers=HEADERS,json={'expected_revision':scope['revision']}).status_code==200
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            first=publish_report_batch(cur,report_bytes=content,source_account_id='U_TEST',day=DAY,
                                       report_sha256=digest,cash_entries=[],cash_pending_count=1)
            assert first['written']==1
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(base_currency,quote_currency,as_of,rate,source) VALUES ('CNY','EUR',%s,8,'synthetic')",(DAY,))
        envelope=parse_flex_report(content.decode(),account_code='ibkr').cash_events[0]
        envelope['source_evidence']={'broker_source':'ibkr_flex','source_sha256':digest,'confirmed_day':DAY.isoformat()}
        events=import_cash_events(conn,[envelope],account_code='ibkr',cutoff=date(2026,1,1),
                                  statement_as_of=DAY,defer_source_link=True)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            retried=publish_report_batch(cur,report_bytes=content,source_account_id='U_TEST',day=DAY,
                report_sha256=digest,cash_entries=events['_deferred_source_events'])
            assert retried['replayed']
            cur.execute("SELECT source_table,trade_id FROM pams_investment_trade_fact")
            ids={row['source_table']:row['trade_id'] for row in cur.fetchall()}
            cur.execute('SELECT count(*) AS n FROM source_files'); assert cur.fetchone()['n']==1
            cur.execute("SELECT count(*) AS n FROM source_items WHERE origin_file_id IS NOT NULL")
            assert cur.fetchone()['n']==3  # report scope, trade, cash supplement
        conn.commit()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            distinct=content+b'\n'
            observed=publish_report_batch(cur,report_bytes=distinct,source_account_id='U_TEST',day=DAY,
                report_sha256=hashlib.sha256(distinct).hexdigest(),cash_entries=[],cash_pending_count=1)
            assert observed['existing_verified']==1 and observed['source_contributions']=='not_recorded'
        conn.commit()
        conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cache=EvidenceCache()
            for table in ('ibkr_flex_trade','ibkr_flex_cash_transaction'):
                proof=verify_ibkr_fact(cur,trade_id=ids[table],cache=cache)
                assert proof['status'] in {'partial','consistent'} and not proof['source_authenticated'],proof
            assert len(cache.files)==1 and len(cache.documents)==1
