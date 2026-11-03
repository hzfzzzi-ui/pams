# ruff: noqa: F811
from datetime import date
from uuid import uuid4

import psycopg2
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg2.extras import RealDictCursor

from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from scripts import bootstrap_isolated_sources as bootstrap
from domain.imports import source_records_v2 as source
from endpoints import investment_transactions as endpoint


def test_new_trade_list_uses_source_and_trade_day_fx(empty_source_db, monkeypatch):
    day = date(2026, 10, 22)
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            source_id, item_id = uuid4(), uuid4()
            source.create_source(cur, source_id=source_id, account_code='tonghuashun', source_name='synthetic',
                                 acquisition_method='manual', document_type='trade')
            fields = {'business_date': day.isoformat(), 'trade_type': 'buy', 'symbol': 'TEST',
                      'currency': 'USD', 'quantity': '10', 'price': '10', 'gross_amount': '100',
                      'fee_amount': '2', 'tax_amount': '0', 'net_cash': '-102'}
            source.add_item(cur, source_id=source_id, item_id=item_id, parse_method='manual',
                            raw_fields={'test': 'synthetic'}, parsed_fields=fields)
            revision = source.review_item(cur, source_id=source_id, item_id=item_id, expected_revision=2,
                                          parsed_fields=fields, review_status='confirmed')
            source.publish_trade_source(cur, source_id=source_id, expected_revision=revision)
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES ('2026-10-23','CNY','USD',8,'synthetic')")
    monkeypatch.setattr(endpoint, 'get_conn', lambda: psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(endpoint, '_load_accounts_config', lambda: {})
    app = FastAPI(); app.include_router(endpoint.router, prefix='/api')
    with TestClient(app) as client:
        response = client.get('/api/investment-transactions')
        assert response.status_code == 200, response.text
        row = response.json()['rows'][0]
        assert float(row['gross_amount']) == 100 and row['gross_amount_cny'] is None
        assert row['source_evidence_status'] == 'verified' and response.json()['summary']['buy_amount_cny'] is None
        assert len(client.get('/api/investment-transactions?side=buy&account_code=tonghuashun').json()['rows']) == 1
        options = client.get('/api/investment-transactions/options')
        assert options.status_code == 200, options.text
        asset = next(item for item in options.json()['assets'] if item['asset_key'] == 'test')
        assert asset['trade_count'] == 1 and asset['pending_count'] == 1
        assert 'source_records' in options.json()['sources']
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO investment_trade_review(trade_id,review_note,review_status) SELECT trade_id,'pending note','needs_review' FROM pams_investment_trade_fact WHERE source_item_id=%s", (str(item_id),))
        review_row = client.get('/api/investment-transactions?review_status=needs_review').json()['rows'][0]
        assert review_row['review_note'] == 'pending note' and review_row['status'] == 'needs_review'
        ident = review_row['native_id']
        response = client.post(f'/api/investment-transactions/{ident}/review', json={
            'review_status': 'ignored', 'current_position_identity': 'trend', 'buy_reason': 'plan_execution'})
        assert response.status_code == 200, response.text
        assert response.json()['trade']['review_note'] == 'pending note'
        response = client.post(f'/api/investment-transactions/{ident}/review', json={'trade_plan': 'test plan'})
        assert response.status_code == 200 and response.json()['trade']['review_status'] == 'ignored'
        assert response.json()['trade']['buy_reason'] == 'plan_execution'
        assert response.json()['trade']['review_note'] == 'pending note'
        response = client.post(f'/api/investment-transactions/{ident}/review', json={
            'current_position_identity': 'value', 'identity_transition_type': 'other'})
        assert response.status_code == 200 and response.json()['trade']['identity_transition_quality'] == 'suspected_stop_avoidance'
        assert client.post('/api/investment-transactions/999999/review', json={'review_note': 'x'}).status_code == 404
        updated = client.get('/api/investment-transactions?review_status=ignored').json()['rows'][0]
        assert updated['review_note'] == 'pending note' and updated['status'] == 'ignored'
        assert updated['source_evidence_status'] == 'verified'
        attribution = client.get(f'/api/investment-transactions/{ident}/attribution')
        assert attribution.status_code == 200, attribution.text
        assert attribution.json()['found'] and attribution.json()['trade']['trade_id'] == ident
        assert client.get('/api/investment-transactions?side=sell').json()['rows'] == []
        assert client.get('/api/investment-transactions?start=2026-10-23').json()['rows'] == []
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'synthetic')", (day,))
        row = client.get('/api/investment-transactions').json()['rows'][0]
        assert float(row['gross_amount_cny']) == 700 and float(row['fees_cny']) == 14
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
            cur.execute('UPDATE pams_investment_trade_fact SET gross_amount=101 WHERE source_item_id=%s', (str(item_id),))
        data = client.get('/api/investment-transactions').json()
        assert data['rows'][0]['source_evidence_status'] == 'unknown'
        assert data['rows'][0]['gross_amount'] is None and data['summary']['buy_amount_cny'] is None
        assert client.get(f'/api/investment-transactions/{ident}/attribution').status_code == 409
