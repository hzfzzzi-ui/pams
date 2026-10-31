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

from domain.imports.tradingview_source_candidate import stage_csvs
from domain.imports.tradingview_source_publish import publish


def main(argv=None):
    parser = argparse.ArgumentParser(description='TradingView isolated source candidate')
    commands = parser.add_subparsers(dest='action', required=True)
    stage = commands.add_parser('stage')
    stage.add_argument('--account', required=True)
    stage.add_argument('--allow-existing', action='store_true')
    stage.add_argument('csv', nargs='+')
    commit = commands.add_parser('publish')
    commit.add_argument('batch_id')
    commit.add_argument('--confirm', action='store_true', required=True)
    commit.add_argument('--allow-existing', action='store_true')
    args = parser.parse_args(argv)
    if os.environ.get('PAMS_UNIFIED_SOURCE_ISOLATED', '').lower() != 'true':
        raise ValueError('explicit isolated source mode required')
    dsn = os.environ.get('PAMS_TEST_POSTGRES_DSN', '')
    params = parse_dsn(dsn)
    if (params.get('host') not in {'127.0.0.1', 'localhost', '::1'} or
        not re.fullmatch(r'pams_import_test_[0-9a-f]{32}', params.get('dbname', ''))):
        raise ValueError('explicit disposable TradingView database required')
    files = [(Path(value).name, Path(value).read_bytes()) for value in args.csv] if args.action == 'stage' else None
    with psycopg2.connect(dsn, connect_timeout=5) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        if args.action == 'stage':
            result = stage_csvs(cur, batch_id=uuid4(), account_code=args.account, files=files,
                                allow_existing=args.allow_existing)
        else:
            result = publish(cur, batch_id=args.batch_id, confirm=args.confirm, allow_existing=args.allow_existing)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
