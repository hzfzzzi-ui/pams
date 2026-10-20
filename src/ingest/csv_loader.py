from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd


CANONICAL_COLUMNS = [
    "as_of",
    "source_type",
    "account_code",
    "account_name",
    "owner_ratio",
    "symbol",
    "asset_id",
    "asset_name",
    "asset_type",
    "currency",
    "quantity",
    "price",
    "market_value",
    "cost",
    "fx_rate_to_cny",
    "market_value_cny",
    "cost_cny",
    "is_cash",
]


def _default_owner_ratio(account_code: str, risk_limits: dict[str, Any]) -> float:
    data_cfg = risk_limits.get("data", {}) or {}
    account_ratios = data_cfg.get("account_owner_ratios", {}) or {}
    return float(account_ratios.get(account_code, data_cfg.get("default_owner_ratio", 1.0)))


def _default_fx(currency: str, risk_limits: dict[str, Any]) -> float:
    data_cfg = risk_limits.get("data", {}) or {}
    fx_rates = data_cfg.get("fx_rates_to_cny", {}) or {}
    return float(fx_rates.get(str(currency).upper(), 1.0))


def _is_cash_row(row: pd.Series, risk_limits: dict[str, Any]) -> bool:
    data_cfg = risk_limits.get("data", {}) or {}
    cash_types = {str(x).lower() for x in data_cfg.get("cash_asset_types", ["cash"])}
    asset_type = str(row.get("asset_type", "") or "").strip().lower()
    asset_id = str(row.get("asset_id", "") or row.get("symbol", "") or "").strip().upper()
    return asset_type in cash_types or asset_id.startswith("CASH")


def normalize_positions_frame(
    frame: pd.DataFrame,
    source_type: str,
    risk_limits: dict[str, Any],
) -> pd.DataFrame:
    """Normalize a CSV-like positions frame to the V0 canonical schema."""
    df = frame.copy()
    df.columns = [str(c).strip() for c in df.columns]

    defaults: dict[str, Any] = {
        "as_of": "",
        "source_type": source_type,
        "account_code": source_type,
        "account_name": source_type,
        "owner_ratio": None,
        "symbol": "",
        "asset_id": "",
        "asset_name": "",
        "asset_type": "manual_asset",
        "currency": risk_limits.get("base_currency", "CNY"),
        "quantity": 0.0,
        "price": 0.0,
        "market_value": None,
        "cost": None,
        "fx_rate_to_cny": None,
    }
    for col, default in defaults.items():
        if col not in df.columns:
            df[col] = default

    for col in ["account_code", "account_name", "symbol", "asset_id", "asset_name", "asset_type", "currency"]:
        df[col] = df[col].fillna("").astype(str).str.strip()

    blank_asset_id = df["asset_id"] == ""
    df.loc[blank_asset_id, "asset_id"] = df.loc[blank_asset_id, "symbol"]
    blank_asset_id = df["asset_id"] == ""
    df.loc[blank_asset_id, "asset_id"] = df.loc[blank_asset_id, "asset_name"]
    blank_asset_name = df["asset_name"] == ""
    df.loc[blank_asset_name, "asset_name"] = df.loc[blank_asset_name, "asset_id"]

    for col in ["quantity", "price", "market_value", "cost", "owner_ratio", "fx_rate_to_cny"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    missing_owner = df["owner_ratio"].isna()
    if missing_owner.any():
        df.loc[missing_owner, "owner_ratio"] = df.loc[missing_owner, "account_code"].map(
            lambda code: _default_owner_ratio(str(code), risk_limits)
        )

    missing_fx = df["fx_rate_to_cny"].isna()
    if missing_fx.any():
        df.loc[missing_fx, "fx_rate_to_cny"] = df.loc[missing_fx, "currency"].map(
            lambda currency: _default_fx(str(currency), risk_limits)
        )

    missing_mv = df["market_value"].isna()
    if missing_mv.any():
        df.loc[missing_mv, "market_value"] = df.loc[missing_mv, "quantity"].fillna(0) * df.loc[missing_mv, "price"].fillna(0)
    df["market_value"] = df["market_value"].fillna(0.0)

    missing_cost = df["cost"].isna()
    if missing_cost.any():
        df.loc[missing_cost, "cost"] = df.loc[missing_cost, "market_value"]
    df["cost"] = df["cost"].fillna(0.0)

    df["market_value_cny"] = df["market_value"] * df["fx_rate_to_cny"] * df["owner_ratio"]
    df["cost_cny"] = df["cost"] * df["fx_rate_to_cny"] * df["owner_ratio"]
    df["source_type"] = source_type
    df["is_cash"] = df.apply(lambda row: _is_cash_row(row, risk_limits), axis=1)

    for col in CANONICAL_COLUMNS:
        if col not in df.columns:
            df[col] = None

    return df[CANONICAL_COLUMNS]


def read_positions_csv(
    path: str | Path,
    source_type: str,
    risk_limits: dict[str, Any],
) -> pd.DataFrame:
    """Read a UTF-8/UTF-8-SIG CSV file and normalize positions."""
    frame = pd.read_csv(path, encoding="utf-8-sig")
    return normalize_positions_frame(frame, source_type, risk_limits)

