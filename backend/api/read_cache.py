"""Thread-safe short-TTL response cache for heavy read-only GET endpoints.

Scoped invalidation ensures user writes (imports, ledger edits, transfers,
reviews, trades) clear all relevant caches immediately, while high-frequency
scheduler polling (page-projection/refresh and scheduled price ticks) does not
repeatedly evict closed-month historical reports like /api/assets/monthly-overview.
"""
from __future__ import annotations

from collections import OrderedDict
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

    def clear(self, *, prefixes: tuple[str, ...] | None = None) -> int:
        with self._lock:
            if not prefixes:
                count = len(self._store)
                self._store.clear()
                return count
            keys = [k for k in self._store if any(k.startswith(p) for p in prefixes)]
            for k in keys:
                self._store.pop(k, None)
            return len(keys)

    def invalidate_for_mutation(self, path: str, query_string: str = "") -> int:
        scope = invalidation_scope_for_mutation(path, query_string)
        if scope == "none":
            return 0
        if scope == "market":
            return self.clear(prefixes=_MARKET_DEPENDENT_CACHE_PREFIXES)
        return self.clear()

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
