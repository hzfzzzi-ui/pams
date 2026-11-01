"""Explicit broker account mapping in a disposable, server-read-only database."""
# ruff: noqa: F811
from datetime import date
import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi.testclient import TestClient
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from tests.database.test_ibkr_holding_review_http import setup, HEADERS
from tests.database.test_ibkr_holding_identity_review import XML, DAY
from domain.investments.valuation_nav import daily_close_account_identity
from domain.investments import portfolio_state
from tests.brokers.ibkr.test_ibkr_daily_close_consumers import publish, balance, ACCOUNT
from decimal import Decimal


def test_account_mapping_requires_separate_review_and_published_evidence(empty_source_db, monkeypatch, tmp_path):
    import main
    with psycopg2.connect(**empty_source_db) as conn:
        setup(conn, monkeypatch, empty_source_db)
        with TestClient(main.app, base_url='http://pams.local') as web:
            staged=web.post('/api/ibkr/holding-review/stage', headers=HEADERS, json={
                'source_account_id':ACCOUNT, 'business_date':DAY.isoformat(), 'xml_text':XML.decode().replace('U_TEST', ACCOUNT)})
            assert staged.status_code == 200, staged.text
            source=staged.json(); base='/api/ibkr/holding-review/'+source['batch_id']
            rev=web.get(base,headers=HEADERS).json()['revision']
            body={'source_account_id':ACCOUNT,'expected_revision':rev,'note':'synthetic explicit mapping'}
            assert web.post(base+'/account-binding',headers=HEADERS,json=body).status_code == 409
            row=web.post(base+'/review',headers=HEADERS,json={'item_id':source['position_item_ids'][0],
                'expected_revision':rev,'note':'synthetic row'})
            scope=web.post(base+'/review',headers=HEADERS,json={'item_id':source['scope_item_id'],
                'expected_revision':row.json()['revision'],'note':'synthetic scope','scope_complete':True})
            assert scope.status_code == 200, scope.text
            body['expected_revision']=scope.json()['revision']
            assert web.post(base+'/account-binding',headers=HEADERS,json={**body,'source_account_id':'U_OTHER'}).status_code == 409
            bound=web.post(base+'/account-binding',headers=HEADERS,json=body)
            assert bound.status_code == 200, bound.text
            assert bound.json()['account_authentication'] == 'unknown'
            assert web.post(base+'/account-binding',headers=HEADERS,json=body).status_code == 409
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                assert daily_close_account_identity(cur, source_account_id=ACCOUNT, as_of=DAY)['status']=='unconfirmed'
            conn.rollback()
            done=web.post(base+'/publish',headers=HEADERS,json={'expected_revision':bound.json()['revision']})
            assert done.status_code == 200, done.text
        with conn.cursor() as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(base_currency,quote_currency,as_of,rate,source) VALUES ('CNY','USD',%s,7,'synthetic')", (DAY,))
        conn.commit()
        publish(tmp_path, monkeypatch, day=DAY)
        monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED', 'false')
        with psycopg2.connect(**empty_source_db) as read:
            read.set_session(readonly=True, isolation_level='REPEATABLE READ')
            with read.cursor(cursor_factory=RealDictCursor) as cur:
                result=daily_close_account_identity(cur, source_account_id=ACCOUNT, as_of=DAY)
                assert result['status']=='ok' and result['authentication']=='unknown'
                row = balance(DAY)
                portfolio_state._apply_daily_close_balance(cur, row, base_currency='CNY', owner_ratio=Decimal('1'))
                assert row['amount_cny'] == Decimal('553'), row.get('daily_close')
                assert row['daily_close']['identity_evidence']['authentication'] == 'unknown'
                assert daily_close_account_identity(cur, source_account_id='U_OTHER', as_of=DAY)['status']=='unconfirmed'
                assert daily_close_account_identity(cur, source_account_id=ACCOUNT, as_of=date(2026,10,23))['status']=='unconfirmed'
        from endpoints import accounts
        from fastapi import FastAPI
        monkeypatch.setattr(accounts, 'get_conn', lambda: psycopg2.connect(**empty_source_db))
        monkeypatch.setattr(accounts, '_load_accounts_config', lambda: {'accounts': {'ibkr': {
            'account_type': 'brokerage', 'truth_source': 'ibkr_flex', 'base_currency': 'USD',
            'owner_ratio': '1', 'include_in_nav': True}}})
        monkeypatch.setattr(accounts, '_load_bank_config', lambda: {})
        monkeypatch.setattr(accounts, '_view_as_of', lambda: DAY)
        monkeypatch.setattr(portfolio_state, '_view_as_of', lambda: DAY)
        app=FastAPI(); app.include_router(accounts.router,prefix='/api')
        with TestClient(app) as web:
            response=web.get('/api/accounts/overview')
            assert response.status_code == 200, response.text
            account=next(row for row in response.json()['accounts'] if row['account_code']=='ibkr')
            assert account['daily_close']['identity_evidence']['authentication']=='unknown', account
            assert account['daily_close']['status']=='ok',account
            assert Decimal(str(account['daily_close']['accrual_and_other_components_cny']))==Decimal('-7')
            assert Decimal(str(account['primary_balance']['amount']))==Decimal('79')
            assert Decimal(str(account['primary_balance']['amount_cny']))==Decimal('553')
            import json, os
            from pathlib import Path
            from domain.brokers.ibkr import ibkr_daily_close_read
            manifest=Path(os.environ[ibkr_daily_close_read.MANIFEST_ENV])
            source_path=manifest.parent/json.loads(manifest.read_text(encoding='utf-8'))['source_file']
            original=source_path.read_bytes()
            source_path.write_bytes(original+b'changed')
            invalid=web.get('/api/accounts/overview')
            assert invalid.status_code==200
            invalid_account=next(r for r in invalid.json()['accounts'] if r['account_code']=='ibkr')
            assert invalid_account['primary_balance'] is None and invalid_account['daily_close']['status']=='unconfirmed'
            source_path.write_bytes(original)
            with conn.cursor() as cur:
                cur.execute("DELETE FROM pams_fx_rates_daily WHERE base_currency='CNY' AND quote_currency='USD' AND as_of=%s",(DAY,))
            conn.commit()
            missing=web.get('/api/accounts/overview')
            assert missing.status_code==200
            missing_account=next(r for r in missing.json()['accounts'] if r['account_code']=='ibkr')
            assert missing_account['primary_balance'] is None and missing_account['daily_close']['status']=='unconfirmed'




def _publish_binding(cur, account):
    from uuid import uuid4
    from domain.imports.ibkr_holding_source import stage_report
    from domain.brokers.ibkr.ibkr_holding_review import review, review_account_binding, publish as publish_review
    source=stage_report(cur,batch_id=uuid4(),account_code='ibkr',source_account_id=account,
        business_date=DAY,content=XML.replace(b'U_TEST',account.encode()))
    cur.execute('SELECT revision FROM source_batches WHERE batch_id=%s',(source['batch_id'],))
    revision=cur.fetchone()['revision']
    revision=review(cur,batch_id=source['batch_id'],item_id=source['position_item_ids'][0],
        expected_revision=revision,note='row')['revision']
    revision=review(cur,batch_id=source['batch_id'],item_id=source['scope_item_id'],
        expected_revision=revision,note='scope',scope_complete=True)['revision']
    revision=review_account_binding(cur,batch_id=source['batch_id'],source_account_id=account,
        expected_revision=revision,note='account mapping')['revision']
    publish_review(cur,batch_id=source['batch_id'],expected_revision=revision)
    return source


def test_ambiguous_account_bindings_remain_unknown(empty_source_db):
    from scripts.bootstrap_isolated_sources import initialize_isolated_sources
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            _publish_binding(cur,'U_TEST')
            _publish_binding(cur,'U_OTHER')
        conn.commit()
        conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            result=daily_close_account_identity(cur,source_account_id='U_TEST',as_of=DAY)
            assert result['status']=='unconfirmed'
            assert result['reasons']==['ambiguous_native_account_identity']


def test_changed_source_bytes_cannot_prove_mapping(empty_source_db):
    from scripts.bootstrap_isolated_sources import initialize_isolated_sources
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            source=_publish_binding(cur,'U_TEST')
        conn.commit()
        with conn.cursor() as cur:
            cur.execute('ALTER TABLE source_files DISABLE TRIGGER USER')
            cur.execute("UPDATE source_files SET content=%s WHERE batch_id=%s", (b'modified',source['batch_id']))
            cur.execute('ALTER TABLE source_files ENABLE TRIGGER USER')
        conn.commit()
        conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            result=daily_close_account_identity(cur,source_account_id='U_TEST',as_of=DAY)
            assert result['status']=='unconfirmed'
            assert result['reasons']==['account_binding_evidence_mismatch']
