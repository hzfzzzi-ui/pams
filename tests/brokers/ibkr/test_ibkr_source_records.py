"""IBKR source staging against an isolated, freshly initialized database."""
from uuid import UUID

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from tests.brokers.ibkr.test_ibkr_sync_transaction import XML
from scripts.bootstrap_isolated_sources import initialize_isolated_sources
from collectors.ibkr_source_records import stage_trade_report
from domain.imports.source_records_v2 import review_item, publish_trade_source
from domain.investments.trade_read_v2 import load_trade_rows


def test_ibkr_trade_source_keeps_original_and_requires_explicit_review(empty_source_db):  # noqa: F811
    from datetime import date
    report = XML.replace('ibCommission="-1.25"', 'ibCommission="-1.25" ibCommissionCurrency="USD" taxAmount="0"').encode('utf-8')
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            with pytest.raises(ValueError, match='invalid IBKR report'):
                stage_trade_report(cur, report_bytes=report, expected_source_account='U_OTHER')
            with pytest.raises(ValueError, match='amounts do not reconcile'):
                stage_trade_report(cur, report_bytes=report.replace(b'tradeMoney="340"', b'tradeMoney="341"'),
                                   expected_source_account='U_TEST')
            cur.execute('SELECT count(*) AS n FROM source_records')
            assert cur.fetchone()['n'] == 0
            result = stage_trade_report(cur, report_bytes=report, expected_source_account='U_TEST')
            assert result['row_count'] == 1 and result['revision'] == 2
            source_id = UUID(result['source_id'])
            cur.execute('SELECT status,content,sha256 FROM source_records WHERE source_id=%s', (str(source_id),))
            record = cur.fetchone()
            assert record['status'] == 'draft' and bytes(record['content']) == report
            assert record['sha256'] == result['report_sha256']
            cur.execute('SELECT source_item_id,source_uid,raw_fields,parsed_fields,review_status FROM source_items WHERE source_id=%s', (str(source_id),))
            item = cur.fetchone()
            assert item['review_status'] == 'pending' and item['source_uid'].startswith('ibkr_trade:')
            assert item['raw_fields']['official_attributes']['tradeID'] == 'T1'
            assert item['parsed_fields']['net_cash'] == '-341.25' and item['parsed_fields']['fee_currency'] == 'USD'
            with pytest.raises(ValueError, match='business fields cannot be edited'):
                review_item(cur, source_id=source_id, item_id=item['source_item_id'], expected_revision=2,
                            parsed_fields={**item['parsed_fields'], 'net_cash':'-1'}, review_status='confirmed')
            with pytest.raises(ValueError, match='business fields cannot be edited'):
                review_item(cur, source_id=source_id, item_id=item['source_item_id'], expected_revision=2,
                            parsed_fields={**item['parsed_fields'], 'fee_scope':'total_charges'}, review_status='confirmed')
            with pytest.raises(ValueError, match='unreviewed'):
                publish_trade_source(cur, source_id=source_id, expected_revision=2)
            revision = review_item(cur, source_id=source_id, item_id=item['source_item_id'], expected_revision=2,
                                   parsed_fields=item['parsed_fields'], review_status='confirmed')
            assert publish_trade_source(cur, source_id=source_id, expected_revision=revision)['published'] == 1
            facts = load_trade_rows(cur, account_code='ibkr', start=date(2026, 10, 22), end=date(2026, 10, 22))
            assert len(facts) == 1 and facts[0]['source_evidence_status'] == 'verified'
            assert str(facts[0]['gross_amount']) == '340.0000000000'
            with conn.cursor() as tuple_cursor:
                tuple_cursor.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES ('2026-10-22','CNY','USD',7,'synthetic')")
                tuple_facts = load_trade_rows(tuple_cursor, account_code='ibkr', start=date(2026, 10, 22), end=date(2026, 10, 22))
                assert tuple_facts[0]['source_evidence_status'] == 'verified'
            with pytest.raises(ValueError, match='already exists'):
                stage_trade_report(cur, report_bytes=report, expected_source_account='U_TEST')
            changed = report.replace(b'ibCommission="-1.25"', b'ibCommission="-1.50"').replace(b'netCash="-341.25"', b'netCash="-341.50"')
            later = stage_trade_report(cur, report_bytes=changed, expected_source_account='U_TEST')
            cur.execute('SELECT source_item_id,parsed_fields FROM source_items WHERE source_id=%s', (later['source_id'],))
            observed = cur.fetchone()
            revision = review_item(cur, source_id=UUID(later['source_id']), item_id=observed['source_item_id'],
                expected_revision=later['revision'], parsed_fields=observed['parsed_fields'], review_status='confirmed')
            with pytest.raises(ValueError, match='existing source trade identifier'):
                publish_trade_source(cur, source_id=UUID(later['source_id']), expected_revision=revision)
            cur.execute('SET CONSTRAINTS ALL IMMEDIATE')


def test_ibkr_report_http_stages_then_requires_review(empty_source_db, monkeypatch):  # noqa: F811
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from endpoints import account_import
    from domain.imports import account_import_store

    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
    monkeypatch.setattr(account_import_store, 'get_conn', lambda: psycopg2.connect(**empty_source_db))
    monkeypatch.setenv('PAMS_SOURCE_TEST_TOKEN_KEY', 'a1' * 32)
    monkeypatch.setenv('PAMS_IMPORT_PUBLISH_ENABLED', 'true')
    app = FastAPI(); app.include_router(account_import.router, prefix='/api')
    report = XML.replace('ibCommission="-1.25"', 'ibCommission="-1.25" ibCommissionCurrency="USD" taxAmount="0"').encode('utf-8')
    url = '/api/account-import/source-records/ibkr-report'
    with TestClient(app) as client:
        assert client.post(url, content=report, headers={'Origin':'http://other','X-PAMS-IBKR-Account':'U_TEST'}).status_code == 403
        client.headers.update({'Origin':'http://testserver','X-PAMS-IBKR-Account':'U_TEST'})
        assert client.post(url, content=b'<broken').status_code == 400
        assert client.post(url, content=b'<!DOCTYPE x [<!ENTITY a "x">]><x>&a;</x>').status_code == 400
        response = client.post(url, content=report)
        assert response.status_code == 200, response.text
        path = '/api/account-import/source-records/' + response.json()['source_id']
        draft = client.get(path).json()
        assert client.post(path+'/trade-preview', json={'revision':draft['revision']}).status_code == 400
        item = draft['items'][0]
        reviewed = client.put(path+'/items/'+item['source_item_id'], json={'revision':draft['revision'],
            'parsed_fields':item['parsed_fields'], 'review_status':'confirmed'})
        assert reviewed.status_code == 200
        revision = reviewed.json()['revision']
        preview = client.post(path+'/trade-preview', json={'revision':revision})
        assert preview.status_code == 200, preview.text
        published = client.post(path+'/trade-publish', json={'revision':revision,'preview_token':preview.json()['preview_token']})
        assert published.status_code == 200 and published.json()['published'] == 1


@pytest.mark.parametrize('tamper', ['parsed_and_fact', 'raw_and_fact', 'source_uid'])
def test_ibkr_new_source_verification_rejects_tampering(empty_source_db, tamper):  # noqa: F811
    from datetime import date
    from hashlib import sha256
    import json
    from psycopg2.extras import Json

    report = XML.replace('ibCommission="-1.25"', 'ibCommission="-1.25" ibCommissionCurrency="USD" taxAmount="0"').encode('utf-8')
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            source = stage_trade_report(cur, report_bytes=report, expected_source_account='U_TEST')
            cur.execute('SELECT source_item_id,parsed_fields FROM source_items WHERE source_id=%s', (source['source_id'],))
            item = cur.fetchone()
            revision = review_item(cur, source_id=UUID(source['source_id']), item_id=item['source_item_id'],
                                   expected_revision=source['revision'], parsed_fields=item['parsed_fields'],
                                   review_status='confirmed')
            publish_trade_source(cur, source_id=UUID(source['source_id']), expected_revision=revision)
        conn.commit()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            day = date(2026, 10, 22)
            assert load_trade_rows(cur, account_code='ibkr', start=day, end=day)[0]['source_evidence_status'] == 'verified'
            cur.execute('ALTER TABLE source_items DISABLE TRIGGER USER')
            if tamper == 'source_uid':
                cur.execute("UPDATE source_items SET source_uid='ibkr_trade:forged' WHERE source_item_id=%s", (item['source_item_id'],))
            else:
                cur.execute('SELECT raw_fields,parsed_fields FROM source_items WHERE source_item_id=%s', (item['source_item_id'],))
                stored = cur.fetchone()
                stored['parsed_fields']['gross_amount'] = '341'
                if tamper == 'raw_and_fact':
                    stored['raw_fields']['official_attributes']['tradeMoney'] = '341'
                digest = sha256(json.dumps(stored['raw_fields'], ensure_ascii=False, sort_keys=True,
                    separators=(',', ':')).encode('utf-8')).hexdigest()
                cur.execute('UPDATE source_items SET raw_fields=%s,parsed_fields=%s,record_hash=%s WHERE source_item_id=%s',
                    (Json(stored['raw_fields']), Json(stored['parsed_fields']), digest, item['source_item_id']))
                cur.execute('UPDATE pams_investment_trade_fact SET gross_amount=341 WHERE source_item_id=%s', (item['source_item_id'],))
            cur.execute('ALTER TABLE source_items ENABLE TRIGGER USER')
            assert load_trade_rows(cur, account_code='ibkr', start=day, end=day)[0]['source_evidence_status'] == 'unknown'
