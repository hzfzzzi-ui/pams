from datetime import date
from decimal import Decimal
from pathlib import Path
import sys

import psycopg2
from psycopg2.extras import RealDictCursor

API_ROOT = Path(__file__).resolve().parents[2] / 'backend' / 'api'
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from domain.imports.document_stage_v2 import stage_document
from domain.imports.source_records_v2 import publish_trade_source
from domain.investments.nav_trade_source_v2 import project
from domain.investments.valuation_nav import ValuationEvidenceSeries
from scripts.bootstrap_isolated_sources import initialize_isolated_sources
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from tests.imports.test_account_import_ths import event, export


def test_ths_nav_keeps_verified_prefix_across_overlapping_and_partial_exports(empty_source_db):
    first, second, third, fourth = [date(2025, 10, day) for day in (20, 21, 22, 23)]

    def interest(day, amount, balance, suffix=''):
        return event('利息归本', **{
            '成交日期': day.strftime('%Y%m%d'), '发生金额': amount, '本次金额': balance,
            '定位串': day.isoformat() + suffix,
        })

    original = interest(first, '2.00', '102.00')
    incremental = interest(second, '1.00', '103.00')
    incomplete = interest(fourth, '1.00', '105.00')
    unreviewed = event('未知业务', **{'成交日期': '20251022', '本次金额': '104.00',
                                      '发生金额': '1.00', '定位串': '20251022-pending'})

    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            def publish(*rows, mode='all'):
                source = stage_document(cur, account_code='tonghuashun', source_name='ths', filename='broker.xls',
                                        content=export(*rows))
                return publish_trade_source(cur, source_id=source['source_id'],
                                            expected_revision=source['revision'], mode=mode)

            def series():
                return project(cur, dates=[first, second, third, fourth], owner_ratio=Decimal(1),
                               series_type=ValuationEvidenceSeries)

            assert publish(original)['published'] == 1
            assert series().preview_by_date[first] == 102
            assert publish(incremental, original, mode='ready')['published'] == 1
            assert series().preview_by_date == {first: 102, second: 103, third: 103, fourth: 103}

            assert publish(incomplete, unreviewed, mode='ready')['published'] == 1
            result = series()
            assert result.preview_by_date == {first: 102, second: 103}
            assert result.preview_source_through == second
            assert result.preview_performance_by_date[second]['total_pnl'] == 3
