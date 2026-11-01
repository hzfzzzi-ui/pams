"""Exercise the real Flex cash/source transaction on a marked new database."""
# ruff: noqa: F811
import re

from tests.brokers.ibkr.test_ibkr_sync_transaction import database, sync_case, cash_report, cash_facts, business_state  # noqa: F401


def test_flex_http_cash_event_publishes_source_in_same_transaction(sync_case, database, monkeypatch):
    from domain.brokers.ibkr import ibkr_report_batch
    web, conn, report = sync_case
    params = conn.get_dsn_parameters()
    assert params['host'] in {'127.0.0.1', 'localhost', '::1'}
    assert re.fullmatch(r'pams_import_test_[0-9a-f]{32}', params['dbname'])
    monkeypatch.setenv('PAMS_IBKR_CASH_EVENTS_AFTER', '2026-10-21')
    report['xml'] = cash_report({'transactionID': 'SYNTHETIC-DIV', 'type': 'Dividends', 'amount': '4'}).replace(
        'ibCommission="-1.25"', 'ibCommission="-1.25" ibCommissionCurrency="USD" taxAmount="0"')
    before = business_state(database)
    original = ibkr_report_batch.add_item
    with monkeypatch.context() as patch:
        def fail_link(*args, **kwargs):
            result = original(*args, **kwargs)
            if kwargs.get('raw_fields', {}).get('kind') == 'cash_event':
                raise ValueError('synthetic source-link failure')
            return result
        patch.setattr(ibkr_report_batch, 'add_item', fail_link)
        failed = web.post('/api/ibkr/flex/sync', json={})
        assert failed.status_code == 500, failed.text
        assert business_state(database) == before
    response = web.post('/api/ibkr/flex/sync', json={})
    assert response.status_code == 200, response.text
    assert response.json()['sync_run']['publication']['cash_events']['inserted_count'] == 1
    with conn.cursor() as cur:
        cur.execute("""SELECT i.raw_fields,b.status FROM source_items i JOIN source_batches b USING(batch_id)
            JOIN pams_investment_trade_fact f ON f.source_item_id=i.source_item_id
            WHERE f.source_table='ibkr_flex_cash_transaction'""")
        item = cur.fetchone()
        assert item[1] == 'published' and item[0]['official_attributes']['transactionID'] == 'SYNTHETIC-DIV'
    conn.rollback()
    assert len(cash_facts(database)) == 1
    before = business_state(database)
    response = web.post('/api/ibkr/flex/sync', json={})
    assert response.status_code == 200, response.text
    assert business_state(database) == before
