from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from domain.configuration import resolve_base_currency


REPO_ROOT = Path(__file__).resolve().parents[5]
DEFAULT_RISK_LIMITS_PATH = REPO_ROOT / "config" / "risk_limits.yaml"
DEFAULT_FACTOR_MAP_PATH = REPO_ROOT / "config" / "factor_map.yaml"


def load_yaml(path: str | Path) -> dict[str, Any]:
    """Load a YAML file and return an empty mapping when the file is empty."""
    with Path(path).open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data or {}


def load_risk_limits(path: str | Path = DEFAULT_RISK_LIMITS_PATH) -> dict[str, Any]:
    """Load portfolio risk limits from YAML."""
    limits = load_yaml(path)
    if isinstance(limits, dict):
        limits["base_currency"] = resolve_base_currency(Path(path).parent, limits)
    return limits


def load_factor_map(path: str | Path = DEFAULT_FACTOR_MAP_PATH) -> dict[str, Any]:
    """Load asset-to-risk-factor mappings from YAML."""
    return load_yaml(path)
