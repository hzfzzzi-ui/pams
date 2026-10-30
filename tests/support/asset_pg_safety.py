"""Explicitly isolated PostgreSQL test targets; never use application settings."""
from __future__ import annotations

import os
import re
from pathlib import PurePosixPath
from typing import Mapping

from psycopg2.extensions import parse_dsn
from psycopg2.extras import RealDictCursor


MARKER = "pams-isolated-asset-identity"
CI_DATABASE = "pams_asset_test_ci"
_ALLOWED_DSN_KEYS = {"host", "port", "dbname", "user", "password"}


def resolve_asset_test_target(env: Mapping[str, str] | None = None) -> tuple[str, str, str] | None:
    env = os.environ if env is None else env
    local = env.get("PAMS_ASSET_ISOLATED_TEST_DSN", "").strip()
    ci_dsn = env.get("PAMS_ASSET_CI_TEST_DSN", "").strip()
    in_github_ci = env.get("CI") == "true" and env.get("GITHUB_ACTIONS") == "true"
    if local and ci_dsn:
        raise ValueError("ambiguous asset test database configuration")
    if not local and not ci_dsn:
        if in_github_ci:
            raise ValueError("explicit GitHub asset test database is required")
        return None
    if any(env.get(key) for key in ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS")):
        raise ValueError("refusing libpq overrides for isolated asset tests")
    dsn = local or ci_dsn
    try:
        params = parse_dsn(dsn)
    except Exception as exc:
        raise ValueError("invalid asset test database configuration") from exc
    if set(params) - _ALLOWED_DSN_KEYS:
        raise ValueError("unsupported asset test connection parameters")
    host = params.get("host", "")
    name = params.get("dbname", "")
    if local:
        if (not host.startswith("/tmp/pams-asset-test-")
                or str(PurePosixPath(host)) != host
                or ".." in PurePosixPath(host).parts
                or not re.fullmatch(r"pams_asset_test_[a-z0-9_]+", name)):
            raise ValueError("refusing non-isolated Unix-socket asset test database")
        return dsn, "unix", name
    if not in_github_ci:
        raise ValueError("asset CI database requires explicit GitHub Actions environment")
    if (host, params.get("port"), name, params.get("user"), params.get("password")) != (
        "127.0.0.1", "5432", CI_DATABASE, "pams", "pams_ci"
    ):
        raise ValueError("refusing asset CI database outside the dedicated local service")
    return dsn, "github-ci", name


def validate_asset_test_database(conn, mode: str, name: str) -> None:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT current_database() AS name, current_user AS role, inet_server_addr() AS host, inet_server_port() AS port, shobj_description(oid, 'pg_database') AS marker FROM pg_database WHERE datname = current_database()")
        identity = cur.fetchone()
    if not identity or identity["name"] != name or identity["marker"] != MARKER or conn.server_version // 10000 != 16:
        raise ValueError("refusing unmarked or incompatible asset test database")
    if mode == "unix" and identity["host"] is None:
        return
    # Docker's published port can reach a service container at its private IP.
    # The client DSN is already restricted to 127.0.0.1 by resolve_asset_test_target.
    if mode == "github-ci" and identity["host"] is not None and identity["port"] == 5432 and identity["role"] == "pams":
        return
    raise ValueError("asset test database transport does not match the isolated target")
