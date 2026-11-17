"""Account synchronization and source review reads."""
from __future__ import annotations

import os
from contextlib import closing
from decimal import Decimal
from typing import Any, Literal

import psycopg2
import psycopg2.extras
from fastapi import APIRouter
from domain.accounts.sync_status import read_account_sync, review_records
from domain.investments.portfolio_state import _load_accounts_config

router = APIRouter(prefix="/sync", tags=["sync"])


def get_conn():
    from domain.imports.isolation import guarded_connect

    return guarded_connect(
        host=os.environ.get("PGHOST", "pams-postgres"),
        port=int(os.environ.get("PGPORT", "5432")),
        dbname=os.environ.get("PGDATABASE", "pams"),
        user=os.environ.get("PGUSER", "pams"),
        password=os.environ.get("PGPASSWORD", ""),
    )


def _norm(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, list):
        return [_norm(v) for v in value]
    if isinstance(value, dict):
        return {k: _norm(v) for k, v in value.items()}
    return value


@router.get('/accounts')
def account_sync_status():
    with closing(get_conn()) as conn:
        conn.set_session(isolation_level='REPEATABLE READ', readonly=True)
        with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            return _norm(read_account_sync(cur, _load_accounts_config()))


@router.get('/accounts/{account_code}/source-anomalies')
def account_source_anomalies(account_code: str, offset: int = 0,
                             kind: Literal['duplicates', 'cashflow', 'investment', 'balance', 'unposted_manual'] = 'duplicates'):
    from domain.accounts.bank_source_status import anomalies, unposted_manual

    with closing(get_conn()) as conn:
        conn.set_session(isolation_level='REPEATABLE READ', readonly=True)
        with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            offset = max(0, offset)
            bank_reviews = {'duplicates': anomalies, 'unposted_manual': unposted_manual}
            rows = (bank_reviews[kind](cur, account_code, offset) if kind in bank_reviews
                    else review_records(cur, _load_accounts_config(), account_code, kind, offset))
            return _norm({'rows': rows})
