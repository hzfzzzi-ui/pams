"""Historical evidence is partial, dated, and never invents holding cost."""
import os
from datetime import date
from decimal import Decimal
from uuid import uuid4

import psycopg2
from psycopg2.extras import RealDictCursor
import pytest

from domain.imports import holding_publication_v2 as publication, source_records_v2 as sources
from domain.investments.position_nav_projection import load_position_nav_estimates
from domain.investments.holding_evidence_writer import verify_historical_holding_source
from domain.investments.holdings_view import LATEST_KNOWN_HOLDINGS_CTE
from domain.accounts.sync_status import _latest_cash_snapshots, _manual_position_value
from domain.accounts.accounts_view import build_account_detail_payload


def test_reviewed_historical_holding_publish_and_nav(monkeypatch):
    dsn = os.getenv('PAMS_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('requires explicitly isolated PostgreSQL')
    monkeypatch.setenv('PAMS_SOURCE_TEST_TOKEN_KEY', '12' * 32)
    monkeypatch.setenv('PAMS_IMPORT_PUBLISH_ENABLED', 'true')
    day, later = date(2025, 12, 31), date(2026, 1, 1)
    with psycopg2.connect(dsn) as conn:
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("INSERT INTO pams_accounts(account_code,display_name) VALUES ('test_history','Test')")
                cur.execute("INSERT INTO pams_asset_master(asset_id,canonical_name,primary_symbol,asset_type,currency) VALUES ('test_fund','Test fund','TESTFUND','fund','CNY')")
                cur.execute("""INSERT INTO pams_holdings_snapshot(account_code,as_of,symbol,name,currency,asset_liability,major_category,minor_category,market_value)
                    VALUES ('test_history','2026-03-01','OTHER','Other','CNY','资产','其他','其他',25)""")
                source, item = uuid4(), uuid4()
                sources.create_source(cur, source_id=source, account_code='test_history', source_name='reviewed example',
                    acquisition_method='manual', document_type='holding')
                fields = {'business_date': day.isoformat(), 'currency':'CNY', 'asset_type':'fund', 'symbol':'TESTFUND',
                          'quantity':'100', 'price':'1.2', 'market_value':'120', 'cost_basis_type':'unknown'}
                sources.add_item(cur, source_id=source, item_id=item, parse_method='manual', raw_fields={'entered_fields':fields}, parsed_fields=fields)
                sources.review_item(cur, source_id=source, item_id=item, expected_revision=2, parsed_fields=fields, review_status='confirmed')
                with pytest.raises(ValueError):
                    publication.preview(cur, source_id=source, revision=3)
                with pytest.raises(ValueError, match='完整范围'):
                    publication.preview(cur, source_id=source, revision=3, historical=True, scope={})
                plan = publication.preview(cur, source_id=source, revision=3, historical=True)
                assert plan['holdings'][0]['cost'] is None
                result = publication.publish(cur, source_id=source, revision=3, historical=True, preview_token=plan['preview_token'])
                assert result['coverage_status'] == 'partial'
                cur.execute("SELECT id,cost,buy_price FROM pams_holdings_snapshot WHERE source_item_id=%s", (str(item),))
                holding = cur.fetchone()
                assert holding['cost'] is None and holding['buy_price'] is None
                assert verify_historical_holding_source(cur, holding_id=holding['id'], source_item_id=str(item))
                for cutoff, expected in ((day, ['TESTFUND']), (later, []), (date(2026, 3, 1), ['OTHER'])):
                    cur.execute(f'WITH {LATEST_KNOWN_HOLDINGS_CTE} SELECT symbol FROM latest_known_holding ORDER BY symbol', (cutoff,))
                    assert [row['symbol'] for row in cur.fetchall()] == expected
                with pytest.raises(ValueError):
                    publication.publish(cur, source_id=source, revision=3, historical=True, preview_token=plan['preview_token'])
                assert _latest_cash_snapshots(cur, ['test_history']) == []
                assert _manual_position_value(cur, 'test_history', later) is None
                assert build_account_detail_payload(cur, account_code='test_history')['holdings_count'] == 1
                estimates = load_position_nav_estimates(cur, accounts={'test_history': {}}, dates=[day, later],
                    owner_ratio_case_sql=lambda _: ('1::numeric', []), account_owner_ratio=lambda _: Decimal('1'),
                    rate_at=lambda currency, _: Decimal('1') if currency == 'CNY' else None)
                assert estimates[day]['test_history'][0] == Decimal('120')
                assert 'test_history' not in estimates.get(later, {})
        finally:
            conn.rollback()
