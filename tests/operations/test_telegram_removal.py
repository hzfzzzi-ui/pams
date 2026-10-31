from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_telegram_services_and_sources_are_removed():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    assert "pams-telegram-bot" not in compose["services"]
    assert "pams-assistant-bot" not in compose["services"]
    assert not any((ROOT / "telegram-bot").rglob("*.py"))
    assert not any((ROOT / "telegram-bot-assistant").rglob("*.py"))


def test_shared_transaction_and_ledger_api_sources_remain():
    assert not (ROOT / "backend/api/endpoints/transactions.py").exists()
    assert (ROOT / "backend/api/endpoints/holdings_trade.py").is_file()
    assert (ROOT / "backend/api/endpoints/ledger.py").is_file()


def test_telegram_queue_routes_are_removed_but_asset_routes_remain():
    import sys

    api_root = str(ROOT / "backend/api")
    sys.path.insert(0, api_root)
    try:
        import main

        paths = {route.path for route in main.app.routes}
        assert not any(path.startswith(("/api/tasks", "/api/notifications")) for path in paths)
        assert "/api/transactions" not in paths
        assert "/api/holdings/trade" in paths
        assert "/api/accounts" in paths
    finally:
        sys.path.remove(api_root)


def test_empty_database_manifest_excludes_telegram_tables():
    manifest = (ROOT / "database/postgres/bootstrap/manifest.txt").read_text(encoding="utf-8")
    assert "008_task_queue.sql" not in manifest
    assert "009_notifications.sql" not in manifest
