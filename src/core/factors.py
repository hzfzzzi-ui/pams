from __future__ import annotations

from typing import Any

import pandas as pd


def normalize_factor_weights(weights: dict[str, Any] | None) -> dict[str, float]:
    """Convert a YAML factor weight mapping to numeric weights."""
    if not weights:
        return {}
    normalized: dict[str, float] = {}
    for factor, weight in weights.items():
        try:
            value = float(weight)
        except (TypeError, ValueError):
            continue
        if value != 0:
            normalized[str(factor)] = value
    return normalized


def factors_for_position(row: pd.Series | dict[str, Any], factor_map: dict[str, Any]) -> dict[str, float]:
    """Resolve factor weights for one position using symbol, asset type, then account defaults."""
    getter = row.get if isinstance(row, dict) else row.get
    symbol = str(getter("symbol", "") or getter("asset_id", "") or "").strip().upper()
    asset_id = str(getter("asset_id", "") or "").strip().upper()
    asset_type = str(getter("asset_type", "") or "").strip().lower()
    account_code = str(getter("account_code", "") or "").strip().lower()

    symbols = factor_map.get("symbols", {}) or {}
    asset_types = factor_map.get("asset_types", {}) or {}
    accounts = factor_map.get("accounts", {}) or {}

    weights: dict[str, float] = {}
    for key in (symbol, asset_id):
        if key and key in symbols:
            weights.update(normalize_factor_weights(symbols.get(key)))
            break

    if not weights and asset_type in asset_types:
        weights.update(normalize_factor_weights(asset_types.get(asset_type)))

    account_weights = normalize_factor_weights(accounts.get(account_code))
    for factor, weight in account_weights.items():
        weights.setdefault(factor, weight)

    if not weights:
        weights.update(normalize_factor_weights(factor_map.get("default")))

    return weights


def factor_exposure_rows(positions: pd.DataFrame, factor_map: dict[str, Any]) -> list[dict[str, Any]]:
    """Expand normalized positions into per-position risk factor exposure rows."""
    rows: list[dict[str, Any]] = []
    if positions.empty:
        return rows

    for _, row in positions.iterrows():
        value_cny = float(row.get("market_value_cny", 0) or 0)
        for factor, weight in factors_for_position(row, factor_map).items():
            rows.append(
                {
                    "asset_id": row.get("asset_id"),
                    "asset_name": row.get("asset_name"),
                    "account_code": row.get("account_code"),
                    "factor": factor,
                    "weight": weight,
                    "exposure_cny": value_cny * weight,
                }
            )
    return rows
