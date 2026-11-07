from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import psycopg2
from psycopg2.extras import RealDictCursor
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend/api'))
from domain.market.tencent_stock_history import parse_history, publish_history
from domain.investments import ths_nav_history_refresh as refresh
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401


DAY = date(2026, 1, 2)


def sample(close='10.200'):
    return {'code': 0, 'data': {'sz000001': {'day': [['2026-01-02', '10.000', close, '10.500', '9.900', '100']]}}}


def test_unadjusted_source_and_invalid_prices():
    rows = parse_history(sample(), 'sz000001', start=DAY, end=DAY)
    assert rows[0]['close'] == Decimal('10.2')
    assert rows[0]['evidence']['basis'] == 'unadjusted'
    with pytest.raises(ValueError):
        parse_history({'code': 0, 'data': {'sz000001': {'qfqday': sample()['data']['sz000001']['day']}}},
                      'sz000001', start=DAY, end=DAY)
    for bad in ('NaN', '-1', '11', '10.20001'):
        with pytest.raises(ValueError):
            parse_history(sample(bad), 'sz000001', start=DAY, end=DAY)


def test_atomic_publish_replay_and_conflict_rollback(empty_source_db):
    with psycopg2.connect(**empty_source_db) as conn:
        with conn.cursor() as cur:
            cur.execute('''CREATE TABLE pams_ohlcv_daily (symbol text,market text,trade_date date,
                close numeric(12,4),PRIMARY KEY(symbol,market,trade_date))''')
            cur.execute('''CREATE TABLE market_observations (observation_type text,as_of date,symbol text,
                market text,currency text,source text,source_key text,provider text,confidence text,
                open_price numeric,high_price numeric,low_price numeric,close_price numeric,
                source_hash text,raw_json jsonb)''')
            cur.execute("CREATE UNIQUE INDEX daily_key ON market_observations (as_of,source,source_key) WHERE observation_type='daily'")
    rows = parse_history(sample(), 'sz000001', start=DAY, end=DAY)
    for expected in (1, 0):
        with psycopg2.connect(**empty_source_db) as conn:
            conn.set_session(isolation_level='SERIALIZABLE')
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                assert publish_history(cur, rows)['inserted'] == expected
    conflicting = parse_history(sample('10.300'), 'sz000001', start=DAY, end=DAY)
    second = parse_history({'code': 0, 'data': {'sz000002': {'day': [['2026-01-02','10','10','10','10','1']]}}},
                           'sz000002', start=DAY, end=DAY)
    with pytest.raises(ValueError, match='conflicts'):
        with psycopg2.connect(**empty_source_db) as conn:
            conn.set_session(isolation_level='SERIALIZABLE')
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                publish_history(cur, second + conflicting)
    with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
        cur.execute('SELECT symbol,close FROM pams_ohlcv_daily')
        assert cur.fetchall() == [('sz000001', Decimal('10.2000'))]
        cur.execute('SELECT count(*) FROM market_observations')
        assert cur.fetchone()[0] == 1


def test_scheduled_history_retries_only_missing_completed_session(empty_source_db, monkeypatch):
    from scripts.bootstrap_isolated_sources import initialize_isolated_sources

    def connect():
        return psycopg2.connect(**empty_source_db)

    with connect() as conn:
        initialize_isolated_sources(conn)
    day = date(2026, 11, 6)
    zone = ZoneInfo('Asia/Shanghai')
    events = [{'trade_date': day, 'trade_type': 'buy', 'symbol': '000001',
               'quantity': Decimal(100), 'source_market': '深圳Ａ股'}]
    monkeypatch.setattr(refresh, 'read_reconciled_events', lambda cur: events)
    calls = []
    ready = False

    def fetch(symbol, *, start, end):
        calls.append((symbol, start, end))
        if symbol == 'sz000001' and not ready:
            raise ValueError('provider has not published the day')
        raw = [str(day), '10.0', '10.2', '10.5', '9.9', '100']
        return parse_history({'code': 0, 'data': {symbol: {'day': [raw]}}}, symbol, start=start, end=end)

    monkeypatch.setattr(refresh, 'fetch_history', fetch)
    assert refresh.refresh_history(connect, now=datetime(2026, 11, 6, 15, 4, tzinfo=zone), start=day)['rows'] == 0
    assert not calls
    kwargs = {'now': datetime(2026, 11, 6, 15, 5, tzinfo=zone), 'start': day}
    first = refresh.refresh_history(connect, **kwargs)
    assert (first['status'], first['rows'], first['missing']) == ('incomplete', 1, 1)
    ready = True
    calls.clear()
    second = refresh.refresh_history(connect, **kwargs)
    assert (second['status'], second['rows']) == ('ok', 1)
    assert calls == [('sz000001', day, day)]
    calls.clear()
    assert refresh.refresh_history(connect, **kwargs)['rows'] == 0
    assert not calls
