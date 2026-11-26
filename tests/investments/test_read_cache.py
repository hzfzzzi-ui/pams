import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "api"))

from read_cache import ReadResponseCache, cache_key_for_request, should_bypass_cache, ttl_for_path


def test_read_cache_hit_expiry_and_clear():
    cache = ReadResponseCache(max_entries=8)
    key = cache_key_for_request("/api/assets/monthly-overview", "")
    assert cache.get(key, now=100.0) is None
    cache.set(key, status_code=200, body=b'{"ok":true}', media_type="application/json", ttl=60.0, now=100.0)
    assert cache.get(key, now=130.0) == (200, b'{"ok":true}', "application/json")
    assert cache.get(key, now=161.0) is None

    cache.set(key, status_code=200, body=b'{"ok":2}', media_type="application/json", ttl=60.0, now=200.0)
    assert cache.clear() == 1
    assert cache.get(key, now=201.0) is None


def test_read_cache_ttl_and_bypass_rules():
    assert ttl_for_path("/api/assets/monthly-overview") == 1800.0
    assert ttl_for_path("/api/analysis/lookthrough") == 180.0
    assert ttl_for_path("/api/health") is None
    assert should_bypass_cache("cache=false", None) is True
    assert should_bypass_cache("limit=100", "no-cache") is True
    assert should_bypass_cache("limit=100", None) is False


def test_scheduler_polling_does_not_evict_monthly_overview():
    cache = ReadResponseCache(max_entries=16)
    cache.set("/api/assets/monthly-overview", status_code=200, body=b"{}", media_type="application/json", ttl=1800.0, now=100.0)
    cache.set("/api/analysis/lookthrough?baseCurrency=CNY", status_code=200, body=b"{}", media_type="application/json", ttl=180.0, now=100.0)

    # Scheduler projection poll and scheduled price tick should not evict monthly-overview
    assert cache.invalidate_for_mutation("/api/investment/page-projection/refresh") == 0
    assert cache.invalidate_for_mutation("/api/prices/refresh/us-stock", "market=us&scheduled=true") == 0
    assert cache.get("/api/assets/monthly-overview", now=110.0) is not None

    # Manual price refresh clears market-dependent caches but keeps closed-month overview
    assert cache.invalidate_for_mutation("/api/prices/refresh/investments") == 1
    assert cache.get("/api/analysis/lookthrough?baseCurrency=CNY", now=110.0) is None
    assert cache.get("/api/assets/monthly-overview", now=110.0) is not None

    # User ledger/import mutation clears everything including monthly-overview
    assert cache.invalidate_for_mutation("/api/ledger/rows/123") == 1
    assert cache.get("/api/assets/monthly-overview", now=110.0) is None


def test_shared_disk_cache_across_workers_and_invalidation(tmp_path):
    worker_a = ReadResponseCache(max_entries=16, cache_dir=tmp_path)
    worker_b = ReadResponseCache(max_entries=16, cache_dir=tmp_path)

    worker_a.set(
        "/api/assets/monthly-overview",
        status_code=200,
        body=b'{"months":[1,2]}',
        media_type="application/json",
        ttl=1800.0,
        now=1000.0,
    )
    worker_a.set(
        "/api/analysis/lookthrough?baseCurrency=CNY",
        status_code=200,
        body=b'{"rows":[]}',
        media_type="application/json",
        ttl=180.0,
        now=1000.0,
    )

    # Worker B immediately hits both entries written by Worker A
    assert worker_b.get("/api/assets/monthly-overview", now=1010.0) == (
        200,
        b'{"months":[1,2]}',
        "application/json",
    )
    assert worker_b.get("/api/analysis/lookthrough?baseCurrency=CNY", now=1010.0) == (
        200,
        b'{"rows":[]}',
        "application/json",
    )

    # Market refresh on Worker A invalidates lookthrough on Worker B, keeps monthly-overview
    worker_a.invalidate_for_mutation("/api/prices/refresh/investments", now=1020.0)
    assert worker_b.get("/api/analysis/lookthrough?baseCurrency=CNY", now=1021.0) is None
    assert worker_b.get("/api/assets/monthly-overview", now=1021.0) is not None

    # User ledger write on Worker A invalidates monthly-overview on Worker B
    worker_a.invalidate_for_mutation("/api/ledger/rows/99", now=1030.0)
    assert worker_b.get("/api/assets/monthly-overview", now=1031.0) is None
