"""Validate or explicitly publish supplemental Flex evidence; never import facts."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "api"))
from services.ibkr_daily_close import MAX_XML_BYTES, parse_daily_close_evidence, parse_daily_close_series  # noqa: E402
from services.ibkr_daily_close_read import MAX_MANIFEST_BYTES, read_bounded, validate_close_day  # noqa: E402

LOCK_WAIT_SECONDS = 10


@contextmanager
def publication_lock(directory: Path):
    """OS ownership, not lock-file existence; serialized check and replace."""
    with (directory / ".daily-close.lock").open("a+b") as handle:
        if not handle.seek(0, os.SEEK_END):
            handle.write(b"0")
            handle.flush()
        if sys.platform == "win32":
            import msvcrt
            def acquire():
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            def release():
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            def acquire():
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            def release():
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while True:
            try:
                acquire()
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise ValueError("publication_lock_timeout") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            release()


def publish(content: bytes, *, directory: Path, account_id: str, as_of: date,
            period_start: date) -> dict:
    """An explicit local publication, not production configuration activation."""
    if len(content) > MAX_XML_BYTES:
        raise ValueError("xml_size_limit")
    validate_close_day(as_of)
    digest = hashlib.sha256(content).hexdigest()
    result = parse_daily_close_evidence(content.decode("utf-8"), account_id=account_id,
        as_of=as_of, period_start=period_start, expected_sha256=digest)
    if result["status"] != "ok" or as_of > date.today():
        raise ValueError("daily_close_or_period_not_proven")
    series = parse_daily_close_series(content.decode("utf-8"), account_id=account_id, expected_sha256=digest)
    if series["status"] != "ok" or max(series["daily_closes"], default="") != as_of.isoformat():
        raise ValueError("publication_closing_date_mismatch")
    directory.mkdir(parents=True, exist_ok=True)
    directory = directory.resolve(strict=True)
    with publication_lock(directory):
        return _publish_locked(content, directory=directory, account_id=account_id, as_of=as_of,
                               period_start=period_start, digest=digest)


def _publish_locked(content: bytes, *, directory: Path, account_id: str, as_of: date,
                    period_start: date, digest: str) -> dict:
    target = directory / "daily-close.json"
    if target.exists():
        previous = json.loads(read_bounded(target, MAX_MANIFEST_BYTES).decode("utf-8"))
        if (type(previous.get("version")) is not int or previous.get("version") != 1
                or previous.get("purpose") != "ibkr_daily_close"
                or previous.get("source_account_id") != account_id
                or previous.get("account_code") != "ibkr"
                or date.fromisoformat(previous["as_of"]) > as_of
                or date.fromisoformat(previous["period_start"]) < period_start):
            raise ValueError("publication_scope_or_date_regression")
        # Same-date replacements must not silently rewrite the evidence used by
        # consumers. A revised official close needs an independently approved act.
        if previous["as_of"] == as_of.isoformat() and previous.get("source_sha256") != digest:
            raise ValueError("publication_same_date_source_conflict")
        previous_source = previous.get("source_file")
        if (not isinstance(previous_source, str) or Path(previous_source).name != previous_source
                or previous_source != previous.get("source_sha256", "") + ".xml"):
            raise ValueError("invalid_previous_publication")
        old_path = (directory / previous_source).resolve(strict=True)
        if (old_path.parent != directory
                or hashlib.sha256(read_bounded(old_path, MAX_XML_BYTES)).hexdigest() != previous["source_sha256"]):
            raise ValueError("previous_source_integrity_failure")
    name = digest + ".xml"
    source = directory / name
    if source.is_symlink():
        raise ValueError("invalid_source_path")
    try:
        with source.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if read_bounded(source, MAX_XML_BYTES) != content:
            raise ValueError("immutable_source_conflict") from None
    manifest = {"version": 1, "purpose": "ibkr_daily_close", "account_code": "ibkr",
        "source_account_id": account_id, "as_of": as_of.isoformat(),
        "period_start": period_start.isoformat(), "source_file": name, "source_sha256": digest}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                          prefix=".daily-close-", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(manifest, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return {"status": "published", "manifest": str(target), "source_sha256": digest,
            "production_activated": False, "facts_written": 0}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--xml", type=Path)
    source.add_argument("--download-query-id")
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--as-of", required=True, type=date.fromisoformat)
    parser.add_argument("--period-start", required=True, type=date.fromisoformat)
    parser.add_argument("--publish-dir", type=Path, help="Explicit publication; default validates only")
    args = parser.parse_args()
    try:
        if args.xml:
            if args.xml.stat().st_size > MAX_XML_BYTES:
                raise ValueError("xml_size_limit")
            content = read_bounded(args.xml, MAX_XML_BYTES)
        else:
            from collectors.ibkr_flex import request_flex_reference, fetch_flex_statement
            token = os.environ.get("IBKR_FLEX_TOKEN")
            production = {value.strip() for name in ("IBKR_FLEX_POSITIONS_QUERY_ID",
                "IBKR_FLEX_OPEN_POSITIONS_QUERY_ID", "IBKR_FLEX_QUERY_ID")
                if (value := os.environ.get(name)) and value.strip()}
            query = str(args.download_query_id).strip()
            production = {value.lstrip("0") for value in production}
            if (not token or not production or not query.isascii() or not query.isdigit()
                    or not query.lstrip("0") or query.lstrip("0") in production):
                raise ValueError("separate_audit_query_and_existing_token_required")
            reference = request_flex_reference(token, query)
            code = reference.get("reference_code")
            if not code:
                raise ValueError("missing_flex_reference")
            content = fetch_flex_statement(token, code).encode("utf-8")
        if args.publish_dir:
            result = publish(content, directory=args.publish_dir, account_id=args.account_id,
                             as_of=args.as_of, period_start=args.period_start)
        else:
            result = parse_daily_close_evidence(content.decode("utf-8"), account_id=args.account_id,
                as_of=args.as_of, period_start=args.period_start,
                expected_sha256=hashlib.sha256(content).hexdigest())
        print(json.dumps(result, default=lambda v: str(v) if isinstance(v, Decimal) else v, sort_keys=True))
        return 0 if result["status"] in {"ok", "published"} else 2
    except Exception as exc:
        # Flex/transport exceptions can include credential-bearing request URLs.
        print(json.dumps({"status": "unavailable", "error_type": type(exc).__name__,
                          "facts_written": 0}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
