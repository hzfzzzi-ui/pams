"""Initialize a separately marked empty PostgreSQL database; never an upgrade tool."""
from __future__ import annotations

import argparse
from contextlib import closing
import os
from pathlib import Path
import re
import sys

import psycopg2
from psycopg2.extensions import TRANSACTION_STATUS_IDLE

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "database/postgres/bootstrap/manifest.txt"
MARKER = "pams-empty-bootstrap"
_WRAPPED = {
    "028_account_references.sql", "029_holding_asset_reference.sql",
    "067_fund_nav_estimates.sql", "069_daily_return_state.sql",
}
_CONTROL = re.compile(r"^\s*(BEGIN|COMMIT|ROLLBACK)\s*;\s*(?:--.*)?$", re.IGNORECASE)


class BootstrapError(RuntimeError):
    """A requested bootstrap could not be safely performed."""


def bootstrap_paths() -> list[Path]:
    paths = []
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        path = (ROOT / line).resolve()
        if not path.is_relative_to(ROOT / "database/postgres") or not path.is_file():
            raise BootstrapError("invalid bootstrap manifest entry")
        paths.append(path)
    if not paths or len(paths) != len(set(paths)):
        raise BootstrapError("empty or duplicate bootstrap manifest")
    return paths


def _transaction_safe_sql(path: Path) -> str:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    controls = []
    for i, line in enumerate(lines):
        match = _CONTROL.match(line)
        if match:
            controls.append((i, match.group(1).upper()))
    if path.name in _WRAPPED:
        if [kind for _, kind in controls] != ["BEGIN", "COMMIT"]:
            raise BootstrapError("unexpected transaction wrapper: " + path.name)
        for i, _ in reversed(controls):
            del lines[i]
    elif controls:
        raise BootstrapError("unexpected transaction statement: " + path.name)
    return "".join(lines)


def initialize_empty_database(conn) -> None:
    if conn.info.transaction_status != TRANSACTION_STATUS_IDLE:
        raise BootstrapError("connection must be idle")
    if conn.autocommit:
        conn.autocommit = False
    paths = bootstrap_paths()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_xact_lock(hashtext('pams-empty-bootstrap'))")
            if not cur.fetchone()[0]:
                raise BootstrapError("another bootstrap is running")
            cur.execute("SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname=current_database()")
            if cur.fetchone()[0] != MARKER:
                raise BootstrapError("target database lacks the empty-bootstrap marker")
            cur.execute("""SELECT count(*) FROM pg_stat_activity
                WHERE datname=current_database() AND pid <> pg_backend_pid()
                  AND backend_type='client backend'""")
            if cur.fetchone()[0]:
                raise BootstrapError("target database has other client connections")
            cur.execute("""SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname NOT IN ('pg_catalog','information_schema')
                  AND n.nspname NOT LIKE 'pg_toast%'
                  AND n.nspname NOT LIKE 'pg_temp_%'
                  AND c.relkind IN ('r','p','v','m','f','S')""")
            if cur.fetchone()[0]:
                raise BootstrapError("target database is not empty")
            for path in paths:
                cur.execute(_transaction_safe_sql(path))
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="initialize the explicitly marked empty database")
    args = parser.parse_args()
    if not args.apply:
        parser.error("--apply is required")
    dsn = os.environ.get("PAMS_BOOTSTRAP_DSN", "").strip()
    if not dsn:
        parser.error("PAMS_BOOTSTRAP_DSN must explicitly select the target database")
    try:
        with closing(psycopg2.connect(dsn, connect_timeout=5)) as conn:
            initialize_empty_database(conn)
    except (psycopg2.Error, BootstrapError) as exc:
        print("Bootstrap failed: " + type(exc).__name__ + "; commit was not confirmed", file=sys.stderr)
        return 1
    print("Empty database initialized successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
