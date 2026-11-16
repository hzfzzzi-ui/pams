from datetime import timedelta
from hashlib import sha256
from xml.etree import ElementTree as ET

import psycopg2
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg2.extras import RealDictCursor

from scripts.bootstrap_isolated_sources import initialize_isolated_sources
from tests.brokers.ibkr.test_ibkr_daily_close import ACCOUNT, DAY, compact
from tests.brokers.ibkr.test_ibkr_daily_holdings import report
from tests.database.test_isolated_source_bootstrap import empty_source_db as isolated_source_db  # noqa: F401


def test_ibkr_read_endpoints_use_verified_archives_without_legacy_sources(isolated_source_db, monkeypatch):  # noqa: F811
    from domain.brokers.ibkr import ibkr_reconciliation, ibkr_report_state
    from domain.brokers.ibkr.ibkr_daily_holdings import publish_report
    from endpoints import ibkr

    def connect():
        return psycopg2.connect(**isolated_source_db)

    def flex_xml(day, *, activity=False):
        root = report(day)
        trades = ET.SubElement(root, 'Trades')
        cash = ET.SubElement(root, 'CashTransactions')
        if activity:
            ET.SubElement(trades, 'Trade', accountId=ACCOUNT, tradeID='42', assetCategory='STK',
                          levelOfDetail='EXECUTION', conid='123456', listingExchange='SEHK',
                          symbol='SYNTH', currency='HKD', buySell='BUY', tradeDate=compact(day),
                          quantity='1', tradePrice='100', tradeMoney='100', ibCommission='1',
                          ibCommissionCurrency='HKD', taxes='0', netCash='-101')
            ET.SubElement(cash, 'CashTransaction', accountId=ACCOUNT, transactionID='fee-1',
                          type='Other Fees', dateTime=compact(day), reportDate=compact(day),
                          currency='USD', amount='-1')
        return ET.tostring(root)

    content = flex_xml(DAY, activity=True)
    digest = sha256(content).hexdigest()
    with connect() as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT to_regclass('source_batches') AS batches, to_regclass('source_files') AS files")
            assert cur.fetchone() == {'batches': None, 'files': None}
            cur.execute("""INSERT INTO pams_accounts(account_code,display_name,base_currency)
                VALUES ('ibkr','Synthetic IBKR','USD') ON CONFLICT (account_code) DO NOTHING""")
            result = publish_report(cur, content=content, source_account=ACCOUNT, day=DAY)
            assert result['facts']['trade']['published'] == result['facts']['cash']['published'] == 1

    monkeypatch.delenv('PAMS_IBKR_HOLDINGS_ACCOUNT_ID', raising=False)
    monkeypatch.delenv('PAMS_IMPORT_PUBLISH_ENABLED', raising=False)
    monkeypatch.setattr(ibkr, 'get_conn', connect)
    monkeypatch.setattr(ibkr_reconciliation, 'get_conn', connect)
    monkeypatch.setattr(ibkr_report_state, 'expected_statement_date', lambda: DAY)
    app = FastAPI()
    app.include_router(ibkr.router, prefix='/api')
    with TestClient(app) as client:
        cash = client.get('/api/ibkr/cash-events/status')
        assert cash.status_code == 200
        assert cash.json()['confirmed_day'] == cash.json()['facts_as_of'] == str(DAY)
        assert cash.json()['source_sha256'] == digest
        assert cash.json()['queued_day_count'] == 0
        flex = client.get('/api/ibkr/flex/status')
        assert flex.status_code == 200
        assert flex.json()['report_quality']['statement_as_of'] == str(DAY)
        assert flex.json()['report_quality']['status'] == 'ok'
        audit = client.get('/api/ibkr/reconciliation')
        assert audit.status_code == 200
        assert audit.json()['report']['sha256'] == digest
        checks = {check['id']: check for check in audit.json()['checks']}
        assert checks['trade_facts']['compared'] == checks['cash_facts']['compared'] == 1
        assert checks['trade_facts']['status'] == checks['cash_facts']['status'] == 'ok'
        assert not checks['source']['reason_counts'].get('missing_table')

        with connect() as conn, conn.cursor() as cur:
            cur.execute("UPDATE pams_cashflow_fact SET amount=amount-1 WHERE account_code='ibkr'")
        changed = client.get('/api/ibkr/reconciliation').json()
        changed_cash = next(check for check in changed['checks'] if check['id'] == 'cash_facts')
        assert changed_cash['compared'] == 0
        assert changed_cash['reason_counts'] == {'source_link_mismatch': 1}

        later = DAY + timedelta(days=1)
        latest_content = flex_xml(later)
        with connect() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            publish_report(cur, content=latest_content, source_account=ACCOUNT, day=later)
        with connect() as conn, conn.cursor() as cur:
            cur.execute('ALTER TABLE ibkr_source_originals DISABLE TRIGGER USER')
            cur.execute("DELETE FROM ibkr_source_originals WHERE sha256=%s",
                        (sha256(latest_content).hexdigest(),))
            cur.execute('ALTER TABLE ibkr_source_originals ENABLE TRIGGER USER')

        cash = client.get('/api/ibkr/cash-events/status').json()
        assert cash['status'] == 'unavailable'
        assert cash['reasons'] == ['unverified_latest_archive']
        assert cash['confirmed_day'] == cash['facts_as_of'] == str(DAY)
        degraded = client.get('/api/ibkr/flex/status').json()['report_quality']
        assert degraded['statement_as_of'] == str(DAY)
        assert degraded['using_previous'] is True
        assert degraded['status'] == 'review'
        assert client.get('/api/ibkr/reconciliation').json()['report']['sha256'] == digest
