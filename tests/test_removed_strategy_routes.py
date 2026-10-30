from pathlib import Path
import sys


def test_removed_routes_and_retained_asset_routes():
    api_root = Path(__file__).resolve().parents[1] / "backend" / "api"
    sys.path.insert(0, str(api_root))
    try:
        import main
        paths = {route.path for route in main.app.routes}
        assert not any(p.startswith(("/api/strategy", "/api/backtest", "/api/live-signals", "/api/datahub")) for p in paths)
        assert "/policy/ips" not in paths
        assert "/watchlist" not in paths
        assert "/health" in paths
        assert "/api/accounts" in paths
    finally:
        sys.path.remove(str(api_root))
