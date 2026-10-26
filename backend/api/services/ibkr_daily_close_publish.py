"""Atomic, monotonic publication of independently validated Flex close evidence."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time

from services.ibkr_daily_close import MAX_XML_BYTES, parse_daily_close_evidence, parse_daily_close_series
from services.ibkr_daily_close_read import MAX_MANIFEST_BYTES, read_bounded, validate_close_day


@contextmanager
def publication_lock(directory: Path, *, wait_seconds: float = 10):
    """Use OS ownership, not lock-file existence, across both API workers."""
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
        deadline = time.monotonic() + wait_seconds
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
            period_start: date, preserve_history: bool = False) -> dict:
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
                               period_start=period_start, digest=digest,
                               historical_series=series if preserve_history else None)


def _publish_locked(content: bytes, *, directory: Path, account_id: str, as_of: date,
                    period_start: date, digest: str, historical_series: dict | None = None) -> dict:
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
        if historical_series is not None:
            old_series = parse_daily_close_series(read_bounded(old_path, MAX_XML_BYTES).decode("utf-8"),
                account_id=account_id, expected_sha256=previous["source_sha256"])
            if (old_series["status"] != "ok" or any(
                _financial_close(close) != _financial_close(historical_series["daily_closes"].get(day, {}))
                for day, close in old_series["daily_closes"].items()
            )):
                raise ValueError("publication_historical_revision_requires_review")
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


def _financial_close(close: dict) -> dict:
    # Source identity changes with a new report; historical financial proof must not.
    return {key: value for key, value in close.items() if key != "source_sha256"}
