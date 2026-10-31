"""Initialize a specifically marked disposable database with source candidates.

This is an isolated-development bootstrap, not an upgrade or deployment tool.
"""
from __future__ import annotations

import argparse
import os
import sys
from contextlib import closing
from pathlib import Path

import psycopg2
from psycopg2.extensions import TRANSACTION_STATUS_IDLE

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'backend/api'))
from domain.imports.isolation import source_enabled

from scripts.bootstrap_empty_database import (
    BootstrapError,
    _initialize_empty_database,
    bootstrap_paths,
)


def isolated_source_paths() -> list[Path]:
    paths = bootstrap_paths()
    return [*paths, ROOT / 'database/postgres/schema/080_unified_source_staging.sql',
            ROOT / 'database/postgres/schema/081_unified_source_fact_links.sql']


def initialize_isolated_sources(conn) -> None:
    """Require the shared isolation gate, then initialize all schemas atomically."""
    if conn.info.transaction_status != TRANSACTION_STATUS_IDLE or conn.autocommit:
        raise BootstrapError('isolated source bootstrap requires an idle transaction')
    try:
        with conn.cursor() as cur:
            if not source_enabled(cur):
                raise BootstrapError('isolated source bootstrap is disabled')
    finally:
        conn.rollback()
    _initialize_empty_database(conn, isolated_source_paths(), 'pams-isolated-account-import')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if not args.apply:
        parser.error('--apply is required')
    dsn = os.environ.get('PAMS_SOURCE_BOOTSTRAP_DSN', '').strip()
    if not dsn:
        parser.error('PAMS_SOURCE_BOOTSTRAP_DSN must explicitly select the isolated target')
    with closing(psycopg2.connect(dsn, connect_timeout=5)) as conn:
        initialize_isolated_sources(conn)


if __name__ == '__main__':
    main()
