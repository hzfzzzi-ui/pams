# ruff: noqa: F811
from uuid import uuid4

import psycopg2
from psycopg2.extras import RealDictCursor

from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from scripts import bootstrap_isolated_sources as bootstrap
from domain.imports import source_records_v2 as source
from domain.investments.valuation_input_evidence import _bound_input


def test_new_source_item_is_bound_and_changes_invalidate_evidence(empty_source_db):
    source_id, item_id = uuid4(), uuid4()
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            source.create_source(cur, source_id=source_id, account_code='tonghuashun', source_name='synthetic',
                                 acquisition_method='manual', document_type='holding')
            source.add_item(cur, source_id=source_id, item_id=item_id, parse_method='manual',
                            raw_fields={'synthetic': 'holding'}, parsed_fields={'asset_type': 'cash'})
            before, raw = _bound_input(cur, 'source_item', {'source_item_id': str(item_id)}, snapshot_cache={})
            assert before['source_ref']['account_code'] == 'tonghuashun'
            assert before['snapshot']['business_type'] == 'holding'
            assert before['snapshot']['review_status'] == 'pending'
            assert before['snapshot']['source_integrity'] == 'verified'
            assert before['snapshot']['source_fields']['asset_type'] == 'cash'
            assert raw['item']['source_id'] == str(source_id)
            source.review_item(cur, source_id=source_id, item_id=item_id, expected_revision=2,
                               parsed_fields={'asset_type': 'cash'}, review_status='confirmed')
            after, _ = _bound_input(cur, 'source_item', {'source_item_id': str(item_id)}, snapshot_cache={})
            assert after['snapshot']['review_status'] == 'confirmed'
            assert before['snapshot']['content_hash'] != after['snapshot']['content_hash']
        conn.commit()
    with psycopg2.connect(**empty_source_db) as conn:
        conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            current, _ = _bound_input(cur, 'source_item', {'source_item_id': str(item_id)}, locking=False)
            assert current['snapshot']['content_hash'] == after['snapshot']['content_hash']


def test_new_file_source_integrity_detects_altered_item(empty_source_db):
    source_id, item_id = uuid4(), uuid4()
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            source.create_source(cur, source_id=source_id, account_code='tonghuashun', source_name='synthetic',
                acquisition_method='file', document_type='holding', filename='data.csv', file_format='csv', content=b'original')
            source.add_item(cur, source_id=source_id, item_id=item_id, parse_method='manual',
                            raw_fields={'row': 'original', 'quantity': 1.25}, parsed_fields={'asset_type': 'cash'})
            before, _ = _bound_input(cur, 'source_item', {'source_item_id': str(item_id)})
            assert before['snapshot']['source_integrity'] == 'verified'
            cur.execute('ALTER TABLE source_items DISABLE TRIGGER USER')
            cur.execute("UPDATE source_items SET raw_fields='{}'::jsonb WHERE source_item_id=%s", (str(item_id),))
            cur.execute('ALTER TABLE source_items ENABLE TRIGGER USER')
            after, _ = _bound_input(cur, 'source_item', {'source_item_id': str(item_id)})
            assert after['snapshot']['source_integrity'] == 'unknown'


def test_new_holding_source_records_verified_evidence(empty_source_db):
    from domain.investments.holding_evidence_writer import finish_holding_change, verify_historical_holding_source
    from domain.investments.account_snapshot_evidence import _complete_member_proven
    from domain.investments.valuation_input_evidence import _bound_input, _verified_current_source

    source_id, item_id = uuid4(), uuid4()
    fields = {'asset_type': 'cash', 'business_date': '2026-01-02', 'currency': 'CNY', 'net_cash': '50'}
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            source.create_source(cur, source_id=source_id, account_code='tonghuashun', source_name='synthetic',
                                 acquisition_method='manual', document_type='holding')
            source.add_item(cur, source_id=source_id, item_id=item_id, parse_method='manual',
                            raw_fields={'entered_fields': fields}, parsed_fields=fields)
            source.review_item(cur, source_id=source_id, item_id=item_id, expected_revision=2,
                               parsed_fields=fields, review_status='confirmed')
            cur.execute("UPDATE source_records SET status='published',revision=revision+1 WHERE source_id=%s", (str(source_id),))
            cur.execute('''INSERT INTO pams_holdings_snapshot(account_code,name,asset_liability,currency,
                major_category,minor_category,market_value,cost,quantity,as_of,source_item_id)
                VALUES ('tonghuashun','Synthetic cash','资产','CNY','现金','现金',50,50,50,'2026-01-02',%s) RETURNING id''', (str(item_id),))
            holding_id = cur.fetchone()['id']
            finish_holding_change(cur, holding_id=holding_id, source_item_id=str(item_id))
            assert verify_historical_holding_source(cur, holding_id=holding_id, source_item_id=str(item_id))
            assert _complete_member_proven(cur, holding_id, item_id)
            _, current = _bound_input(cur, 'holding', {'id': holding_id})
            _, raw = _bound_input(cur, 'source_item', {'source_item_id': str(item_id)})
            assert _verified_current_source(cur, raw, holding_id, current, locking=True)
            assert not _verified_current_source(cur, raw, holding_id, {**current, 'market_value': '51'}, locking=True)
            assert not _verified_current_source(cur, raw, holding_id + 1, current, locking=True)
            assert not _verified_current_source(cur, raw, holding_id, {**current, 'source_item_id': str(uuid4())}, locking=True)
            cur.execute('SAVEPOINT altered_holding')
            cur.execute('UPDATE pams_holdings_snapshot SET market_value=51 WHERE id=%s', (holding_id,))
            assert not _complete_member_proven(cur, holding_id, item_id)
            cur.execute('ROLLBACK TO SAVEPOINT altered_holding')
        conn.commit()
    with psycopg2.connect(**empty_source_db) as conn:
        conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            assert verify_historical_holding_source(cur, holding_id=holding_id, source_item_id=str(item_id))
            assert _complete_member_proven(cur, holding_id, item_id)


def test_new_holding_publish_and_explicit_scope(empty_source_db, monkeypatch):
    from datetime import date
    from decimal import Decimal
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from domain.imports import account_import_store
    from domain.investments.snapshot_coverage import load_account_snapshot_coverage
    from endpoints import account_import

    monkeypatch.setenv('PAMS_SOURCE_TEST_TOKEN_KEY', 'ab' * 32)
    monkeypatch.setenv('PAMS_IMPORT_PUBLISH_ENABLED', 'true')
    monkeypatch.setattr(account_import_store, 'get_conn', lambda: psycopg2.connect(**empty_source_db))
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO pams_asset_master(asset_id,primary_symbol,canonical_name,asset_type,currency)
                VALUES ('TEST_STOCK','000001','Synthetic stock','stock','CNY')""")
            cur.execute("""INSERT INTO pams_asset_alias(asset_id,alias_type,alias_value)
                VALUES ('TEST_STOCK','alias','SYNTH_ALIAS')""")
    app = FastAPI()
    app.include_router(account_import.router, prefix='/api')
    root = '/api/account-import/source-records'
    with TestClient(app) as web:
        web.headers['Origin'] = 'http://testserver'
        response = web.post(root + '/manual', json={'account_code': 'tonghuashun', 'document_type': 'holding'})
        assert response.status_code == 200, response.text
        source_id = response.json()['source_id']
        base = f'{root}/{source_id}'
        common = {'business_date': '2026-01-02', 'currency': 'CNY'}
        fields = [
            {**common, 'asset_type': 'stock', 'symbol': 'SYNTH_ALIAS',
             'quantity': '100', 'price': '10', 'cost': '900', 'market_value': '1000', 'cost_basis_type': 'holding_cost'},
            {**common, 'asset_type': 'cash', 'net_cash': '50'},
        ]
        response = web.put(base + '/reviews', json={'revision': 1, 'items': [
            {'parsed_fields': item, 'review_status': 'confirmed'} for item in fields]})
        assert response.status_code == 200, response.text
        revision = response.json()['revision']
        scope = {'revision': revision, 'as_of': '2026-01-02', 'member_count': 2,
                 'cash_amount': '50', 'note': 'Synthetic complete scope', 'confirmed': True}
        request = {'revision': revision, 'scope': scope}
        response = web.post(base + '/holding-preview', json=request)
        assert response.status_code == 200, response.text
        token = response.json()['preview_token']
        assert web.post(base + '/holding-publish', json={**request, 'scope': None, 'preview_token': token}).status_code == 400
        assert web.get(base).json()['status'] == 'draft'
        response = web.post(base + '/holding-publish', json={**request, 'preview_token': token})
        assert response.status_code == 200, response.text
        assert response.json()['coverage_status'] == 'complete'
        assert web.post(base + '/holding-publish', json={**request, 'preview_token': token}).status_code == 409

    day = date(2026, 1, 2)
    with psycopg2.connect(**empty_source_db) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT sum(market_value) AS total FROM pams_holdings_snapshot WHERE account_code='tonghuashun'")
        assert cur.fetchone()['total'] == Decimal(1050)
        assert load_account_snapshot_coverage(cur, {('tonghuashun', day)})[('tonghuashun', day)]['status'] == 'complete'
        cur.execute("UPDATE pams_holdings_snapshot SET market_value=market_value+1 WHERE symbol='000001'")
        assert load_account_snapshot_coverage(cur, {('tonghuashun', day)})[('tonghuashun', day)]['status'] == 'unknown'
