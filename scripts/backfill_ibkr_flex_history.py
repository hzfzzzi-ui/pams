"""Preview or apply one archived IBKR history backfill in a designated database."""
import argparse
from contextlib import closing
from datetime import date
import json
import os
import re
from pathlib import Path
import sys

import psycopg2
from psycopg2.extensions import parse_dsn
from psycopg2.extras import RealDictCursor

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend/api'))
from domain.brokers.ibkr.ibkr_history_backfill import backfill  # noqa: E402
from domain.imports import isolation  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--as-of', required=True, type=date.fromisoformat)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--approved-production', action='store_true')
    parser.add_argument('--expected-trades', type=int)
    parser.add_argument('--expected-cash', type=int)
    args = parser.parse_args()
    if not re.fullmatch('[a-f0-9]{64}', args.sha256):
        parser.error('an exact archive SHA-256 is required')
    dsn = os.environ.get('PAMS_TEST_POSTGRES_DSN', '')
    account = os.environ.get('PAMS_IBKR_HOLDINGS_ACCOUNT_ID', '').strip()
    if not account:
        parser.error('the configured Flex account is required')
    if args.approved_production:
        if (isolation.source_mode() is not None or dsn or args.expected_trades is None or args.expected_cash is None
                or not os.environ.get('PAMS_IMPORT_RUNTIME_DATABASE')):
            parser.error('configured production target and explicit approved counts are required')
        from domain.brokers.ibkr.ibkr_performance import get_conn
        approved = {'trade': args.expected_trades, 'cash': args.expected_cash}
        connection = get_conn()
    else:
        if not dsn or isolation.source_mode() != 'test':
            parser.error('explicit isolated DSN and isolation switch are required')
        isolation.validate_target(parse_dsn(dsn), mode='test')
        approved = None
        connection = psycopg2.connect(dsn, connect_timeout=5)
    with isolation.account_import_scope(), closing(connection) as conn:
        with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            result = backfill(cur, digest=args.sha256, account=account, as_of=args.as_of,
                              apply=args.apply, approved_counts=approved)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
