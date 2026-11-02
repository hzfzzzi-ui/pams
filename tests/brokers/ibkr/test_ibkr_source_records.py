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
            with pytest.raises(ValueError, match='does not match'):
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
            with pytest.raises(ValueError, match='unreviewed'):
                publish_trade_source(cur, source_id=source_id, expected_revision=2)
            revision = review_item(cur, source_id=source_id, item_id=item['source_item_id'], expected_revision=2,
                                   parsed_fields=item['parsed_fields'], review_status='confirmed')
            assert publish_trade_source(cur, source_id=source_id, expected_revision=revision)['published'] == 1
            facts = load_trade_rows(cur, account_code='ibkr', start=date(2026, 10, 22), end=date(2026, 10, 22))
            assert len(facts) == 1 and facts[0]['source_evidence_status'] == 'verified'
            assert str(facts[0]['gross_amount']) == '340.0000000000'
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
