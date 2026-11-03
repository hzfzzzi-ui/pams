from pathlib import Path
import psycopg2
import pytest
from tests.support.account_import_support import scratch_database

ROOT = Path(__file__).resolve().parents[2] / 'database/postgres/schema'


def install_redesign(cur):
    cur.execute('CREATE SCHEMA redesign_test')
    cur.execute('CREATE TABLE redesign_test.pams_accounts(account_code varchar(50) PRIMARY KEY)')
    cur.execute('CREATE TABLE redesign_test.pams_asset_master(asset_id varchar(120) PRIMARY KEY)')
    cur.execute("INSERT INTO redesign_test.pams_accounts VALUES ('bank-a'), ('bank-b')")
    for filename in ('079_source_records.sql', '080_source_items_redesign.sql', '081_fact_redesign.sql', '082_cashflow_classification.sql', '083_trade_review_redesign.sql', '077_confirmed_transfers.sql'):
        cur.execute((ROOT / filename).read_text(encoding='utf-8-sig').replace('public.', 'redesign_test.'))


def test_reviewed_source_and_account_are_required_before_fact():
    with scratch_database() as params, psycopg2.connect(**params) as conn, conn.cursor() as cur:
        install_redesign(cur)
        cur.execute("""INSERT INTO redesign_test.source_records(source_id,account_code,source_name,acquisition_method,document_type)
                    VALUES ('00000000-0000-0000-0000-000000000001','bank-a','Synthetic bank','manual','cash')""")
        cur.execute("""INSERT INTO redesign_test.source_items(source_item_id,source_id,parse_method,record_hash,review_status)
                    VALUES ('00000000-0000-0000-0000-000000000002','00000000-0000-0000-0000-000000000001','manual',%s,'pending')""", ('a'*64,))
        sql = """INSERT INTO redesign_test.pams_cashflow_fact(account_code,event_date,amount,currency,source_item_id,fact_role)
                 VALUES (%s,'2026-01-01',-10,'CNY','00000000-0000-0000-0000-000000000002','statement_cashflow')"""
        for account in ('bank-a','bank-b'):
            cur.execute('SAVEPOINT invalid')
            with pytest.raises(psycopg2.errors.CheckViolation): cur.execute(sql,(account,))
            cur.execute('ROLLBACK TO SAVEPOINT invalid')
        cur.execute("UPDATE redesign_test.source_items SET review_status='confirmed'")
        cur.execute('SAVEPOINT invalid')
        with pytest.raises(psycopg2.errors.CheckViolation): cur.execute(sql,('bank-b',))
        cur.execute('ROLLBACK TO SAVEPOINT invalid')
        cur.execute(sql,('bank-a',))
        cur.execute('SAVEPOINT duplicate')
        with pytest.raises(psycopg2.errors.UniqueViolation): cur.execute(sql,('bank-a',))
        cur.execute('ROLLBACK TO SAVEPOINT duplicate')
        cur.execute('SELECT amount FROM redesign_test.pams_cashflow_fact')
        assert str(cur.fetchone()[0]) == '-10.0000'
        cur.execute('SAVEPOINT draft_publish')
        with pytest.raises(psycopg2.errors.CheckViolation): cur.execute('SET CONSTRAINTS ALL IMMEDIATE')
        cur.execute('ROLLBACK TO SAVEPOINT draft_publish')
        cur.execute("UPDATE redesign_test.source_records SET status='published'")
        cur.execute('SET CONSTRAINTS ALL IMMEDIATE')
        cur.execute("INSERT INTO redesign_test.cashflow_classification(cashflow_id) SELECT cashflow_id FROM redesign_test.pams_cashflow_fact")
        cur.execute("SELECT status,cashflow_type FROM redesign_test.cashflow_classification")
        assert cur.fetchone() == ('pending', None)
        cur.execute('SAVEPOINT missing_classification')
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute("UPDATE redesign_test.cashflow_classification SET status='confirmed'")
        cur.execute('ROLLBACK TO SAVEPOINT missing_classification')
        cur.execute('SAVEPOINT locked_source')
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute("UPDATE redesign_test.source_items SET review_status='pending'")
        cur.execute('ROLLBACK TO SAVEPOINT locked_source')


def test_final_source_cannot_gain_or_lose_even_excluded_evidence():
    with scratch_database() as params, psycopg2.connect(**params) as conn, conn.cursor() as cur:
        install_redesign(cur)
        cur.execute("""INSERT INTO redesign_test.source_records(source_id,account_code,source_name,acquisition_method,document_type)
                    VALUES ('00000000-0000-0000-0000-000000000001','bank-a','Synthetic bank','manual','cash')""")
        cur.execute("""INSERT INTO redesign_test.source_items(source_item_id,source_id,parse_method,record_hash,review_status)
                    VALUES ('00000000-0000-0000-0000-000000000002','00000000-0000-0000-0000-000000000001','manual',%s,'excluded')""", ('a' * 64,))
        cur.execute("UPDATE redesign_test.source_records SET status='published'")
        for sql, args in [
            ("DELETE FROM redesign_test.source_items", ()),
            ("""INSERT INTO redesign_test.source_items(source_item_id,source_id,parse_method,record_hash)
                VALUES ('00000000-0000-0000-0000-000000000003','00000000-0000-0000-0000-000000000001','manual',%s)""", ('b' * 64,)),
        ]:
            cur.execute('SAVEPOINT immutable_membership')
            with pytest.raises(psycopg2.errors.CheckViolation):
                cur.execute(sql, args)
            cur.execute('ROLLBACK TO SAVEPOINT immutable_membership')


def test_source_writer_review_is_revision_checked_and_preserves_raw():
    from pathlib import Path
    import sys
    from uuid import UUID
    from psycopg2.extras import RealDictCursor
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend/api'))
    from domain.imports import source_records_v2 as source

    class SchemaCursor:
        def __init__(self, inner): self.inner = inner
        @property
        def description(self): return self.inner.description
        @property
        def connection(self): return self.inner.connection
        @property
        def rowcount(self): return self.inner.rowcount
        def execute(self, sql, args=None): return self.inner.execute(sql.replace('public.', 'redesign_test.'), args)
        def fetchone(self): return self.inner.fetchone()

    with scratch_database() as params, psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        install_redesign(cur)
        wrapped = SchemaCursor(cur)
        sid=UUID('00000000-0000-0000-0000-000000000011')
        iid=UUID('00000000-0000-0000-0000-000000000012')
        source.create_source(wrapped, source_id=sid, account_code='bank-a', source_name='Synthetic bank',
                             acquisition_method='file', document_type='cash', filename='sample.csv',
                             file_format='csv', content=b'synthetic')
        source.add_item(wrapped, source_id=sid, item_id=iid, parse_method='template',
                        raw_fields={'amount':'-10'}, parsed_fields={'amount':'-10'})
        assert source.review_item(wrapped, source_id=sid, item_id=iid, expected_revision=2,
                                  parsed_fields={'amount':'-10.00'}, review_status='confirmed') == 3
        with pytest.raises(ValueError, match='stale'):
            source.review_item(wrapped, source_id=sid, item_id=iid, expected_revision=2,
                               parsed_fields={'amount':'-20'}, review_status='confirmed')
        cur.execute('SELECT raw_fields,parsed_fields FROM redesign_test.source_items WHERE source_item_id=%s',(str(iid),))
        row=cur.fetchone()
        assert row['raw_fields']['amount']=='-10' and row['parsed_fields']['amount']=='-10.00'
        cur.execute('SAVEPOINT immutable')
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute("UPDATE redesign_test.source_items SET raw_fields='{}'::jsonb")
        cur.execute('ROLLBACK TO SAVEPOINT immutable')


def test_cash_publication_saves_fact_with_pending_classification_and_blocks_replay(monkeypatch):
    from pathlib import Path
    import sys
    from uuid import UUID
    from psycopg2.extras import RealDictCursor
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend/api'))
    from domain.imports import source_records_v2 as source
    class SchemaCursor:
        def __init__(self, inner): self.inner=inner
        @property
        def description(self): return self.inner.description
        @property
        def connection(self): return self.inner.connection
        @property
        def rowcount(self): return self.inner.rowcount
        def execute(self,sql,args=None): return self.inner.execute(sql.replace('public.','redesign_test.'),args)
        def fetchone(self): return self.inner.fetchone()
        def fetchall(self): return self.inner.fetchall()
    with scratch_database() as params, psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        install_redesign(cur); w=SchemaCursor(cur)
        sid=UUID('00000000-0000-0000-0000-000000000021');iid=UUID('00000000-0000-0000-0000-000000000022')
        source.create_source(w,source_id=sid,account_code='bank-a',source_name='Synthetic bank',acquisition_method='manual',document_type='cash')
        source.add_item(w,source_id=sid,item_id=iid,parse_method='manual',raw_fields={'amount':'-10'},parsed_fields={'business_date':'2026-01-01','net_cash':'-10','currency':'CNY'})
        source.review_item(w,source_id=sid,item_id=iid,expected_revision=2,parsed_fields={'business_date':'2026-01-01','net_cash':'-10','currency':'CNY'},review_status='confirmed')
        assert source.publish_cash_source(w,source_id=sid,expected_revision=3)=={'published':1,'classification':'pending'}
        cur.execute('SELECT f.amount,c.status,c.cashflow_type FROM redesign_test.pams_cashflow_fact f JOIN redesign_test.cashflow_classification c USING(cashflow_id)')
        row=cur.fetchone();assert str(row['amount'])=='-10.0000' and row['status']=='pending' and row['cashflow_type'] is None
        from domain.ledger import cashflow_classification as classification, cashflow_read_v2
        from datetime import date
        cur.execute('CREATE TABLE redesign_test.pams_fx_rates_daily(as_of date,base_currency text,quote_currency text,rate numeric,source text)')
        loaded=cashflow_read_v2.load_cash_rows(w,start=date(2026,1,1),end=date(2026,1,1),base_currency='CNY')
        assert len(loaded)==1 and loaded[0]['source_evidence_status']=='verified' and loaded[0]['classification_status']=='pending'
        from domain.ledger import ledger_view
        monkeypatch.setattr(ledger_view, '_table_exists', lambda cursor, table: table == 'source_records')
        displayed=ledger_view._load_cash_rows(w,start=date(2026,1,1),end=date(2026,1,1),base_currency='CNY')
        assert displayed[0]['review_status']=='confirmed'
        assert ledger_view.classify_ledger_row(displayed[0], set(), set())['flow_type']=='unclassified'
        cur.execute('SAVEPOINT changed_fact')
        cur.execute('UPDATE redesign_test.pams_cashflow_fact SET amount=-11')
        loaded=cashflow_read_v2.load_cash_rows(w,start=date(2026,1,1),end=date(2026,1,1),base_currency='CNY')
        assert loaded[0]['source_evidence_status']=='unknown' and loaded[0]['amount_cny'] is None
        cur.execute('ROLLBACK TO SAVEPOINT changed_fact')
        cur.execute('SELECT cashflow_id FROM redesign_test.pams_cashflow_fact');cash_id=cur.fetchone()['cashflow_id']
        assert classification.suggest(w,cashflow_id=cash_id,cashflow_type='operating_expense',source='rule')
        classification.confirm(w,cashflow_id=cash_id,cashflow_type='fee',note='reviewed')
        assert not classification.suggest(w,cashflow_id=cash_id,cashflow_type='investment_return_cash',source='rule')
        cur.execute('SELECT status,cashflow_type FROM redesign_test.cashflow_classification');result=cur.fetchone()
        assert result['status']=='confirmed' and result['cashflow_type']=='fee'
        with pytest.raises(ValueError): source.publish_cash_source(w,source_id=sid,expected_revision=3)
        cur.execute('SELECT count(*) AS n FROM redesign_test.pams_cashflow_fact');assert cur.fetchone()['n']==1


def test_cash_publication_rolls_back_if_any_reviewed_items_look_duplicate():
    from pathlib import Path
    import sys
    from uuid import UUID
    from psycopg2.extras import RealDictCursor
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend/api'))
    from domain.imports import source_records_v2 as source
    class SchemaCursor:
        def __init__(self,inner): self.inner=inner
        @property
        def description(self): return self.inner.description
        @property
        def connection(self): return self.inner.connection
        @property
        def rowcount(self): return self.inner.rowcount
        def execute(self,sql,args=None): return self.inner.execute(sql.replace('public.','redesign_test.'),args)
        def fetchone(self): return self.inner.fetchone()
        def fetchall(self): return self.inner.fetchall()
    with scratch_database() as params, psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        install_redesign(cur);w=SchemaCursor(cur)
        sid=UUID('00000000-0000-0000-0000-000000000031')
        source.create_source(w,source_id=sid,account_code='bank-a',source_name='Synthetic bank',acquisition_method='manual',document_type='cash')
        fields={'business_date':'2026-01-01','net_cash':'-10','currency':'CNY'}
        for n in (32,33):
            iid=UUID(int=n);source.add_item(w,source_id=sid,item_id=iid,parse_method='manual',raw_fields={'amount':'-10'},parsed_fields=fields)
        for n,revision in ((32,3),(33,4)):
            source.review_item(w,source_id=sid,item_id=UUID(int=n),expected_revision=revision,parsed_fields=fields,review_status='confirmed')
        with pytest.raises(ValueError,match='duplicate'):
            source.publish_cash_source(w,source_id=sid,expected_revision=5)
        cur.execute('SELECT count(*) AS n FROM redesign_test.pams_cashflow_fact');assert cur.fetchone()['n']==0
        cur.execute('SELECT status FROM redesign_test.source_records');assert cur.fetchone()['status']=='draft'


def test_trade_publication_preserves_original_amount_and_unknown_fees():
    from pathlib import Path
    import sys
    from uuid import UUID
    from psycopg2.extras import RealDictCursor
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend/api'))
    from domain.imports import source_records_v2 as source
    class SchemaCursor:
        def __init__(self,inner): self.inner=inner
        @property
        def description(self): return self.inner.description
        @property
        def connection(self): return self.inner.connection
        @property
        def rowcount(self): return self.inner.rowcount
        def execute(self,sql,args=None): return self.inner.execute(sql.replace('public.','redesign_test.'),args)
        def fetchone(self): return self.inner.fetchone()
        def fetchall(self): return self.inner.fetchall()
    with scratch_database() as params, psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        install_redesign(cur);w=SchemaCursor(cur)
        sid=UUID('00000000-0000-0000-0000-000000000041');iid=UUID('00000000-0000-0000-0000-000000000042')
        f={'business_date':'2026-01-01','trade_type':'buy','symbol':'000001','currency':'CNY','quantity':'3','price':'3.33','gross_amount':'10.00','net_cash':'-10.00'}
        source.create_source(w,source_id=sid,account_code='bank-a',source_name='Synthetic broker',acquisition_method='manual',document_type='trade')
        source.add_item(w,source_id=sid,item_id=iid,parse_method='manual',raw_fields=f,parsed_fields=f,source_uid='SYNTHETIC-1')
        source.review_item(w,source_id=sid,item_id=iid,expected_revision=2,parsed_fields=f,review_status='confirmed')
        assert source.publish_trade_source(w,source_id=sid,expected_revision=3)=={'published':1}
        cur.execute('SELECT gross_amount,fee_amount,quantity,price FROM redesign_test.pams_investment_trade_fact')
        row=cur.fetchone();assert row['gross_amount']==10 and row['fee_amount'] is None and row['quantity']*row['price']!=row['gross_amount']
        cur.execute('SET CONSTRAINTS ALL IMMEDIATE')


def test_partial_cash_publication_preserves_pending_and_both_duplicate_sides():
    from pathlib import Path
    import sys
    from uuid import UUID
    from psycopg2.extras import RealDictCursor
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend/api'))
    from domain.imports import source_records_v2 as source
    class SchemaCursor:
        def __init__(self,inner): self.inner=inner
        @property
        def description(self): return self.inner.description
        @property
        def connection(self): return self.inner.connection
        @property
        def rowcount(self): return self.inner.rowcount
        def execute(self,sql,args=None): return self.inner.execute(sql.replace('public.','redesign_test.'),args)
        def fetchone(self): return self.inner.fetchone()
        def fetchall(self): return self.inner.fetchall()
    with scratch_database() as params, psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        install_redesign(cur);w=SchemaCursor(cur);sid=UUID(int=51)
        source.create_source(w,source_id=sid,account_code='bank-a',source_name='Synthetic bank',acquisition_method='file',document_type='cash',filename='sample.csv',file_format='csv',content=b'original')
        fields=[]
        for n,amount in [(52,'-10'),(53,'-20'),(54,'-20')]:
            f={'business_date':'2026-01-01','net_cash':amount,'currency':'CNY'};fields.append(f)
            source.add_item(w,source_id=sid,item_id=UUID(int=n),parse_method='template',raw_fields={'amount':amount},parsed_fields=f)
        for n,revision,f in [(52,4,fields[0]),(53,5,fields[1])]:
            source.review_item(w,source_id=sid,item_id=UUID(int=n),expected_revision=revision,parsed_fields=f,review_status='confirmed')
        from domain.imports.cash_preview_v2 import preview_cash
        preview=preview_cash(w,source_id=sid,expected_revision=6)
        assert preview['counts']['ready']==1 and preview['counts']['duplicate']==1 and preview['counts']['pending']==1
        assert preview['can_publish_ready'] and not preview['can_publish']
        result=source.publish_ready_cash_source(w,source_id=sid,expected_revision=6)
        assert result['published']==1 and result['deferred_count']==2
        cur.execute('SELECT count(*) AS n FROM redesign_test.pams_cashflow_fact');assert cur.fetchone()['n']==1
        cur.execute('SELECT content,status FROM redesign_test.source_records WHERE source_id=%s',(result['deferred_source_id'],));deferred=cur.fetchone()
        assert bytes(deferred['content'])==b'original' and deferred['status']=='draft'
        cur.execute('SELECT raw_fields,source_position FROM redesign_test.source_items WHERE source_id=%s',(result['deferred_source_id'],));rows=cur.fetchall()
        assert len(rows)==2 and all(r['source_position']['deferred_from']['source_id']==str(sid) for r in rows)
        cur.execute('SELECT source_uid,source_position,parse_method,raw_fields,record_hash FROM redesign_test.source_items WHERE source_id=%s',(result['deferred_source_id'],))
        for item in cur.fetchall(): source._verify_deferred_source(w,result['deferred_source_id'],item)
        cur.execute('SET CONSTRAINTS ALL IMMEDIATE')


def test_transfer_confirmation_matches_independent_cash_and_trade_facts():
    from pathlib import Path
    import sys
    from uuid import UUID
    from psycopg2.extras import RealDictCursor
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend/api'))
    from domain.imports import source_records_v2 as source
    from domain.ledger import confirmed_transfers_v2 as transfers
    class SchemaCursor:
        def __init__(self,inner): self.inner=inner
        @property
        def connection(self): return self.inner.connection
        @property
        def rowcount(self): return self.inner.rowcount
        def execute(self,sql,args=None): return self.inner.execute(sql.replace('public.','redesign_test.'),args)
        def fetchone(self): return self.inner.fetchone()
        def fetchall(self): return self.inner.fetchall()
    with scratch_database() as params, psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        install_redesign(cur);w=SchemaCursor(cur)
        for source_n,item_n,account,kind,amount in [(61,62,'bank-a','cash','-10'),(63,64,'bank-b','trade','10')]:
            sid=UUID(int=source_n);iid=UUID(int=item_n)
            f={'business_date':'2026-01-01','net_cash':amount,'currency':'CNY'}
            if kind=='trade':f['trade_type']='deposit'
            source.create_source(w,source_id=sid,account_code=account,source_name='Synthetic',acquisition_method='manual',document_type=kind)
            source.add_item(w,source_id=sid,item_id=iid,parse_method='manual',raw_fields=f,parsed_fields=f)
            source.review_item(w,source_id=sid,item_id=iid,expected_revision=2,parsed_fields=f,review_status='confirmed')
            (source.publish_cash_source if kind=='cash' else source.publish_trade_source)(w,source_id=sid,expected_revision=3)
        cur.execute('SELECT cashflow_id FROM redesign_test.pams_cashflow_fact');cash=cur.fetchone()['cashflow_id']
        cur.execute('SELECT trade_id FROM redesign_test.pams_investment_trade_fact');trade=cur.fetchone()['trade_id']
        result=transfers.confirm(w,from_kind='cashflow',from_id=cash,to_kind='trade',to_id=trade,note='checked',confirmed=True)
        assert result['transfer_id']
        replay=transfers.confirm(w,from_kind='cashflow',from_id=cash,to_kind='trade',to_id=trade,note='checked',confirmed=True)
        assert replay['replayed'] is True and replay['transfer_id'] == result['transfer_id']
        with pytest.raises(ValueError,match='already confirmed'):
            transfers.confirm(w,from_kind='cashflow',from_id=cash,to_kind='trade',to_id=trade,note='different',confirmed=True)
        cur.execute('SELECT count(*) AS n FROM redesign_test.evidence_confirmation');assert cur.fetchone()['n']==1
        cur.execute('SET CONSTRAINTS ALL IMMEDIATE')
        assert transfers.read(w)[0]['evidence_status'] == 'confirmed'
        cur.execute('UPDATE redesign_test.pams_cashflow_fact SET amount=-11 WHERE cashflow_id=%s', (cash,))
        assert transfers.read(w)[0]['evidence_status'] == 'invalidated'


def test_synthetic_ccb_file_stages_as_one_draft_and_retains_original(monkeypatch):
    from pathlib import Path
    import sys
    from psycopg2.extras import RealDictCursor
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend/api'))
    from domain.imports import account_import_statement as statement, document_stage_v2 as staging
    class SchemaCursor:
        def __init__(self,inner): self.inner=inner
        @property
        def connection(self): return self.inner.connection
        @property
        def rowcount(self): return self.inner.rowcount
        def execute(self,sql,args=None): return self.inner.execute(sql.replace('public.','redesign_test.'),args)
        def fetchone(self): return self.inner.fetchone()
        def fetchall(self): return self.inner.fetchall()
    data=('中国建设银行信用卡交易明细\n交易日,入账日,信用卡卡号,类型,入账币种,入账金额,交易描述\n'
          '20260101,20260102,****1234,消费,人民币,20.00,示例商户\n'
          '20260103,20260104,****1234,存入,人民币,-10.00,还款\n').encode('gb18030')
    monkeypatch.setattr(statement,'profiles',lambda *args:{'ccb_test_1234':[{'statement_template':'ccb_credit_pdf'}]})
    with scratch_database() as params, psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        install_redesign(cur);w=SchemaCursor(cur)
        cur.execute("INSERT INTO redesign_test.pams_accounts VALUES ('ccb_test_1234')")
        result=staging.stage_document(w,account_code='ccb_test_1234',source_name='Synthetic CCB',filename='sample.csv',content=data)
        assert result['row_count']==2 and result['revision']==3 and result['template_status']=='matched'
        cur.execute('SELECT status,content FROM redesign_test.source_records WHERE source_id=%s',(result['source_id'],))
        row=cur.fetchone();assert row['status']=='draft' and bytes(row['content'])==data
        cur.execute('SELECT parsed_fields,review_status FROM redesign_test.source_items ORDER BY parsed_fields->>\'business_date\'')
        rows=cur.fetchall();assert len(rows)==2 and all(x['review_status']=='pending' for x in rows)
        assert rows[0]['parsed_fields']['net_cash']=='-20.00' and rows[0]['parsed_fields']['posting_date']=='2026-01-02'
        assert rows[1]['parsed_fields']['net_cash']=='10.00'
        with pytest.raises(ValueError,match='already exists'):
            staging.stage_document(w,account_code='ccb_test_1234',source_name='Synthetic CCB',filename='sample.csv',content=data)
        cur.execute('SELECT count(*) AS n FROM redesign_test.pams_cashflow_fact');assert cur.fetchone()['n']==0
        from domain.imports import source_records_v2 as new_source
        from domain.imports import cash_publication_v2, native_source, native_source_http
        from uuid import UUID
        cur.execute("SELECT source_item_id,parsed_fields FROM redesign_test.source_items ORDER BY parsed_fields->>'business_date'")
        items=cur.fetchall(); revision=result['revision']
        for item in items:
            revision=new_source.review_item(w,source_id=UUID(result['source_id']),item_id=item['source_item_id'],
                expected_revision=revision,parsed_fields=item['parsed_fields'],review_status='confirmed')
        monkeypatch.setattr(native_source_http, '_token_key', lambda: b'synthetic-test-key')
        monkeypatch.setattr(native_source, 'enabled', lambda: True)
        preview=cash_publication_v2.preview(w,source_id=UUID(result['source_id']),revision=revision,mode='all')
        assert preview['counts']['ready']==2 and preview['can_publish']
        with pytest.raises(ValueError,match='preview expired or source changed'):
            cash_publication_v2.publish(w,source_id=UUID(result['source_id']),revision=revision,mode='all',preview_token='0'*64)
        published=cash_publication_v2.publish(w,source_id=UUID(result['source_id']),revision=revision,mode='all',preview_token=preview['preview_token'])
        assert published['published']==2
        cur.execute("SELECT f.amount,f.posting_date,c.status,c.cashflow_type FROM redesign_test.pams_cashflow_fact f JOIN redesign_test.cashflow_classification c USING(cashflow_id) ORDER BY f.event_date")
        facts=cur.fetchall();assert [str(f['amount']) for f in facts]==['-20.0000','10.0000']
        assert str(facts[0]['posting_date'])=='2026-01-02' and all(f['status']=='pending' and f['cashflow_type'] is None for f in facts)
        cur.execute('SET CONSTRAINTS ALL IMMEDIATE')


def test_synthetic_broker_document_preserves_original_execution_amounts(monkeypatch):
    from uuid import UUID
    from psycopg2.extras import RealDictCursor
    from tests.imports.test_account_import_ths import event, export
    from domain.imports import document_stage_v2, source_records_v2
    from domain.imports import native_source, native_source_http, trade_publication_v2
    from domain.investments.trade_read_v2 import calculate_verified_positions, load_trade_rows
    from datetime import date, datetime
    from decimal import Decimal
    from zoneinfo import ZoneInfo
    from domain.investments.trade_snapshot_preview import build_candidates_from_source_records

    class SchemaCursor:
        def __init__(self, inner): self.inner = inner
        @property
        def description(self): return self.inner.description
        @property
        def connection(self): return self.inner.connection
        @property
        def rowcount(self): return self.inner.rowcount
        def execute(self, sql, args=None): return self.inner.execute(sql.replace('public.', 'redesign_test.'), args)
        def fetchone(self): return self.inner.fetchone()
        def fetchall(self): return self.inner.fetchall()

    data = export(event())
    with scratch_database() as params, psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        install_redesign(cur)
        cur.execute("INSERT INTO redesign_test.pams_accounts VALUES ('tonghuashun')")
        wrapped = SchemaCursor(cur)
        result = document_stage_v2.stage_document(wrapped, account_code='tonghuashun', source_name='Synthetic broker', filename='table.xls', content=data)
        assert result['document_type'] == 'trade' and result['row_count'] == 1
        cur.execute('SELECT source_item_id, parsed_fields FROM redesign_test.source_items')
        item = cur.fetchone()
        revision = source_records_v2.review_item(wrapped, source_id=UUID(result['source_id']), item_id=item['source_item_id'],
            expected_revision=result['revision'], parsed_fields=item['parsed_fields'], review_status='confirmed')
        monkeypatch.setattr(native_source_http, '_token_key', lambda: b'synthetic-test-key')
        monkeypatch.setattr(native_source, 'enabled', lambda: True)
        preview = trade_publication_v2.preview(wrapped, source_id=UUID(result['source_id']), revision=revision)
        assert preview['items'][0]['gross_amount'] == '1000.000'
        cur.execute('SELECT count(*) AS n FROM redesign_test.pams_investment_trade_fact')
        assert cur.fetchone()['n'] == 0
        with pytest.raises(ValueError, match='preview expired or source changed'):
            trade_publication_v2.publish(wrapped, source_id=UUID(result['source_id']), revision=revision, preview_token='0'*64)
        assert trade_publication_v2.publish(wrapped, source_id=UUID(result['source_id']), revision=revision,
                                            preview_token=preview['preview_token'])['published'] == 1
        cur.execute('SELECT symbol, quantity, gross_amount, net_cash, fee_amount, tax_amount FROM redesign_test.pams_investment_trade_fact')
        fact = cur.fetchone()
        assert fact['symbol'] == '000001'
        assert str(fact['quantity']) == '100.0000000000'
        assert str(fact['gross_amount']) == '1000.0000000000'
        assert str(fact['net_cash']) == '-1003.1000000000'
        assert str(fact['fee_amount']) == '3.1000000000' and str(fact['tax_amount']) == '0E-10'
        cur.execute('SET CONSTRAINTS ALL IMMEDIATE')
        kwargs = {'account_code': 'tonghuashun', 'start': date(2025, 1, 1), 'end': date(2025, 12, 31)}
        rows = load_trade_rows(wrapped, **kwargs)
        assert len(rows) == 1 and rows[0]['source_evidence_status'] == 'verified'
        with pytest.raises(ValueError, match='coverage'):
            calculate_verified_positions(wrapped, **kwargs, coverage_confirmed=False)
        positions = calculate_verified_positions(wrapped, **kwargs, coverage_confirmed=True)
        assert len(positions) == 1 and str(positions[0]['quantity']) == '100.0000000000'
        assert positions[0]['remaining_cost_price'] == positions[0]['diluted_cost_price']
        as_of = date(2025, 10, 20)
        inputs = {'account_code': 'tonghuashun', 'start': date(2025, 1, 1), 'as_of': as_of,
                  'identities': {'000001': {'symbol': '000001', 'asset_id': 'synthetic', 'name': 'Synthetic stock'}},
                  'quotes': {'000001': {'symbol': 'sz000001', 'price_date': as_of,
                      'price_time': datetime(2025, 10, 20, 15, 0, tzinfo=ZoneInfo('Asia/Shanghai')),
                      'source': 'synthetic', 'price': Decimal('10')}}, 'coverage_confirmed': True}
        candidates = build_candidates_from_source_records(wrapped, **inputs)
        assert len(candidates) == 1 and candidates[0]['market_value'] == Decimal('1000')
        cur.execute('UPDATE redesign_test.pams_investment_trade_fact SET net_cash=-1')
        rows = load_trade_rows(wrapped, **kwargs)
        assert rows[0]['source_evidence_status'] == 'unknown'
        with pytest.raises(ValueError, match='evidence'):
            calculate_verified_positions(wrapped, **kwargs, coverage_confirmed=True)
        with pytest.raises(ValueError, match='evidence'):
            build_candidates_from_source_records(wrapped, **inputs)
