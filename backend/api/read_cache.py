"""Thread-safe short-TTL response cache for heavy read-only GET endpoints.

Any mutating HTTP request (POST/PUT/PATCH/DELETE) clears the cache immediately
so reads never observe stale data after user edits, imports, or price refreshes.
"""
from __future__ import annotations

from collections import OrderedDict
from threading import Lock
import time
from typing import Any


CACHEABLE_PREFIX_TTLS: tuple[tuple[str, float], ...] = (
    ("/api/assets/monthly-overview", 300.0),
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


def ttl_for_path(path: str) -> float | None:
    for prefix, ttl in CACHEABLE_PREFIX_TTLS:
        if path == prefix or path.startswith(prefix + "/"):
            return ttl
    return None


class ReadResponseCache:
    """Bounded LRU + TTL cache storing raw response bytes and media type."""

    def __init__(self, max_entries: int = 64) -> None:
        self._max_entries = max(8, int(max_entries))
        self._lock = Lock()
        self._store: OrderedDict[str, tuple[float, int, bytes, str]] = OrderedDict()

    def get(self, key: str, *, now: float | None = None) -> tuple[int, bytes, str] | None:
        ts = time.monotonic() if now is None else now
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            expires_at, status_code, body, media_type = entry
            if ts >= expires_at:
                self._store.pop(key, None)
                return None
            self._store.move_to_end(key)
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
        ts = time.monotonic() if now is None else now
        with self._lock:
            self._store[key] = (ts + ttl, status_code, body, media_type)
            self._store.move_to_end(key)
            while len(self._store) > self._max_entries:
                self._store.popitem(last=False)

    def clear(self) -> int:
        with self._lock:
            count = len(self._store)
            self._store.clear()
            return count

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
