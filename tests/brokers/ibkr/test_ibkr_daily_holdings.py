"""Verified broker reports use the common source registry and holdings publisher."""
from copy import deepcopy
from datetime import timedelta
from hashlib import sha256
from uuid import uuid4
from xml.etree import ElementTree as ET

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

from tests.brokers.ibkr.test_ibkr_daily_close import ACCOUNT, DAY, compact, statement
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from scripts.bootstrap_isolated_sources import initialize_isolated_sources
from domain.brokers.ibkr.ibkr_daily_holdings import (
    ReportValidationError, historical_us_position_values, publish_report, read_report,
)
from domain.imports.source_records_v2 import create_source
from domain.investments.holdings_view import LATEST_KNOWN_HOLDINGS_CTE


def report(day=DAY, *, empty=False, stock='100'):
    root = statement(day, day, zero=empty, stock=stock)
    if not empty:
        row = root.find('OpenPositions/OpenPosition')
        row.attrib.update(conid='123456', symbol='SYNTH', description='Synthetic Fund',
                          listingExchange='SEHK', assetCategory='STK', currency='HKD',
                          fxRateToBase='0.125', position='10', markPrice='80',
                          costBasisMoney='700', costBasisPrice='70', positionValue='800')
    return root


def test_report_validates_account_date_scope_and_summary_rows():
    _, rows, complete = read_report(ET.tostring(report()), ACCOUNT, DAY)
    assert complete and rows['123456'][1]['currency'] == 'HKD'
    assert rows['123456'][1]['market_value'] == 800
    with pytest.raises(ReportValidationError):
        read_report(ET.tostring(report()), 'WRONG_ACCOUNT', DAY)
    with pytest.raises(ReportValidationError, match='stale_statement_date'):
        read_report(ET.tostring(report()), ACCOUNT, DAY + timedelta(days=1))
    root = report(empty=True)
    root.remove(root.find('OpenPositions'))
    with pytest.raises(ReportValidationError):
        read_report(ET.tostring(root), ACCOUNT, DAY)
    root = report()
    root.find('OpenPositions').remove(root.find('OpenPositions/OpenPosition'))
    with pytest.raises(ReportValidationError):
        read_report(ET.tostring(root), ACCOUNT, DAY)



def test_historical_us_values_require_each_days_verified_securities():
    root = ET.Element('FlexQueryResponse')
    first = report(DAY)
    first.find('OpenPositions/OpenPosition').attrib.update(
        listingExchange='NASDAQ', currency='USD', fxRateToBase='1',
        position='10', positionValue='100', costBasisMoney='80',
    )
    root.append(first)
    # A non-US holding and a US position with inconsistent report totals both leave gaps.
    root.append(report(DAY + timedelta(days=1)))
    inconsistent = report(DAY + timedelta(days=2))
    inconsistent.find('OpenPositions/OpenPosition').attrib.update(
        listingExchange='NASDAQ', currency='USD', fxRateToBase='1',
        position='10', positionValue='900', costBasisMoney='80',
    )
    root.append(inconsistent)
    content = ET.tostring(root)
    digest = sha256(content).hexdigest()
    assert historical_us_position_values(content, ACCOUNT, digest) == {DAY: (100, 80)}
    assert historical_us_position_values(content + b' ', ACCOUNT, digest) == {}

def test_broker_archives_and_snapshot_publication(empty_source_db):
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""INSERT INTO pams_accounts(account_code,display_name,base_currency)
                VALUES ('ibkr','Synthetic IBKR','USD'),('tonghuashun','Synthetic THS','CNY')
                ON CONFLICT (account_code) DO NOTHING""")
            ths, legacy = str(uuid4()), str(uuid4())
            create_source(cur, source_id=ths, account_code='tonghuashun', source_name='ths',
                          acquisition_method='file', document_type='trade', filename='test.csv',
                          file_format='csv', content=b'new synthetic spreadsheet')
            old = b'historical synthetic spreadsheet'
            cur.execute("""INSERT INTO public.source_records(source_id,account_code,source_name,
                acquisition_method,document_type,filename,file_format,content,sha256)
                VALUES (%s,'tonghuashun','ths','file','trade','old.csv','csv',%s,%s)""",
                        (legacy, old, sha256(old).hexdigest()))
            xml = ET.tostring(report())
            result = publish_report(cur, content=xml, source_account=ACCOUNT, day=DAY)
            assert result['published'] == 1 and result['coverage_status'] == 'partial'
            cur.execute("""SELECT r.scope_note,v.coverage_status,v.cash_status,v.scope_attestation
                FROM account_snapshot_version v JOIN source_records r ON r.source_id=v.source_batch_id
                WHERE v.version_id=%s""", (result['snapshot_version_id'],))
            assert cur.fetchone() == {
                'scope_note': None, 'coverage_status': 'partial', 'cash_status': 'unknown',
                'scope_attestation': {'securities_coverage': 'verified',
                                      'method': 'ibkr-flex-securities-v1', 'reported_positions': 1},
            }
            assert publish_report(cur, content=xml, source_account=ACCOUNT, day=DAY)['replayed']
            with pytest.raises(ReportValidationError, match='position_report_conflict'):
                publish_report(cur, content=xml + b'\n', source_account=ACCOUNT, day=DAY)

            cur.execute("""SELECT content,public.source_original_content(account_code,sha256,content) AS original
                FROM source_records WHERE source_id=%s""", (ths,))
            row = cur.fetchone()
            assert row['content'] is None and bytes(row['original']) == b'new synthetic spreadsheet'
            cur.execute("SELECT public.source_original_content(account_code,sha256,content) AS original FROM source_records WHERE source_id=%s", (legacy,))
            assert bytes(cur.fetchone()['original']) == old
            cur.execute("SELECT content FROM ibkr_source_originals")
            assert [bytes(row['content']) for row in cur.fetchall()] == [xml]
            cur.execute("SELECT content FROM tonghuashun_source_originals")
            assert [bytes(row['content']) for row in cur.fetchall()] == [b'new synthetic spreadsheet']
            cur.execute("""SELECT h.currency,h.market_value,h.cost,r.content,r.document_type
                FROM pams_holdings_snapshot h JOIN source_items i USING (source_item_id)
                JOIN source_records r USING (source_id) WHERE h.account_code='ibkr'""")
            assert cur.fetchone() == {'currency': 'HKD', 'market_value': 800, 'cost': 700,
                                      'content': None, 'document_type': 'holding'}
            cur.execute("SELECT count(*) AS count FROM pams_investment_trade_fact")
            assert cur.fetchone()['count'] == 0

            cur.execute("""INSERT INTO pams_asset_master(asset_id,canonical_name,asset_type,currency)
                VALUES ('SYNTH_HK','Synthetic Fund','stock','HKD')""")
            cur.execute("""INSERT INTO pams_asset_alias(asset_id,alias_type,alias_value,source)
                VALUES ('SYNTH_HK','ibkr_conid','123456','synthetic')""")
            renamed_day = DAY + timedelta(days=1)
            renamed = report(renamed_day)
            renamed.find('OpenPositions/OpenPosition').attrib.update(symbol='RENAMED', description='Changed Name')
            assert publish_report(cur, content=ET.tostring(renamed), source_account=ACCOUNT,
                                  day=renamed_day)['published'] == 1
            cur.execute(f"""WITH {LATEST_KNOWN_HOLDINGS_CTE}
                SELECT h.name,h.symbol,h.asset_id,h.quantity,i.source_uid,i.raw_fields->>'description' AS raw_name
                FROM latest_known_holding h JOIN source_items i USING (source_item_id)
                WHERE h.account_code='ibkr'""", (renamed_day,))
            assert cur.fetchall() == [{'name': 'SYNTH - Synthetic Fund', 'symbol': 'RENAMED', 'asset_id': 'SYNTH_HK',
                                       'quantity': 10, 'source_uid': '123456', 'raw_name': 'Changed Name'}]

            day = DAY + timedelta(days=2)
            incomplete = report(day)
            incomplete.find('OpenPositions').remove(incomplete.find('OpenPositions/OpenPosition'))
            with pytest.raises(ReportValidationError):
                publish_report(cur, content=ET.tostring(incomplete), source_account=ACCOUNT, day=day)
            publish_report(cur, content=ET.tostring(report(day, empty=True)), source_account=ACCOUNT, day=day)
            cur.execute(f"""WITH {LATEST_KNOWN_HOLDINGS_CTE}
                SELECT h.quantity,h.as_of,h.asset_id,i.source_uid,i.raw_fields->>'conid' AS conid
                FROM latest_known_holding h JOIN source_items i USING (source_item_id)
                WHERE h.account_code='ibkr'""", (day,))
            assert cur.fetchall() == [{'quantity': 0, 'as_of': day, 'asset_id': 'SYNTH_HK',
                                       'source_uid': '123456', 'conid': '123456'}]
            cur.execute("SELECT quantity FROM pams_holdings_snapshot WHERE account_code='ibkr' AND as_of=%s", (DAY,))
            assert cur.fetchone()['quantity'] == 10


def test_first_publication_requires_legacy_identity_evidence(empty_source_db):
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""INSERT INTO pams_accounts(account_code,display_name,base_currency)
                VALUES ('ibkr','Synthetic IBKR','USD') ON CONFLICT (account_code) DO NOTHING""")
            cur.execute("""INSERT INTO pams_asset_master(asset_id,canonical_name,asset_type,currency)
                VALUES ('SYNTH_HK','Synthetic Fund','stock','HKD'),
                       ('DUPLICATE_HK','Duplicate fund','stock','HKD')""")
            cur.execute("""INSERT INTO pams_holdings_snapshot
                (account_code,as_of,name,symbol,asset_id,currency,asset_liability,major_category,minor_category,
                 quantity,market_value)
                VALUES ('ibkr',%s,'Legacy holding name','SYNTH','SYNTH_HK','HKD',
                        %s,%s,%s,10,800)""", (DAY - timedelta(days=1), '资产', '港股', '股票'))
            xml = ET.tostring(report())
            with pytest.raises(ReportValidationError, match='position_legacy_identity_unverified'):
                publish_report(cur, content=xml, source_account=ACCOUNT, day=DAY)
            cur.execute("""INSERT INTO pams_asset_alias(asset_id,alias_type,alias_value,source)
                VALUES ('SYNTH_HK','ibkr_conid','123456','synthetic'),
                       ('DUPLICATE_HK','ibkr_conid','123456','synthetic')""")
            with pytest.raises(ReportValidationError, match='position_contract_mapping_conflict'):
                publish_report(cur, content=xml, source_account=ACCOUNT, day=DAY)
            cur.execute("SELECT count(*) AS count FROM ibkr_source_originals")
            assert cur.fetchone()['count'] == 0
            cur.execute("DELETE FROM pams_asset_alias WHERE asset_id='DUPLICATE_HK'")
            assert publish_report(cur, content=xml, source_account=ACCOUNT, day=DAY)['published'] == 1
            cur.execute("SELECT name,asset_id FROM pams_holdings_snapshot WHERE account_code='ibkr' AND as_of=%s", (DAY,))
            assert cur.fetchone() == {'name': 'Legacy holding name', 'asset_id': 'SYNTH_HK'}


def test_same_display_name_keeps_distinct_ibkr_contracts(empty_source_db):
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""INSERT INTO pams_accounts(account_code,display_name,base_currency)
                VALUES ('ibkr','Synthetic IBKR','USD') ON CONFLICT (account_code) DO NOTHING""")
            root = report(stock='200')
            section = root.find('OpenPositions')
            second = deepcopy(section.find('OpenPosition'))
            second.set('conid', '234567')
            section.append(second)
            assert publish_report(cur, content=ET.tostring(root), source_account=ACCOUNT, day=DAY)['published'] == 2
            cur.execute("""SELECT i.source_uid,h.name FROM pams_holdings_snapshot h
                JOIN source_items i USING (source_item_id) WHERE h.account_code='ibkr' ORDER BY i.source_uid""")
            assert cur.fetchall() == [{'source_uid': '123456', 'name': 'SYNTH - Synthetic Fund'},
                                      {'source_uid': '234567', 'name': 'SYNTH - Synthetic Fund [234567]'}]


def test_daily_holdings_http_publishes_once(empty_source_db, monkeypatch):
    from contextlib import closing

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from collectors import ibkr_flex
    from domain.brokers.ibkr import ibkr_report_state
    from endpoints import ibkr

    def connect():
        return psycopg2.connect(**empty_source_db)

    with closing(connect()) as conn:
        initialize_isolated_sources(conn)
        with conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO pams_accounts(account_code,display_name,base_currency)
                VALUES ('ibkr','Synthetic IBKR','USD') ON CONFLICT (account_code) DO NOTHING""")

    secret = 'synthetic-holdings-token-for-http-test'
    for key, value in {
        'PAMS_IMPORT_PUBLISH_ENABLED': 'true',
        'IBKR_FLEX_TOKEN': 'synthetic-flex-token',
        'PAMS_IBKR_HOLDINGS_QUERY_ID': '1234',
        'PAMS_IBKR_HOLDINGS_ACCOUNT_ID': ACCOUNT,
        'PAMS_IBKR_HOLDINGS_SYNC_TOKEN': secret,
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(ibkr, 'get_conn', connect)
    monkeypatch.setattr(ibkr_report_state, 'expected_statement_date', lambda: DAY)
    requests = []

    def reference(token, query):
        requests.append(query)
        return {'reference_code': 'synthetic-reference'}

    monkeypatch.setattr(ibkr_flex, 'request_flex_reference', reference)
    monkeypatch.setattr(ibkr_flex, 'fetch_flex_statement', lambda *a, **kw: ET.tostring(report(), encoding='unicode'))
    app = FastAPI()
    app.include_router(ibkr.router, prefix='/api')
    with TestClient(app) as client:
        path = '/api/ibkr/daily-holdings'
        body = {'expected_statement_reference': DAY.isoformat()}
        headers = {'X-PAMS-Holdings-Token': secret}
        assert client.get(f'{path}/status').json()['publication_verified'] is False
        assert client.post(f'{path}/sync', json=body).status_code == 403
        assert client.post(f'{path}/sync', json={}, headers=headers).status_code == 422
        first = client.post(f'{path}/sync', json=body, headers=headers)
        assert first.status_code == 200
        assert first.json()['published'] == 1
        assert client.get(f'{path}/status').json()['publication_verified'] is True
        assert client.post(f'{path}/sync', json=body, headers=headers).json()['replayed'] is True
        assert requests == ['1234']
