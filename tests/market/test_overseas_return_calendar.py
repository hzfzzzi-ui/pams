import sys
from copy import deepcopy
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from psycopg2.extras import RealDictCursor

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend/api'))
from domain.investments.holding_return_display import attach_recent_market_returns
from domain.investments.holdings_view import advance_overseas_view_date
from domain.market.overseas_return_calendar import (
    latest_completed_session,
    market_bucket,
    return_session_date,
)
from tests.market.test_us_quote_projection import BASE_DAY, QUOTE_DAY, _holdings_bytes, _write_quote
from tests.market.test_us_quote_projection import pg_projection  # noqa: F401

NOW = datetime(2026, 10, 28, 8, 30, tzinfo=UTC)
DAY = date(2026, 10, 27)


@pytest.mark.parametrize('bucket,stamp,expected', [
    ('kr', '2026-10-28T06:44:59+00:00', '2026-10-27'),
    ('kr', '2026-10-28T06:45:00+00:00', '2026-10-28'),
    ('hk', '2026-10-28T08:14:59+00:00', '2026-10-27'),
    ('hk', '2026-10-28T08:15:00+00:00', '2026-10-28'),
    ('se', '2026-06-24T15:44:59+00:00', '2026-06-23'),
    ('se', '2026-06-24T15:45:00+00:00', '2026-06-24'),
    ('se', '2026-10-28T16:45:00+00:00', '2026-10-28'),
    ('ch', '2026-06-24T15:45:00+00:00', '2026-06-24'),
    ('ch', '2026-10-28T16:44:59+00:00', '2026-10-27'),
    ('ch', '2026-10-28T16:45:00+00:00', '2026-10-28'),
    ('hk', '2026-12-24T04:15:00+00:00', '2026-12-24'),
    ('us', '2026-10-25T12:00:00+00:00', '2026-10-23'),
])
def test_close_publication_boundaries_follow_exchange_sessions(bucket, stamp, expected):
    assert latest_completed_session(bucket, datetime.fromisoformat(stamp)) == date.fromisoformat(expected)


@pytest.mark.parametrize('bucket,stamp,expected', [
    ('kr', '2026-10-28T00:00:00+00:00', '2026-10-28'),
    ('hk', '2026-10-28T01:00:00+00:00', '2026-10-27'),
    ('se', '2026-10-28T01:00:00+00:00', '2026-10-27'),
    ('ch', '2026-10-28T08:00:00+00:00', '2026-10-28'),
    ('us', '2026-10-27T22:59:59+00:00', '2026-10-27'),
    ('us', '2026-10-27T23:00:00+00:00', '2026-10-28'),
    ('hk', '2026-10-25T04:00:00+00:00', '2026-10-23'),
])
def test_open_session_dates_are_not_the_us_reset_date(bucket, stamp, expected):
    assert return_session_date(bucket, datetime.fromisoformat(stamp)) == date.fromisoformat(expected)


@pytest.mark.parametrize('row,expected', [
    ({'symbol': 'LOCAL.ST', 'currency': 'CHF'}, 'se'),
    ({'symbol': 'LOCAL.SW', 'currency': 'SEK'}, 'ch'),
    ({'symbol': 'LOCAL.KS', 'currency': 'USD'}, 'kr'),
    ({'symbol': '3690', 'major_category': '美股', 'currency': 'USD'}, 'hk'),
    ({'symbol': 'LOCAL', 'listing_region': 'Switzerland'}, 'ch'),
    ({'symbol': 'LOCAL', 'major_category': '美股', 'currency': 'SEK'}, 'se'),
    ({'symbol': '123456', 'major_category': 'A股', 'currency': 'USD'}, None),
    ({'symbol': 'CASH_CHF', 'major_category': '美股', 'currency': 'CHF'}, None),
    ({'symbol': 'LOCAL', 'major_category': '美股', 'currency': 'EUR'}, None),
])
def test_listing_identity_is_not_replaced_by_currency(row, expected):
    assert market_bucket(row) == expected


def holding(symbol='LOCAL.ST', currency='SEK', day=DAY, **changes):
    return dict(account='broker', symbol=symbol, currency=currency, major_category='美股',
                quantity='5', snapshot_as_of=str(day), daily_pnl_stale=True,
                daily_return_status='pending', daily_pnl_cny='0', **changes)


def historical(symbol='LOCAL.ST', currency='SEK', day=DAY):
    row = holding(symbol, currency, day)
    row.update(daily_pnl_stale=False, current_price='22', previous_price='20',
               daily_pnl='10', daily_pnl_native='10', daily_pnl_cny='6.5',
               daily_fx_pnl_cny='-0.5', daily_total_pnl_cny='6',
               price_change_pct='0.10', price_change_amount='2')
    return row


class Cursor:
    def __init__(self, closes=None, exists=True):
        self.queries, self.exists = [], exists
        self.closes = closes or [dict(symbol='LOCAL.ST', trade_date=DAY, close='22'),
                                 dict(symbol='LOCAL.ST', trade_date=date(2026, 10, 26), close='20')]

    def execute(self, sql, params=None):
        assert sql.lstrip().startswith(('SELECT', 'WITH'))
        assert not any(word in sql.upper() for word in ('INSERT ', 'UPDATE ', 'DELETE '))
        self.queries.append((sql, params))

    def fetchone(self):
        return {'exists': self.exists}

    def fetchall(self):
        return self.closes


def attach(rows=None, sources=None, cursor=None, now=NOW):
    payload = {'as_of': '2026-10-28', 'rows': rows or [holding()]}
    original = deepcopy(payload)
    calls = []
    def load(day):
        calls.append(day)
        return {'rows': sources or [historical()]}
    result = attach_recent_market_returns(cursor or Cursor(), payload, active_date=NOW.date(), load_date=load, now=now)
    assert payload == original
    return result, calls


def test_legacy_non_us_close_requires_matching_consecutive_persisted_prices():
    result, calls = attach()
    recent = result['rows'][0]['recent_confirmed_return']
    assert recent['as_of'] == '2026-10-27' and recent['calendar'] == 'XSTO'
    assert recent['current_as_of'] == '2026-10-28'
    assert recent['fields']['daily_total_pnl_cny'] == '6'
    assert calls == [DAY] and result['rows'][0]['daily_pnl_cny'] == '0'


@pytest.mark.parametrize('field,value', [('previous_price', '19'), ('current_price', '23'),
                                      ('daily_fx_pnl_cny', 'NaN'), ('daily_total_pnl_cny', None),
                                      ('snapshot_as_of', '2026-10-26'), ('daily_pnl_stale', True)])
def test_wrong_missing_or_nonfinite_evidence_does_not_confirm(field, value):
    source = historical()
    source[field] = value
    result, _ = attach(sources=[source])
    assert 'recent_confirmed_return' not in result['rows'][0]


def test_missing_previous_session_is_not_a_daily_return():
    cursor = Cursor()
    cursor.closes[1]['trade_date'] = date(2026, 10, 23)
    result, calls = attach(cursor=cursor)
    assert not calls and 'recent_confirmed_returns' not in result


def test_missing_quote_source_does_not_create_zero_return():
    result, calls = attach(cursor=Cursor(exists=False))
    assert not calls and 'recent_confirmed_returns' not in result


def test_per_market_date_caps_and_dated_account_sums_never_mix_days():
    day = NOW.date()
    rows = [holding(), holding('LOCAL.KS', 'KRW', day)]
    cur = Cursor(closes=[dict(symbol='LOCAL.ST', trade_date=DAY, close='22'),
                         dict(symbol='LOCAL.ST', trade_date=date(2026, 10, 26), close='20'),
                         dict(symbol='LOCAL.KS', trade_date=day, close='22'),
                         dict(symbol='LOCAL.KS', trade_date=DAY, close='20')])
    result, calls = attach(rows, [historical(), historical('LOCAL.KS', 'KRW', day)], cur)
    assert sorted(calls) == [DAY, day]
    assert cur.queries[1][1] == (['LOCAL.KS', 'LOCAL.ST'], [day, DAY])
    account = result['recent_confirmed_returns']['by_account']['broker']
    assert account['as_of'] == str(day) and account['amount_cny'] == '6'
    assert account['partial'] and account['covered'] == 1 and account['expected'] == 2


def test_shared_date_replay_is_bounded_by_dates_not_positions():
    rows = [holding(), {**holding(), 'account': 'other'}]
    result, calls = attach(rows, [historical(), {**historical(), 'account': 'other'}])
    assert len(calls) == 1 and len(result['recent_confirmed_returns']['by_account']) == 2


def test_new_session_estimate_keeps_precedence():
    row = holding()
    row.update(daily_return_status='estimated', daily_pnl_stale=False)
    result, calls = attach(rows=[row])
    assert not calls and 'recent_confirmed_return' not in result['rows'][0]


@pytest.mark.parametrize('changes', [{'account': None}, {'symbol': None}, {'currency': 'USD'}, {'is_closed': True}])
def test_missing_identity_or_incompatible_quote_currency_is_not_dated(changes):
    row = {**holding(), **changes}
    cur = Cursor()
    result, calls = attach(rows=[row], cursor=cur)
    assert not cur.queries and not calls and 'recent_confirmed_return' not in result['rows'][0]


def test_korean_quote_can_advance_view_before_china_cutoff_without_moving_future_quotes():
    row = {'market': 'KR', 'currency': 'KRW', 'quote_date': NOW.date()}
    cur = Cursor(closes=[row])
    just_closed = datetime(2026, 10, 28, 6, 45, tzinfo=UTC)
    assert advance_overseas_view_date(cur, DAY, now=just_closed) == NOW.date()
    assert advance_overseas_view_date(Cursor(closes=[row]), DAY, now=just_closed.replace(minute=44)) == DAY
    assert 'JOIN latest_account' in cur.queries[1][0] and cur.queries[1][1] == (NOW.date(), DAY, NOW.date())


def test_view_does_not_advance_for_wrong_market_currency_or_missing_table():
    cur = Cursor(closes=[{'market': 'KR', 'currency': 'USD', 'quote_date': NOW.date()}])
    assert advance_overseas_view_date(cur, DAY, now=NOW) == DAY
    assert advance_overseas_view_date(Cursor(exists=False), DAY, now=NOW) == DAY


def test_current_date_never_needs_an_extra_calendar_or_quote_query():
    cur = Cursor()
    assert advance_overseas_view_date(cur, NOW.date(), now=NOW) == NOW.date()
    assert cur.queries == []


def test_failed_optional_replay_is_attempted_once_per_date_and_keeps_original(caplog):
    payload = {'rows': [holding(), {**holding(), 'account': 'other'}]}
    calls = []
    def fail(day):
        calls.append(day)
        raise RuntimeError('sensitive-details-not-for-logs')
    result = attach_recent_market_returns(Cursor(), payload, active_date=NOW.date(), load_date=fail, now=NOW)
    assert calls == [DAY] and result is payload
    assert 'RuntimeError' in caplog.text and 'sensitive-details' not in caplog.text


@pytest.mark.parametrize('symbol,currency,minute,expected', [
    ('005930.KS', 'KRW', 44, BASE_DAY), ('005930.KS', 'KRW', 45, QUOTE_DAY),
    ('NOT_HELD.KS', 'KRW', 45, BASE_DAY), ('005930.KS', 'USD', 45, BASE_DAY),
])
def test_pg16_early_market_cutoff_requires_current_member_and_completed_price(request, symbol, currency, minute, expected):
    conn = request.getfixturevalue('pg_projection')
    _write_quote(conn, symbol, market='KR', currency=currency, source='yahoo_close', return_status='confirmed')
    physical = _holdings_bytes(conn)
    class TempCursor:
        def __init__(self, cur):
            self.cur = cur
        def execute(self, query, params=None):
            # Only adapt the production table-existence probe to the isolated
            # session's TEMP schema; execute all membership/price SQL verbatim.
            return self.cur.execute(query.replace("to_regclass('public.market_observations')",
                                                  "to_regclass('market_observations')"), params)
        def fetchone(self):
            return self.cur.fetchone()
        def fetchall(self):
            return self.cur.fetchall()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        result = advance_overseas_view_date(TempCursor(cur), BASE_DAY,
                                           now=datetime(2026, 10, 26, 6, minute, tzinfo=UTC))
    assert result == expected and _holdings_bytes(conn) == physical
