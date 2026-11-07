"""Keep the verified Tonghuashun NAV inputs current after the A-share close."""

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
from decimal import Decimal

import exchange_calendars as xcals
from psycopg2.extras import RealDictCursor

from domain.investments.nav_trade_source_v2 import CALENDAR, read_reconciled_events, securities_by_symbol
from domain.market.tencent_stock_history import SOURCE, fetch_history, publish_history


def _required_days(events, symbols, sessions):
    required = defaultdict(set)
    quantities = defaultdict(Decimal)
    event_iter = iter(events)
    event = next(event_iter, None)
    for day in sessions:
        while event is not None and event['trade_date'] <= day:
            if event['trade_type'] in {'buy', 'sell'}:
                direction = 1 if event['trade_type'] == 'buy' else -1
                quantities[event['symbol']] += event['quantity'] * direction
            event = next(event_iter, None)
        required[CALENDAR].add(day)
        for symbol, quantity in quantities.items():
            if quantity > 0:
                required[symbols[symbol]].add(day)
    return required


def refresh_history(connect, *, now, start=None):
    """Fetch only missing evidence for recent completed sessions, then publish atomically."""
    end = now.date()
    start = start or end - timedelta(days=9)
    calendar = xcals.get_calendar('XSHG')
    sessions = [session.date() for session in calendar.sessions_in_range(start.isoformat(), end.isoformat())
                if calendar.session_close(session).to_pydatetime() + timedelta(minutes=5) <= now]
    if not sessions:
        return {'status': 'ok', 'rows': 0}
    with closing(connect()) as conn:
        conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
        with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT to_regclass('public.source_records') IS NOT NULL AS ready")
            if not cur.fetchone()['ready']:
                return {'status': 'source_unavailable', 'rows': 0}
            events = read_reconciled_events(cur)
            symbols = securities_by_symbol(events) if events else None
            if not symbols:
                return {'status': 'source_unavailable', 'rows': 0}
            required = _required_days(events, symbols, [day for day in sessions if day >= events[0]['trade_date']])
            cur.execute("""SELECT symbol, as_of FROM market_observations
                WHERE observation_type='daily' AND source=%s AND market='CN_A' AND currency='CNY'
                  AND symbol=ANY(%s) AND as_of=ANY(%s) AND close_price>0 AND source_hash IS NOT NULL""",
                        (SOURCE, list(required), sessions))
            for row in cur.fetchall():
                required[row['symbol']].discard(row['as_of'])

    missing = [(symbol, days) for symbol, days in sorted(required.items()) if days]
    if not missing:
        return {'status': 'ok', 'rows': 0, 'as_of': sessions[-1].isoformat()}

    def fetch(item):
        symbol, days = item
        try:
            rows = fetch_history(symbol, start=min(days), end=max(days))
        except (OSError, ValueError):
            return []
        return [row for row in rows if row['date'] in days]

    with ThreadPoolExecutor(max_workers=6) as workers:
        rows = [row for batch in workers.map(fetch, missing) for row in batch]
    with closing(connect()) as conn:
        conn.set_session(isolation_level='SERIALIZABLE')
        with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            result = publish_history(cur, rows) if rows else {'rows': 0}
    pending = sum(len(days) for _, days in missing) - len(rows)
    return {**result, 'status': 'incomplete' if pending else 'ok', 'missing': pending,
            'as_of': sessions[-1].isoformat()}
