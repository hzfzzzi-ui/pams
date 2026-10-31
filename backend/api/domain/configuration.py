"""Application configuration shared by asset and risk services."""
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"


def resolve_base_currency(config_dir: Path, legacy: Any = None) -> str:
    """Prefer application config; retain the old risk setting when absent."""
    path = config_dir / "application.yaml"
    if path.is_file():
        with path.open("r", encoding="utf-8") as file:
            config = yaml.safe_load(file) or {}
        if isinstance(config, dict) and config.get("base_currency"):
            return str(config["base_currency"])
    if isinstance(legacy, dict) and legacy.get("base_currency"):
        return str(legacy["base_currency"])
    return "CNY"


def load_base_currency(config_dir: Path = CONFIG_DIR) -> str:
    """Read shared currency configuration with a legacy risk-file fallback."""
    app_path = config_dir / "application.yaml"
    if app_path.is_file():
        with app_path.open("r", encoding="utf-8") as file:
            config = yaml.safe_load(file) or {}
        if isinstance(config, dict) and config.get("base_currency"):
            return str(config["base_currency"])
    legacy_path = config_dir / "risk_limits.yaml"
    if legacy_path.is_file():
        with legacy_path.open("r", encoding="utf-8") as file:
            legacy = yaml.safe_load(file) or {}
        return resolve_base_currency(config_dir, legacy)
    return "CNY"
