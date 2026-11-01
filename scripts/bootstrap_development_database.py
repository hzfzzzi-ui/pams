"""Initialize a specifically marked, empty local development database.

This does not upgrade, migrate or import data into an existing database.
The database itself must already exist and carry the development marker.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import os
from pathlib import Path
import re
import sys

import psycopg2
from psycopg2.extensions import TRANSACTION_STATUS_IDLE, parse_dsn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'backend/api'))
from domain.imports.isolation import IsolationError, source_enabled, validate_target
from scripts.bootstrap_empty_database import BootstrapError, _initialize_empty_database, bootstrap_paths


MARKER = 'pams-development-sandbox'
_DB_NAME = re.compile(r'pams_dev_[0-9a-f]{32}\Z')
_DSN_KEYS = {'host', 'port', 'dbname', 'user', 'password', 'sslmode'}


def validate_development_dsn(dsn: str) -> None:
    """Reject ambiguous or nonlocal connection information before connecting."""
    try:
        params = parse_dsn(dsn)
    except psycopg2.ProgrammingError as exc:
        raise BootstrapError('invalid development database DSN') from exc
    if (set(params) - _DSN_KEYS or params.get('host') not in {'127.0.0.1', 'localhost', '::1'}
            or not _DB_NAME.fullmatch(params.get('dbname', ''))
            or not params.get('user') or not params.get('port', '').isdigit()
            or not 1 <= int(params['port']) <= 65535):
        raise BootstrapError('explicit local development database DSN required')
    if any(os.environ.get(key) for key in ('PGHOSTADDR', 'PGSERVICE', 'PGSERVICEFILE', 'PGOPTIONS')):
        raise BootstrapError('libpq connection overrides are not allowed')
    try:
        validate_target(params, mode='development')
    except IsolationError as exc:
        raise BootstrapError('invalid local development database target') from exc


def initialize_development_database(conn) -> None:
    """Initialize a new marked development database in one transaction."""
    if conn.info.transaction_status != TRANSACTION_STATUS_IDLE or conn.autocommit:
        raise BootstrapError('development bootstrap requires an idle transaction')
    try:
        with conn.cursor() as cur:
            if not source_enabled(cur):
                raise BootstrapError('development source mode is disabled')
    finally:
        conn.rollback()
    _initialize_empty_database(conn, bootstrap_paths(), MARKER)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if not args.apply:
        parser.error('--apply is required')
    if os.environ.get('PAMS_SOURCE_MODE', '').lower() != 'development':
        parser.error('PAMS_SOURCE_MODE=development is required')
    dsn = os.environ.get('PAMS_DEV_BOOTSTRAP_DSN', '').strip()
    if not dsn:
        parser.error('PAMS_DEV_BOOTSTRAP_DSN must explicitly select the development database')
    try:
        validate_development_dsn(dsn)
        with closing(psycopg2.connect(dsn, connect_timeout=5)) as conn:
            initialize_development_database(conn)
    except (psycopg2.Error, ValueError, BootstrapError) as exc:
        print('Development bootstrap failed: ' + type(exc).__name__ + '; no changes confirmed', file=sys.stderr)
        return 1
    print('Development database initialized successfully')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
