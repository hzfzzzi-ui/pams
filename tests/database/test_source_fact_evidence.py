"""Reviewed native source/fact consistency against a disposable PG database."""
# ruff: noqa: F811
import psycopg2
import pytest
from psycopg2.extras import RealDictCursor
from tests.database.test_native_source_http import isolated  # noqa: F401
from tests.imports.test_account_import_publish import DAY, client, draft, preview, publish, trade, sql  # noqa: F401
from domain.imports.source_fact_evidence import verify_native_fact


def verify(params, table, identifier):
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        return verify_native_fact(cur,table=table,fact_id=identifier)


def test_trade_source_matches_reviewed_fact_and_wrong_amount_is_rejected(isolated):
    web, params = isolated
    batch=draft(web,[trade()])
    check=preview(web,batch)
    response=publish(web,batch,check)
    assert response.status_code==200,response.text
    fact=sql(params,'SELECT trade_id FROM pams_investment_trade_fact')[0]['trade_id']
    assert verify(params,'pams_investment_trade_fact',fact)['status']=='verified', verify(params,'pams_investment_trade_fact',fact)
    from datetime import date, timedelta
    from domain.accounts.account_balance import _cash_delta_cny
    sql(params,'CREATE TABLE IF NOT EXISTS pams_fx_rates_daily (as_of date,base_currency text,quote_currency text,rate numeric,source text)')
    with psycopg2.connect(**params) as conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
        assert _cash_delta_cny(cur,'tonghuashun',date.fromisoformat(DAY)-timedelta(days=1),date.fromisoformat(DAY)) == -1005
    sql(params,'UPDATE pams_investment_trade_fact SET net_cash=-1004 WHERE trade_id=%s',(fact,))
    assert verify(params,'pams_investment_trade_fact',fact)['status']=='inconsistent'
    with psycopg2.connect(**params) as conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
        assert _cash_delta_cny(cur,'tonghuashun',date.fromisoformat(DAY)-timedelta(days=1),date.fromisoformat(DAY)) is None


@pytest.mark.parametrize('invalid_action', [None, '', [], {}])
def test_malformed_source_action_is_unknown_even_with_matching_snapshot(isolated, invalid_action):
    """A self-consistent but malformed stored snapshot must not raise or verify."""
    from psycopg2.extras import Json
    from domain.imports.source_fact_evidence import _digest
    web, params = isolated
    batch = draft(web, [trade()])
    response = publish(web, batch, preview(web, batch))
    assert response.status_code == 200, response.text
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute('ALTER TABLE source_items DISABLE TRIGGER USER')
        cur.execute("UPDATE source_items SET parsed_fields=jsonb_set(parsed_fields, '{fields,action}', %s::jsonb) WHERE batch_id=%s",
                    (Json(invalid_action), batch['batch_id']))
        cur.execute('ALTER TABLE source_items ENABLE TRIGGER USER')
        cur.execute('SELECT * FROM source_batches WHERE batch_id=%s', (batch['batch_id'],))
        source_batch = dict(cur.fetchone())
        cur.execute('''SELECT file_id,filename,file_format,content_type,sha256,current_metadata,review_fingerprint
                       FROM source_files WHERE batch_id=%s ORDER BY file_id''', (batch['batch_id'],))
        files = [dict(row) for row in cur.fetchall()]
        cur.execute('''SELECT source_item_id,source_order,file_id,origin_file_id,source_uid,external_id,location,
                       parse_method,raw_fields,parsed_fields,record_hash,review_status,review_note
                       FROM source_items WHERE batch_id=%s ORDER BY source_order''', (batch['batch_id'],))
        items = [dict(row) for row in cur.fetchall()]
        keys = ('batch_id','account_code','business_type','workflow','institution','acquisition_method',
                'date_from','date_to','completeness','scope_attestation','status','revision')
        snapshot_batch = {key: source_batch[key] for key in keys}
        snapshot_batch['status'] = 'draft'
        digest = _digest({'batch': snapshot_batch, 'files': files, 'items': items, 'blockers': []})
        cur.execute('ALTER TABLE source_batches DISABLE TRIGGER USER')
        cur.execute('UPDATE source_batches SET published_source_hash=%s WHERE batch_id=%s', (digest, batch['batch_id']))
        cur.execute('ALTER TABLE source_batches ENABLE TRIGGER USER')
        cur.execute('SELECT trade_id FROM pams_investment_trade_fact')
        fact_id = cur.fetchone()['trade_id']
    assert verify(params, 'pams_investment_trade_fact', fact_id) == {
        'status': 'unknown', 'reason': 'source_action_missing'}


def test_derived_bank_leg_and_primary_trade_share_reviewed_source(isolated):
    web, params=isolated
    fields={'kind':'cash','business_date':'2026-10-29','currency':'CNY','action':'deposit',
            'net_cash':'500','counter_account_code':'bank_test'}
    batch=draft(web,[fields])
    check=preview(web,batch)
    assert check['can_publish'],check
    response=publish(web,batch,check)
    assert response.status_code==200,response.text
    tid=sql(params,'SELECT trade_id FROM pams_investment_trade_fact')[0]['trade_id']
    cid=sql(params,'SELECT cashflow_id FROM pams_cashflow_fact')[0]['cashflow_id']
    assert verify(params,'pams_investment_trade_fact',tid)['status']=='verified'
    leg=verify(params,'pams_cashflow_fact',cid)
    assert leg['status']=='verified' and leg['source_role']=='derived_bank_leg'
    assert leg['external_authority_verified'] is False
    sql(params,'UPDATE pams_cashflow_fact SET amount=-501 WHERE cashflow_id=%s',(cid,))
    assert verify(params,'pams_cashflow_fact',cid)['status']=='inconsistent'


from domain.imports import account_import_store as store
from domain.imports import account_import_statement as statement


@pytest.mark.parametrize('column,value', [('currency','USD'),('trade_date','2026-10-28'),('trade_type','sell'),
    ('quantity','101'),('gross_amount','999'),('fee_amount','6'),('tax_amount','0'),('symbol','000002'),('side','sell'),('net_cash_cny','-1'),('fx_rate_to_cny','2')])
def test_trade_fact_field_mismatch_is_never_verified(isolated,column,value):
    web,params=isolated
    batch=draft(web,[trade()]); response=publish(web,batch,preview(web,batch))
    assert response.status_code==200,response.text
    fact=sql(params,'SELECT trade_id FROM pams_investment_trade_fact')[0]['trade_id']
    assert verify(params,'pams_investment_trade_fact',fact)['status']=='verified', verify(params,'pams_investment_trade_fact',fact)
    sql(params,f'UPDATE pams_investment_trade_fact SET {column}=%s WHERE trade_id=%s',(value,fact))
    assert verify(params,'pams_investment_trade_fact',fact)['status']!='verified'


def test_direct_bank_csv_source_amount_and_account_must_match(isolated,monkeypatch):
    web,params=isolated
    monkeypatch.setattr(statement,'config',lambda:{'profiles':{'test':{'account_code':'bank_test',
        'profile_code':'test','statement_template':'manual_statement_csv','currency':'CNY'}},'rules':[],'amount_rules':[]})
    batch=web.post('/api/account-import/batches',json={'account_code':'bank_test','mode':'upload','kind':'cash'}).json()
    path=f"/api/account-import/batches/{batch['batch_id']}"
    data=b'transaction_date,amount,currency,balance,summary\n2026-10-29,100.05,CNY,200.05,pay\n'
    batch=web.post(path+'/files?filename=bank.csv',content=data).json()
    batch=web.put(path,json={'revision':batch['revision'],'rows':batch['rows']}).json()
    check=preview(web,batch)
    assert check['can_publish'],check
    response=publish(web,batch,check)
    assert response.status_code==200,response.text
    cid=sql(params,'SELECT cashflow_id FROM pams_cashflow_fact')[0]['cashflow_id']
    assert verify(params,'pams_cashflow_fact',cid)['status']=='verified'
    from datetime import date
    from decimal import Decimal
    from domain.ledger.ledger_view import _load_cash_rows
    sql(params,'ALTER TABLE pams_cashflow_fact ADD COLUMN IF NOT EXISTS source_channel text')
    with psycopg2.connect(**params) as conn:
        conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            rows=_load_cash_rows(cur,start=date(2026,10,29),end=date(2026,10,29),base_currency='CNY')
            assert len(rows)==1 and rows[0]['source_evidence_status']=='verified' and rows[0]['amount_cny']==Decimal('100.05')
    sql(params,'UPDATE pams_cashflow_fact SET amount=101 WHERE cashflow_id=%s',(cid,))
    with psycopg2.connect(**params) as conn:
        conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            rows=_load_cash_rows(cur,start=date(2026,10,29),end=date(2026,10,29),base_currency='CNY')
            assert len(rows)==1 and rows[0]['amount_cny'] is None and rows[0]['source_evidence_status']=='inconsistent'
    sql(params,'UPDATE pams_cashflow_fact SET amount=100.05 WHERE cashflow_id=%s',(cid,))
    with psycopg2.connect(**params) as conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
        from domain.imports.source_fact_evidence import NativeFactVerifier
        verifier=NativeFactVerifier(cur)
        assert verifier.verify(table='pams_cashflow_fact',fact_id=cid)['status']=='verified'
        cur.execute('SAVEPOINT tamper')
        cur.execute('ALTER TABLE source_files DISABLE TRIGGER USER')
        cur.execute("UPDATE source_files SET content=%s",(b'changed synthetic file',))
        assert verifier.verify(table='pams_cashflow_fact',fact_id=cid)['reason']=='source_snapshot_mismatch'
        cur.execute('ROLLBACK TO SAVEPOINT tamper')
        assert verifier.verify(table='pams_cashflow_fact',fact_id=cid)['status']=='verified'
    sql(params,'UPDATE pams_cashflow_fact SET event_date=%s WHERE cashflow_id=%s',('2026-10-28',cid))
    assert verify(params,'pams_cashflow_fact',cid)['status']=='inconsistent'


@pytest.mark.parametrize('origin,filename',[('file','trades.csv'),('ocr','screen.png')])
def test_reviewed_uploaded_trade_source_is_proven(isolated,monkeypatch,origin,filename):
    web,params=isolated
    monkeypatch.setattr(store,'parse',lambda content,*args:{'origin':origin,'rows':[
        {'fields':trade(),'source':{'row':1},'issues':[]}]})
    batch=web.post('/api/account-import/batches',json={'account_code':'tonghuashun','mode':'upload','kind':'trade'}).json()
    path=f"/api/account-import/batches/{batch['batch_id']}"
    batch=web.post(path+'/files?filename='+filename,content=b'synthetic').json()
    if origin=='ocr':
        batch=web.post(path+'/files/'+batch['files'][0]['file_id']+'/review',json={'revision':batch['revision']}).json()
    else:
        batch=web.put(path,json={'revision':batch['revision'],'rows':batch['rows']}).json()
    check=preview(web,batch); assert check['can_publish'],check
    response=publish(web,batch,check); assert response.status_code==200,response.text
    identifier=sql(params,'SELECT trade_id FROM pams_investment_trade_fact')[0]['trade_id']
    assert verify(params,'pams_investment_trade_fact',identifier)['status']=='verified'


def test_equal_amount_different_reviewed_sources_are_not_interchangeable(isolated):
    web,params=isolated
    for name in ('first','second'):
        batch=draft(web,[trade(source_ref=name,duplicate_decision='distinct')])
        check=preview(web,batch); assert check['can_publish'],check
        response=publish(web,batch,check); assert response.status_code==200,response.text
    rows=sql(params,'SELECT trade_id,source_item_id FROM pams_investment_trade_fact ORDER BY trade_id')
    assert len(rows)==2 and str(rows[0]['source_item_id']) != str(rows[1]['source_item_id'])
    assert all(verify(params,'pams_investment_trade_fact',row['trade_id'])['status']=='verified' for row in rows)
    with psycopg2.connect(**params) as conn,conn.cursor() as cur:
        cur.execute('SAVEPOINT bad_link')
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute('UPDATE pams_investment_trade_fact SET source_item_id=%s WHERE trade_id=%s',
                        (rows[1]['source_item_id'],rows[0]['trade_id']))
        cur.execute('ROLLBACK TO SAVEPOINT bad_link')


def test_real_postgres_readonly_repeatable_read_consistency_and_batch_cache(isolated,monkeypatch):
    from domain.imports import source_fact_evidence as evidence
    web,params=isolated
    batch=draft(web,[trade(source_ref='one'),trade(source_ref='two',quantity='200',gross_amount='2000',net_cash='-2005')])
    check=preview(web,batch); assert check['can_publish'],check
    response=publish(web,batch,check); assert response.status_code==200,response.text
    identifiers=[row['trade_id'] for row in sql(params,'SELECT trade_id FROM pams_investment_trade_fact ORDER BY trade_id')]
    count={'snapshots':0}
    original=evidence._digest
    def count_snapshot(value):
        if isinstance(value,dict) and set(value)=={'batch','files','items','blockers'}:
            count['snapshots']+=1
        return original(value)
    monkeypatch.setattr(evidence,'_digest',count_snapshot)
    with psycopg2.connect(**params) as conn:
        conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            verifier=evidence.NativeFactVerifier(cur)
            assert all(verifier.verify(table='pams_investment_trade_fact',fact_id=tid)['status']=='verified' for tid in identifiers)
            assert count['snapshots']==1
            # MVCC preserves a consistent transaction snapshot even if a fact is changed concurrently.
            sql(params,'UPDATE pams_investment_trade_fact SET net_cash=-1004 WHERE trade_id=%s',(identifiers[0],))
            assert verifier.verify(table='pams_investment_trade_fact',fact_id=identifiers[0])['status']=='verified'
    with psycopg2.connect(**params) as conn:
        conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            assert evidence.NativeFactVerifier(cur).verify(table='pams_investment_trade_fact',fact_id=identifiers[0])['status']=='inconsistent'
    with psycopg2.connect(**params) as conn:
        conn.set_session(readonly=True,isolation_level='READ COMMITTED')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            result=evidence.NativeFactVerifier(cur).verify(table='pams_investment_trade_fact',fact_id=identifiers[1])
            assert result=={'status':'unknown','reason':'consistent_snapshot_required'}


def test_readonly_cache_does_not_survive_commit_or_rollback(isolated,monkeypatch):
    from domain.imports import source_fact_evidence as evidence
    web,params=isolated
    batch=draft(web,[trade()]); check=preview(web,batch)
    response=publish(web,batch,check); assert response.status_code==200,response.text
    identifier=sql(params,'SELECT trade_id FROM pams_investment_trade_fact')[0]['trade_id']
    count={'snapshots':0}; original=evidence._digest
    def count_snapshot(value):
        if isinstance(value,dict) and set(value)=={'batch','files','items','blockers'}:
            count['snapshots']+=1
        return original(value)
    monkeypatch.setattr(evidence,'_digest',count_snapshot)
    with psycopg2.connect(**params) as conn:
        conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            verifier=evidence.NativeFactVerifier(cur)
            for operation in ('commit','rollback','commit'):
                assert verifier.verify(table='pams_investment_trade_fact',fact_id=identifier)['status']=='verified'
                getattr(conn,operation)()
    assert count['snapshots']==3


def test_readonly_cached_publication_is_rechecked_after_commit(isolated):
    from domain.imports.source_fact_evidence import NativeFactVerifier
    web,params=isolated
    batch=draft(web,[trade()]); response=publish(web,batch,preview(web,batch))
    assert response.status_code==200,response.text
    identifier=sql(params,'SELECT trade_id FROM pams_investment_trade_fact')[0]['trade_id']
    with psycopg2.connect(**params) as reader:
        reader.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with reader.cursor(cursor_factory=RealDictCursor) as cur:
            verifier=NativeFactVerifier(cur)
            assert verifier.verify(table='pams_investment_trade_fact',fact_id=identifier)['status']=='verified'
            with psycopg2.connect(**params) as writer,writer.cursor() as w:
                w.execute('ALTER TABLE source_batches DISABLE TRIGGER USER')
                w.execute("UPDATE source_batches SET published_result=jsonb_set(published_result,'{records}','[]'::jsonb)")
                w.execute('ALTER TABLE source_batches ENABLE TRIGGER USER')
            # Old snapshot remains internally consistent until the transaction ends.
            assert verifier.verify(table='pams_investment_trade_fact',fact_id=identifier)['status']=='verified'
            reader.commit()
            assert verifier.verify(table='pams_investment_trade_fact',fact_id=identifier)['status']=='inconsistent'


def test_batched_verification_fails_closed_if_transaction_changes(isolated,monkeypatch):
    from domain.imports.source_fact_evidence import NativeFactVerifier
    web,params=isolated
    batch=draft(web,[trade()]); response=publish(web,batch,preview(web,batch))
    assert response.status_code==200,response.text
    identifier=sql(params,'SELECT trade_id FROM pams_investment_trade_fact')[0]['trade_id']
    with psycopg2.connect(**params) as conn:
        conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            verifier=NativeFactVerifier(cur)
            actual=verifier._readonly_key()
            states=iter([actual,('changed-backend','changed-transaction')])
            monkeypatch.setattr(verifier,'_readonly_key',lambda:next(states))
            proof=verifier.verify_many(table='pams_investment_trade_fact',fact_ids=[identifier])
            assert proof[identifier]=={'status':'unknown','reason':'transaction_changed_during_verification'}
            assert verifier._proof_cache == {}


def test_writable_verifier_does_not_wait_on_account_lock(isolated):
    web,params=isolated
    batch=draft(web,[trade()]); response=publish(web,batch,preview(web,batch))
    assert response.status_code==200,response.text
    identifier=sql(params,'SELECT trade_id FROM pams_investment_trade_fact')[0]['trade_id']
    with psycopg2.connect(**params) as owner, owner.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended('account-import:tonghuashun',0))")
        assert verify(params,'pams_investment_trade_fact',identifier)=={'status':'unknown','reason':'account_lock_busy'}


def test_alias_mapping_requires_consistent_snapshot(isolated):
    web,params=isolated
    sql(params,"INSERT INTO pams_asset_alias(asset_id,alias_type,alias_value) VALUES ('TEST_STOCK','symbol','SYN-ALIAS')")
    batch=draft(web,[trade(symbol='SYN-ALIAS')]); check=preview(web,batch)
    assert check['can_publish'],check
    response=publish(web,batch,check); assert response.status_code==200,response.text
    identifier=sql(params,'SELECT trade_id FROM pams_investment_trade_fact')[0]['trade_id']
    assert verify(params,'pams_investment_trade_fact',identifier)=={'status':'unknown','reason':'asset_mapping_snapshot_required'}
    with psycopg2.connect(**params) as conn:
        conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            assert verify_native_fact(cur,table='pams_investment_trade_fact',fact_id=identifier)['status']=='verified'


def test_native_verifier_supports_plain_cursor_and_readonly_snapshot(isolated):
    web,params=isolated
    batch=draft(web,[trade()]); response=publish(web,batch,preview(web,batch))
    assert response.status_code==200,response.text
    identifier=sql(params,'SELECT trade_id FROM pams_investment_trade_fact')[0]['trade_id']
    with psycopg2.connect(**params) as conn:
        conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with conn.cursor() as cur:
            assert verify_native_fact(cur,table='pams_investment_trade_fact',fact_id=identifier)['status']=='verified'
