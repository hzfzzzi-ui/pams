from datetime import date
from decimal import Decimal
from pathlib import Path
import sys

import psycopg2
from psycopg2.extras import RealDictCursor
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend/api'))
from domain.market.tencent_stock_history import parse_history, publish_history
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
