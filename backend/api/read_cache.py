"""Thread-safe short-TTL response cache for heavy read-only GET endpoints.

Scoped invalidation ensures user writes (imports, ledger edits, transfers,
reviews, trades) clear all relevant caches immediately, while high-frequency
scheduler polling (page-projection/refresh and scheduled price ticks) does not
repeatedly evict closed-month historical reports like /api/assets/monthly-overview.
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
import os
from pathlib import Path
from threading import Lock
import time


CACHEABLE_PREFIX_TTLS: tuple[tuple[str, float], ...] = (
    ("/api/assets/monthly-overview", 1800.0),
    ("/api/analysis/performance-summary", 300.0),
    ("/api/analysis/return-attribution", 180.0),
    ("/api/analysis/lookthrough", 180.0),
    ("/api/portfolio/risk-and-config", 180.0),
    ("/api/portfolio/risk-disclosure", 180.0),
    ("/api/portfolio/config", 180.0),
    ("/api/accounts/overview", 120.0),
    ("/api/accounts/networth-and-debts", 120.0),
    ("/api/ledger/workbench", 120.0),
    ("/api/investment-transactions", 120.0),
    ("/api/prices/freshness", 60.0),
)

_MARKET_ONLY_WRITE_PREFIXES: tuple[str, ...] = (
    "/api/investment/page-projection/refresh",
    "/api/prices/refresh",
    "/api/fx-rates/refresh",
)

_MARKET_DEPENDENT_CACHE_PREFIXES: tuple[str, ...] = (
    "/api/analysis/",
    "/api/portfolio/",
    "/api/prices/freshness",
    "/api/accounts/networth-and-debts",
)


def ttl_for_path(path: str) -> float | None:
    for prefix, ttl in CACHEABLE_PREFIX_TTLS:
        if path == prefix or path.startswith(prefix + "/"):
            return ttl
    return None


def invalidation_scope_for_mutation(path: str, query_string: str = "") -> str:
    """Return 'none', 'market', or 'all' for a mutating request."""
    qs = (query_string or "").lower()
    if path == "/api/investment/page-projection/refresh":
        return "none"
    if any(path == p or path.startswith(p + "/") for p in _MARKET_ONLY_WRITE_PREFIXES):
        return "none" if "scheduled=true" in qs else "market"
    return "all"


def _is_market_dependent_key(key: str) -> bool:
    return any(key.startswith(p) for p in _MARKET_DEPENDENT_CACHE_PREFIXES)


class ReadResponseCache:
    """Bounded LRU + TTL cache storing raw response bytes and media type.

    When ``cache_dir`` is provided, entries and invalidation epochs are shared
    across multiple Uvicorn worker processes via atomic files on local tmpfs.
    """

    def __init__(self, max_entries: int = 64, *, cache_dir: str | Path | None = None) -> None:
        self._max_entries = max(8, int(max_entries))
        self._lock = Lock()
        self._store: OrderedDict[str, tuple[float, float, int, bytes, str]] = OrderedDict()
        self._cache_dir: Path | None = Path(cache_dir) if cache_dir else None
        if self._cache_dir is not None:
            try:
                self._cache_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                self._cache_dir = None

    def _now(self, now: float | None) -> float:
        if now is not None:
            return now
        return time.time() if self._cache_dir is not None else time.monotonic()

    def _entry_path(self, key: str) -> Path:
        assert self._cache_dir is not None
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self._cache_dir / f"entry_{digest}.bin"

    def _epoch_path(self, scope: str) -> Path:
        assert self._cache_dir is not None
        return self._cache_dir / f"_epoch_{scope}.txt"

    def _read_epoch(self, scope: str) -> float:
        if self._cache_dir is None:
            return 0.0
        try:
            raw = self._epoch_path(scope).read_text(encoding="utf-8").strip()
            return float(raw) if raw else 0.0
        except (OSError, ValueError):
            return 0.0

    def _bump_epoch(self, scope: str, ts: float) -> None:
        if self._cache_dir is None:
            return
        try:
            prev = self._read_epoch(scope)
            next_val = max(ts, prev + 1e-6)
            path = self._epoch_path(scope)
            tmp = path.with_suffix(f".{os.getpid()}.{time.time_ns()}.tmp")
            tmp.write_text(f"{next_val:.6f}", encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass

    def _min_valid_created_at(self, key: str) -> float:
        if self._cache_dir is None:
            return 0.0
        min_ts = self._read_epoch("all")
        if _is_market_dependent_key(key):
            min_ts = max(min_ts, self._read_epoch("market"))
        return min_ts

    def _read_disk(self, key: str) -> tuple[float, float, int, bytes, str] | None:
        if self._cache_dir is None:
            return None
        try:
            raw = self._entry_path(key).read_bytes()
            sep = raw.index(b"\n")
            meta = json.loads(raw[:sep].decode("utf-8"))
            body = raw[sep + 1 :]
            return (
                float(meta["expires_at"]),
                float(meta["created_at"]),
                int(meta["status_code"]),
                body,
                str(meta["media_type"]),
            )
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return None

    def _write_disk(
        self,
        key: str,
        *,
        expires_at: float,
        created_at: float,
        status_code: int,
        body: bytes,
        media_type: str,
    ) -> None:
        if self._cache_dir is None:
            return
        try:
            path = self._entry_path(key)
            tmp = path.with_suffix(f".{os.getpid()}.{time.time_ns()}.tmp")
            header = json.dumps(
                {
                    "expires_at": expires_at,
                    "created_at": created_at,
                    "status_code": status_code,
                    "media_type": media_type,
                },
                separators=(",", ":"),
            ).encode("utf-8")
            tmp.write_bytes(header + b"\n" + body)
            os.replace(tmp, path)
        except OSError:
            pass

    def get(self, key: str, *, now: float | None = None) -> tuple[int, bytes, str] | None:
        ts = self._now(now)
        min_created = self._min_valid_created_at(key)
        with self._lock:
            entry = self._store.get(key)
            if entry is not None:
                expires_at, created_at, status_code, body, media_type = entry
                if ts >= expires_at or created_at < min_created:
                    self._store.pop(key, None)
                else:
                    self._store.move_to_end(key)
                    return status_code, body, media_type

        disk_entry = self._read_disk(key)
        if disk_entry is None:
            return None
        expires_at, created_at, status_code, body, media_type = disk_entry
        if ts >= expires_at or created_at < min_created:
            return None
        with self._lock:
            self._store[key] = disk_entry
            self._store.move_to_end(key)
            while len(self._store) > self._max_entries:
                self._store.popitem(last=False)
        return status_code, body, media_type

    def set(
        self,
        key: str,
        *,
        status_code: int,
        body: bytes,
        media_type: str,
        ttl: float,
        now: float | None = None,
    ) -> None:
        if status_code != 200 or ttl <= 0:
            return
        ts = self._now(now)
        expires_at = ts + ttl
        with self._lock:
            self._store[key] = (expires_at, ts, status_code, body, media_type)
            self._store.move_to_end(key)
            while len(self._store) > self._max_entries:
                self._store.popitem(last=False)
        self._write_disk(
            key,
            expires_at=expires_at,
            created_at=ts,
            status_code=status_code,
            body=body,
            media_type=media_type,
        )

    def clear(self, *, prefixes: tuple[str, ...] | None = None, now: float | None = None) -> int:
        ts = self._now(now)
        with self._lock:
            if not prefixes:
                count = len(self._store)
                self._store.clear()
            else:
                keys = [k for k in self._store if any(k.startswith(p) for p in prefixes)]
                for k in keys:
                    self._store.pop(k, None)
                count = len(keys)
        if not prefixes:
            self._bump_epoch("all", ts)
        elif prefixes == _MARKET_DEPENDENT_CACHE_PREFIXES:
            self._bump_epoch("market", ts)
        else:
            self._bump_epoch("all", ts)
        return count

    def invalidate_for_mutation(self, path: str, query_string: str = "", *, now: float | None = None) -> int:
        scope = invalidation_scope_for_mutation(path, query_string)
        if scope == "none":
            return 0
        if scope == "market":
            return self.clear(prefixes=_MARKET_DEPENDENT_CACHE_PREFIXES, now=now)
        return self.clear(now=now)

    def size(self) -> int:
        with self._lock:
            return len(self._store)


def cache_key_for_request(path: str, query_string: str) -> str:
    return f"{path}?{query_string}" if query_string else path


def should_bypass_cache(query_string: str, cache_control: str | None) -> bool:
    qs = (query_string or "").lower()
    if "cache=false" in qs or "nocache=1" in qs or "fresh=1" in qs:
        return True
    cc = (cache_control or "").lower()
    return "no-cache" in cc or "no-store" in cc
