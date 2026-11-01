"""Explicit disposable-database CLI for TradingView source experiments."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
from uuid import uuid4

import psycopg2
from psycopg2.extensions import parse_dsn
from psycopg2.extras import RealDictCursor

from domain.imports import isolation
from domain.imports.tradingview_source import stage_csvs
from domain.imports.tradingview_source_publish import inspect_batch, publish, review_row


def main(argv=None):
    parser = argparse.ArgumentParser(description='TradingView isolated source candidate')
    commands = parser.add_subparsers(dest='action', required=True)
    stage = commands.add_parser('stage')
    stage.add_argument('--account', required=True)
    stage.add_argument('csv', nargs='+')
    commit = commands.add_parser('publish')
    commit.add_argument('batch_id')
    commit.add_argument('--confirm', action='store_true', required=True)
    inspect = commands.add_parser('inspect')
    inspect.add_argument('batch_id')
    inspect.add_argument('--file-id')
    review = commands.add_parser('review')
    review.add_argument('batch_id')
    review.add_argument('item_id')
    review.add_argument('--revision', type=int, required=True)
    review.add_argument('--note', required=True)
    args = parser.parse_args(argv)
    mode = isolation.source_mode()
    if mode is None:
        raise ValueError('explicit isolated source mode required')
    files = [(Path(value).name, Path(value).read_bytes()) for value in args.csv] if args.action == 'stage' else None
    if mode == 'development':
        from collectors.tradingview_portfolio import get_conn
        conn = get_conn()
    else:
        dsn = os.environ.get('PAMS_TEST_POSTGRES_DSN', '')
        params = parse_dsn(dsn)
        if (params.get('host') not in {'127.0.0.1', 'localhost', '::1'} or
            not re.fullmatch(r'pams_import_test_[0-9a-f]{32}', params.get('dbname', ''))):
            raise ValueError('explicit disposable TradingView database required')
        conn = psycopg2.connect(dsn, connect_timeout=5)
    with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        if args.action == 'stage':
            result = stage_csvs(cur, batch_id=uuid4(), account_code=args.account, files=files)
        elif args.action == 'inspect':
            result = inspect_batch(cur, batch_id=args.batch_id, file_id=args.file_id)
        elif args.action == 'review':
            result = review_row(cur, batch_id=args.batch_id, item_id=args.item_id,
                                expected_revision=args.revision, note=args.note)
        else:
            result = publish(cur, batch_id=args.batch_id, confirm=args.confirm)
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
