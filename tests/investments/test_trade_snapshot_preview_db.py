"""Synthetic source publication and strictly read-only snapshot preview."""
from datetime import date, datetime, timezone
from decimal import Decimal

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

from tests.imports.test_account_import_publish import client, draft, preview, publish, sql, trade, DAY
from domain.imports.source_fact_evidence import NativeFactVerifier
from domain.investments.trade_snapshot_preview import build_trade_snapshot_candidates


def test_verified_trade_builds_preview_without_writing_holdings(client):
    web, params = client
    batch = draft(web, [trade(trade_time='10:00:00')])
    check = preview(web, batch)
    assert publish(web, batch, check).status_code == 200
    before = sql(params, 'SELECT trade_id FROM pams_investment_trade_fact')
    with psycopg2.connect(**params) as conn:
        conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT trade_id,account_code,symbol,currency,trade_date,trade_time,side,quantity,net_cash FROM pams_investment_trade_fact')
            events = [dict(row) for row in cur.fetchall()]
            verified = NativeFactVerifier(cur).verify_many(table='pams_investment_trade_fact', fact_ids=[r['trade_id'] for r in events])
            for row in events:
                row['source_verified'] = verified[row['trade_id']]['status'] == 'verified'
            day = date.fromisoformat(DAY)
            result = build_trade_snapshot_candidates(
                events=events,
                identities={'000001': {'symbol': '000001', 'asset_id': 'TEST_STOCK', 'name': 'Test stock'}},
                quotes={'000001': {'symbol': 'sz000001', 'source': 'tencent', 'price': Decimal('12'),
                                   'price_date': day, 'price_time': datetime(day.year, day.month, day.day, tzinfo=timezone.utc)}},
                as_of=day, closed_adjustments=[])
            missing_time = [{**row, 'trade_time': None} for row in events]
            with pytest.raises(ValueError, match='ordering'):
                build_trade_snapshot_candidates(events=missing_time, identities={}, quotes={}, as_of=day)
    assert len(result) == 1
    assert result[0]['remaining_cost'] == Decimal('1005')
    assert result[0]['market_value'] == Decimal('1200')
    assert result[0]['total_pnl_status'] == 'coverage_unverified'
    assert not sql(params, 'SELECT * FROM pams_holdings_snapshot')
    assert sql(params, 'SELECT trade_id FROM pams_investment_trade_fact') == before
