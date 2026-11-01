"""Database boundaries for source operations in test and local development."""
from collections.abc import Mapping
import os
import re
import socket
from contextlib import contextmanager
from contextvars import ContextVar
from ipaddress import ip_address

import psycopg2
from psycopg2.extensions import parse_dsn


class IsolationError(ValueError):
    """The requested source database is outside its explicitly designated mode."""


_TEST_NAME = re.compile(r'pams_import_test_[0-9a-f]{32}')
_DEV_NAME = re.compile(r'pams_dev_[0-9a-f]{32}')
_HOSTS = {'127.0.0.1', 'localhost', '::1'}
_LIBPQ_OVERRIDES = ('PGHOSTADDR', 'PGSERVICE', 'PGSERVICEFILE', 'PGOPTIONS')
_account_import_scope = ContextVar('account_import_scope', default=False)


@contextmanager
def account_import_scope():
    """Limit runtime source access to an account-import request."""
    token = _account_import_scope.set(True)
    try:
        yield
    finally:
        _account_import_scope.reset(token)


def runtime_import_active() -> bool:
    return _account_import_scope.get() and os.environ.get('PAMS_IMPORT_RUNTIME_ENABLED', '').lower() == 'true'


def source_mode() -> str | None:
    """Return the explicitly selected mode, rejecting ambiguous configuration."""
    selected = os.environ.get('PAMS_SOURCE_MODE', '').strip().lower()
    isolated = os.environ.get('PAMS_UNIFIED_SOURCE_ISOLATED', '').lower() == 'true'
    if selected not in {'', 'development'}:
        raise IsolationError('unsupported source mode')
    if selected == 'development' and isolated:
        raise IsolationError('source modes cannot be enabled together')
    return selected or ('test' if isolated else None)


def validate_target(params: Mapping[str, object], *, mode: str) -> None:
    """Reject unsafe targets before connecting; never resolve arbitrary hosts."""
    host = params.get('host')
    name = params.get('dbname')
    pattern = _DEV_NAME if mode == 'development' else _TEST_NAME
    if host not in _HOSTS or not isinstance(name, str) or not pattern.fullmatch(name):
        raise IsolationError('source mode requires a designated local database')
    if mode == 'development':
        if any(os.environ.get(key) for key in _LIBPQ_OVERRIDES):
            raise IsolationError('libpq overrides are not permitted in development mode')
        if any(params.get(key) for key in ('hostaddr', 'service', 'options')):
            raise IsolationError('libpq target overrides are not permitted')
        if host == 'localhost':
            try:
                addresses = socket.getaddrinfo('localhost', None, type=socket.SOCK_STREAM)
                if not addresses or any(not ip_address(entry[4][0]).is_loopback for entry in addresses):
                    raise IsolationError('localhost does not resolve exclusively to loopback')
            except (OSError, ValueError) as exc:
                raise IsolationError('cannot validate localhost as loopback') from exc


def source_enabled(cur) -> bool:
    mode = source_mode()
    if mode is None:
        if not runtime_import_active():
            return False
        expected_db = os.environ.get('PAMS_IMPORT_RUNTIME_DATABASE', '').strip()
        params = cur.connection.get_dsn_parameters()
        if not expected_db or params.get('dbname') != expected_db or cur.connection.server_version // 10000 != 16:
            raise IsolationError('account import runtime target does not match')
        cur.execute("SELECT shobj_description(oid, 'pg_database') AS marker FROM pg_database WHERE datname=current_database()")
        row = cur.fetchone()
        marker = row.get('marker') if isinstance(row, Mapping) else row[0] if row else None
        if marker != 'pams-empty-bootstrap':
            raise IsolationError('account import runtime database marker does not match')
        return True
    params = cur.connection.get_dsn_parameters()
    validate_target(params, mode=mode)
    if cur.connection.server_version // 10000 != 16:
        raise IsolationError('source mode requires PostgreSQL 16')
    cur.execute("SELECT shobj_description(oid, 'pg_database') AS marker FROM pg_database WHERE datname=current_database()")
    row = cur.fetchone()
    marker = row.get('marker') if isinstance(row, Mapping) else row[0] if row else None
    expected = 'pams-development-sandbox' if mode == 'development' else 'pams-isolated-account-import'
    if marker != expected:
        raise IsolationError('source database marker does not match its mode')
    return True


def guarded_connect(dsn=None, **kwargs):
    """Preserve existing connections; in dev validate before and after connect."""
    mode = source_mode()
    if mode != 'development':
        return psycopg2.connect(dsn, **kwargs) if dsn is not None else psycopg2.connect(**kwargs)
    try:
        params = parse_dsn(dsn) if dsn else {}
    except psycopg2.Error as exc:
        raise IsolationError('invalid development database target') from exc
    params.update(kwargs)
    validate_target(params, mode=mode)
    conn = psycopg2.connect(dsn, **kwargs) if dsn is not None else psycopg2.connect(**kwargs)
    try:
        with conn.cursor() as cur:
            if not source_enabled(cur):
                raise IsolationError('development source mode is disabled')
        # The marker check starts a transaction. Callers may set isolation.
        conn.rollback()
        return conn
    except BaseException:
        conn.close()
        raise


def require_external_access():
    """Fail closed before contacting live brokers or market providers in development."""
    if source_mode() == "development":
        raise IsolationError("External synchronization is disabled in development mode")
