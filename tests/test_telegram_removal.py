from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_telegram_services_and_sources_are_removed():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    assert "pams-telegram-bot" not in compose["services"]
    assert "pams-assistant-bot" not in compose["services"]
    assert not any((ROOT / "telegram-bot").rglob("*.py"))
    assert not any((ROOT / "telegram-bot-assistant").rglob("*.py"))


def test_shared_transaction_and_ledger_api_sources_remain():
    assert (ROOT / "backend/api/endpoints/transactions.py").is_file()
    assert (ROOT / "backend/api/endpoints/holdings_trade.py").is_file()
    assert (ROOT / "backend/api/endpoints/ledger.py").is_file()
