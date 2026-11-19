from uuid import uuid4

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from tests.database.test_transaction_fx_evidence import _publish_synthetic_trades
from scripts.bootstrap_isolated_sources import initialize_isolated_sources


def test_supplemental_source_links_existing_trade_without_new_fact(empty_source_db):
    from domain.imports import source_records_v2 as sources
    from domain.imports import trade_supplemental_evidence as evidence

    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("INSERT INTO pams_accounts(account_code,display_name) VALUES ('test_asset','Synthetic')")
        fields = {'business_date': '2026-01-22', 'trade_type': 'buy', 'symbol': 'TEST', 'name': 'Synthetic',
                  'currency': 'CNY', 'net_cash': '-100', 'quantity': '10', 'price': '10',
                  'gross_amount': '100', 'fee_amount': '0'}
        _publish_synthetic_trades(conn, [fields], account_code='test_asset')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT trade_id FROM pams_investment_trade_fact')
            trade = cur.fetchone()['trade_id']
            source, item = uuid4(), uuid4()
            sources.create_source(cur, source_id=source, account_code='test_asset', source_name='test',
                                  acquisition_method='screenshot', document_type='trade', filename='proof.jpg',
                                  file_format='jpg', content_type='image/jpeg', content=b'synthetic screenshot')
            sources.add_item(cur, source_id=source, item_id=item, parse_method='manual', raw_fields={'row': 1}, parsed_fields=fields)
            revision = sources.review_item(cur, source_id=source, item_id=item, expected_revision=2,
                                           parsed_fields=fields, review_status='confirmed')
            args = dict(source_id=source, item_id=item, trade_id=trade, expected_revision=revision,
                        note='User confirmed evidence', confirmed=True)
            saved = evidence.attach(cur, **args)
            assert not saved['replayed'] and evidence.attach(cur, **args)['replayed']
            linked, = evidence.read(cur, trade_id=trade)
            assert linked['status'] == 'verified' and linked['fields']['quantity'] == '10'
            cur.execute('SELECT count(*) AS n FROM pams_investment_trade_fact')
            assert cur.fetchone()['n'] == 1
            assert not evidence.revoke(cur, confirmation_id=saved['confirmation_id'], note='Undo', confirmed=True)['replayed']
            assert evidence.revoke(cur, confirmation_id=saved['confirmation_id'], note='Undo', confirmed=True)['replayed']
            assert evidence.read(cur, trade_id=trade) == []
            assert not evidence.attach(cur, **{**args, 'expected_revision': revision + 1})['replayed']


@pytest.mark.parametrize('field,value', [('symbol','WRONG'), ('business_date','2026-01-23'),
                                        ('currency','USD'), ('net_cash','-99'), ('trade_type','sell')])
def test_supplemental_source_rejects_conflicting_identity(empty_source_db, field, value):
    from domain.imports import source_records_v2 as sources
    from domain.imports import trade_supplemental_evidence as evidence

    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("INSERT INTO pams_accounts(account_code,display_name) VALUES ('test_asset','Synthetic')")
        fields = {'business_date': '2026-01-22', 'trade_type': 'buy', 'symbol': 'TEST', 'name': 'Synthetic',
                  'currency': 'CNY', 'net_cash': '-100', 'quantity': '10', 'price': '10',
                  'gross_amount': '100', 'fee_amount': '0'}
        _publish_synthetic_trades(conn, [fields], account_code='test_asset')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT trade_id FROM pams_investment_trade_fact')
            trade = cur.fetchone()['trade_id']
            source, item = uuid4(), uuid4(); altered = {**fields, field: value}
            sources.create_source(cur, source_id=source, account_code='test_asset', source_name='test',
                                  acquisition_method='screenshot', document_type='trade', filename='proof.jpg',
                                  file_format='jpg', content_type='image/jpeg', content=b'synthetic screenshot')
            sources.add_item(cur, source_id=source, item_id=item, parse_method='manual', raw_fields={'row': 1}, parsed_fields=altered)
            revision = sources.review_item(cur, source_id=source, item_id=item, expected_revision=2,
                                           parsed_fields=altered, review_status='confirmed')
            with pytest.raises(ValueError, match='differs'):
                evidence.attach(cur, source_id=source, item_id=item, trade_id=trade, expected_revision=revision,
                                note='User confirmed evidence', confirmed=True)


def test_upgrade_after_reversal_preserves_both_confirmation_types(empty_source_db):
    from pathlib import Path
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute(Path('database/postgres/upgrades/087_cash_reversal.sql').read_text(encoding='utf-8'))
            cur.execute(Path('database/postgres/upgrades/088_trade_source.sql').read_text(encoding='utf-8'))
            cur.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid='evidence_confirmation'::regclass AND conname='evidence_confirmation_confirmation_type_check'")
            definition = cur.fetchone()[0]
            assert 'cash_reversal' in definition and 'trade_source' in definition


def test_supplementary_evidence_api_attaches_reviewed_source(empty_source_db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from endpoints import account_import
    from domain.imports import source_records_v2 as sources

    def connect():
        return psycopg2.connect(**empty_source_db)
    with connect() as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("INSERT INTO pams_accounts(account_code,display_name) VALUES ('test_asset','Synthetic')")
        fields = {'business_date':'2026-01-22', 'trade_type':'buy', 'symbol':'TEST', 'currency':'CNY',
                  'net_cash':'-100', 'quantity':'10', 'price':'10', 'gross_amount':'100', 'fee_amount':'0'}
        _publish_synthetic_trades(conn, [fields], account_code='test_asset')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT trade_id FROM pams_investment_trade_fact')
            trade = cur.fetchone()['trade_id']
    monkeypatch.setattr(account_import.store, 'get_conn', connect)
    monkeypatch.setattr(account_import.access, 'same_origin', lambda request: None)
    app = FastAPI(); app.include_router(account_import.router, prefix='/api')
    with TestClient(app) as client:
        from io import BytesIO
        from PIL import Image
        image = BytesIO(); Image.new('RGB', (10, 10), 'white').save(image, format='PNG')
        response = client.post(f'/api/account-import/trade-evidence/{trade}/stage?filename=proof.png', content=image.getvalue())
        assert response.status_code == 200, response.text
        source = response.json()['source_id']; revision = response.json()['revision']
        response = client.post(f'/api/account-import/source-records/{source}/items', json={'revision': revision, 'parsed_fields': fields})
        assert response.status_code == 200, response.text
        item = response.json()['source_item_id']; revision = response.json()['revision']
        response = client.put(f'/api/account-import/source-records/{source}/items/{item}', json={
            'revision': revision, 'parsed_fields': fields, 'review_status': 'confirmed'})
        assert response.status_code == 200, response.text
        revision = response.json()['revision']
        response = client.post(f'/api/account-import/source-records/{source}/attach-trade-evidence', json={
            'revision': revision, 'item_id': item, 'trade_id': trade, 'note': 'Reviewed', 'confirmed': True})
        assert response.status_code == 200, response.text
        response = client.get(f'/api/account-import/trade-evidence/{trade}')
        assert response.status_code == 200 and response.json()['sources'][0]['status'] == 'verified'
