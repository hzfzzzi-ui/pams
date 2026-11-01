"""Synthetic source publication and strictly read-only snapshot preview."""
from datetime import date, datetime, timezone
from decimal import Decimal

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

from tests.imports.test_account_import_publish import client, draft, preview, publish, sql, trade, DAY
from domain.investments.trade_snapshot_preview import build_trade_snapshot_candidates
from domain.investments.trade_snapshot_source import load_verified_equity_events
from domain.investments.trade_snapshot_writer import preview as writer_preview, publish_isolated


def test_verified_trade_builds_preview_without_writing_holdings(client):
    web, params = client
    batch = draft(web, [trade(trade_time='10:00:00')])
    check = preview(web, batch)
    assert publish(web, batch, check).status_code == 200
    before = sql(params, 'SELECT trade_id FROM pams_investment_trade_fact')
    with psycopg2.connect(**params) as conn:
        conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            events, identities = load_verified_equity_events(cur, 'tonghuashun')
            day = date.fromisoformat(DAY)
            result = build_trade_snapshot_candidates(
                events=events,
                identities=identities,
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
    sql(params, 'UPDATE pams_investment_trade_fact SET net_cash=net_cash-1')
    with psycopg2.connect(**params) as conn:
        conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            with pytest.raises(ValueError, match='source verification failed'):
                load_verified_equity_events(cur, 'tonghuashun')


def test_isolated_snapshot_write_binds_inputs_and_rejects_replay(client, monkeypatch):
    web, params = client
    batch = draft(web, [trade(trade_time='10:00:00')])
    assert publish(web, batch, preview(web, batch)).status_code == 200
    sql(params, '''CREATE TABLE market_observations (
        quote_id bigint primary key, observation_type text, symbol text, asset_id text, currency text,
        provider text, source text, as_of date, quote_date date, price numeric, price_time timestamptz)''')
    sql(params, '''INSERT INTO market_observations VALUES
        (1,'quote','sz000001','TEST_STOCK','CNY','tencent','tencent_stock_quote',%s,%s,12,%s)''',
        (DAY, DAY, datetime.fromisoformat(DAY).replace(tzinfo=timezone.utc)))
    with psycopg2.connect(**params) as conn:
        conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            candidate = writer_preview(cur, account_code='tonghuashun', as_of=date.fromisoformat(DAY), quote_ids={'000001': 1})
    with pytest.raises(ValueError, match='preview is stale'):
        with psycopg2.connect(**params) as conn:
            conn.set_session(isolation_level='SERIALIZABLE')
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                publish_isolated(cur, account_code='tonghuashun', as_of=date.fromisoformat(DAY),
                                 quote_ids={'000001': 1}, expected_digest='0' * 64)
    assert not sql(params, 'SELECT id FROM pams_holdings_snapshot')
    import domain.investments.trade_snapshot_writer as writer
    original = writer.finish_holding_change
    def fail_before_evidence(*args, **kwargs):
        raise RuntimeError('synthetic evidence failure')
    monkeypatch.setattr(writer, 'finish_holding_change', fail_before_evidence)
    with pytest.raises(RuntimeError, match='synthetic evidence failure'):
        with psycopg2.connect(**params) as conn:
            conn.set_session(isolation_level='SERIALIZABLE')
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                publish_isolated(cur, account_code='tonghuashun', as_of=date.fromisoformat(DAY),
                                 quote_ids={'000001': 1}, expected_digest=candidate['digest'])
    assert not sql(params, 'SELECT id FROM pams_holdings_snapshot')
    monkeypatch.setattr(writer, 'finish_holding_change', original)
    with psycopg2.connect(**params) as conn:
        conn.set_session(isolation_level='SERIALIZABLE')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            result = publish_isolated(cur, account_code='tonghuashun', as_of=date.fromisoformat(DAY),
                                      quote_ids={'000001': 1}, expected_digest=candidate['digest'])
    assert len(result['holding_ids']) == 1
    rows = sql(params, 'SELECT quantity,cost,market_value FROM pams_holdings_snapshot')
    assert rows[0]['quantity'] == Decimal('100')
    assert rows[0]['cost'] == Decimal('1005')
    assert rows[0]['market_value'] == Decimal('1200')
    assert sql(params, 'SELECT calculation_version FROM valuation_evidence WHERE calculation_version=%s', ('holding-change-after',))
    with psycopg2.connect(**params) as conn:
        conn.set_session(isolation_level='SERIALIZABLE')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            with pytest.raises(ValueError, match='existing account snapshot'):
                publish_isolated(cur, account_code='tonghuashun', as_of=date.fromisoformat(DAY),
                                 quote_ids={'000001': 1}, expected_digest=candidate['digest'])
    assert len(sql(params, 'SELECT id FROM pams_holdings_snapshot')) == 1
