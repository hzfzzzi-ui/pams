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
