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
    assert ttl_for_path("/api/assets/monthly-overview") == 300.0
    assert ttl_for_path("/api/analysis/lookthrough") == 180.0
    assert ttl_for_path("/api/health") is None
    assert should_bypass_cache("cache=false", None) is True
    assert should_bypass_cache("limit=100", "no-cache") is True
    assert should_bypass_cache("limit=100", None) is False
