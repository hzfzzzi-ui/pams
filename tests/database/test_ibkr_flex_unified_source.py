"""Exercise the real Flex HTTP path with synthetic report and disposable DB."""
# ruff: noqa: F811
from pathlib import Path
import re

import psycopg2
from psycopg2 import sql

from tests.brokers.ibkr.test_ibkr_sync_transaction import database, sync_case, cash_report, cash_facts  # noqa: F401
from domain.imports import isolation

ROOT = Path(__file__).resolve().parents[2]


def test_flex_http_cash_event_publishes_source_in_same_transaction(sync_case, database, monkeypatch):
    web, conn, report = sync_case
    params = conn.get_dsn_parameters()
    assert params['host'] in {'127.0.0.1', 'localhost', '::1'}
    assert re.fullmatch(r'pams_ibkr_tx_test_[0-9a-f]{32}', params['dbname'])
    with conn.cursor() as cur:
        cur.execute(sql.SQL("COMMENT ON DATABASE {} IS 'pams-isolated-ibkr-sync'").format(sql.Identifier(params['dbname'])))
        for name in ('080_unified_source_staging.sql', '081_unified_source_fact_links.sql'):
            cur.execute((ROOT / 'database/postgres/schema' / name).read_text(encoding='utf-8'))
    conn.commit()

    def test_database_only(cur):
        details = cur.connection.get_dsn_parameters()
        assert details['host'] in {'127.0.0.1', 'localhost', '::1'}
        assert re.fullmatch(r'pams_ibkr_tx_test_[0-9a-f]{32}', details['dbname'])
        cur.execute("SELECT shobj_description(oid, 'pg_database') AS marker FROM pg_database WHERE datname=current_database()")
        assert cur.fetchone()['marker'] == 'pams-isolated-ibkr-sync'
        return True

    monkeypatch.setattr(isolation, 'source_enabled', test_database_only)
    monkeypatch.setenv('PAMS_IBKR_CASH_EVENTS_AFTER', '2026-10-21')
    report['xml'] = cash_report({'transactionID': 'SYNTHETIC-DIV', 'type': 'Dividends', 'amount': '4'})
    from domain.brokers.ibkr import unified_source
    with monkeypatch.context() as patch:
        def fail_link(*args, **kwargs):
            raise ValueError('synthetic source-link failure')
        patch.setattr(unified_source, 'link_new_fact', fail_link)
        failed = web.post('/api/ibkr/flex/sync', json={})
        assert failed.status_code == 500
        assert cash_facts(database) == []
        with conn.cursor() as cur:
            cur.execute('SELECT count(*) FROM source_batches')
            assert cur.fetchone()[0] == 0
        conn.rollback()
    response = web.post('/api/ibkr/flex/sync', json={})
    assert response.status_code == 200, response.text
    assert response.json()['sync_run']['publication']['cash_events']['inserted_count'] == 1
    with conn.cursor() as cur:
        cur.execute('''SELECT i.raw_fields, b.status FROM source_items i
                       JOIN source_batches b USING(batch_id)
                       JOIN pams_investment_trade_fact f ON f.source_item_id=i.source_item_id''')
        item = cur.fetchone()
        assert item[1] == 'published' and item[0]['official_attributes']['transactionID'] == 'SYNTHETIC-DIV'
    conn.rollback()
    assert len(cash_facts(database)) == 1
    response = web.post('/api/ibkr/flex/sync', json={})
    assert response.status_code == 200
    assert len(cash_facts(database)) == 1
