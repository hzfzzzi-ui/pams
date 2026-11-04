"""Fetch Tencent closes for reviewed Tonghuashun positions. Default is a read-only plan."""
from argparse import ArgumentParser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

from psycopg2.extras import RealDictCursor

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend/api'))
from domain.imports.account_import_store import get_conn
from domain.imports.isolation import account_import_scope, source_enabled
from domain.investments.nav_trade_source_v2 import CALENDAR, read_reconciled_events, securities_by_symbol
from domain.market.tencent_stock_history import fetch_history, publish_history


def source_scope():
    with account_import_scope(), get_conn() as conn:
        conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if not source_enabled(cur):
                raise ValueError('source database not enabled')
            events = read_reconciled_events(cur)
            symbols = securities_by_symbol(events) if events else None
            if not symbols:
                raise ValueError('Tonghuashun source evidence unavailable')
    return events, symbols


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    events, symbols = source_scope()
    start = events[0]['trade_date']
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    end = now.date() if now.time() >= time(15, 5) else now.date() - timedelta(days=1)
    instruments = sorted(set(symbols.values()) | {CALENDAR})
    with ThreadPoolExecutor(max_workers=6) as workers:
        history = [row for series in workers.map(lambda symbol: fetch_history(symbol, start=start, end=end),
                                                  instruments) for row in series]
    if args.apply:
        with account_import_scope(), get_conn() as conn:
            conn.set_session(isolation_level='SERIALIZABLE')
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                if not source_enabled(cur) or read_reconciled_events(cur) != events:
                    raise ValueError('source changed during quote retrieval')
                print(publish_history(cur, history))
    else:
        print({'symbols': len(instruments), 'observations': len(history), 'from': str(start), 'through': str(end)})


if __name__ == '__main__':
    main()
