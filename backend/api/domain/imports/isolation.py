"""Independent safety gate for temporary source-layer experiments."""
from collections.abc import Mapping
import os
import re


class IsolationError(ValueError):
    """The experimental source bridge was requested outside its test database."""


def source_enabled(cur) -> bool:
    if os.environ.get('PAMS_UNIFIED_SOURCE_ISOLATED', '').lower() != 'true':
        return False
    params = cur.connection.get_dsn_parameters()
    if (params.get('host') not in {'127.0.0.1', 'localhost', '::1'} or
        not re.fullmatch(r'pams_import_test_[0-9a-f]{32}', params.get('dbname', '')) or
        cur.connection.server_version // 10000 != 16):
        raise IsolationError('source experiment requires a disposable test database')
    cur.execute("SELECT shobj_description(oid, 'pg_database') AS marker FROM pg_database WHERE datname=current_database()")
    row = cur.fetchone()
    marker = row.get('marker') if isinstance(row, Mapping) else row[0] if row else None
    if marker != 'pams-isolated-account-import':
        raise IsolationError('source experiment requires a disposable test database')
    return True
