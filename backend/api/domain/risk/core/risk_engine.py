from __future__ import annotations

import math
from typing import Any

import pandas as pd

from domain.risk.core.factors import factor_exposure_rows, factors_for_position


LEVEL_ORDER = {"green": 0, "yellow": 1, "red": 2}


def ensure_positions(positions: pd.DataFrame, risk_limits: dict[str, Any] | None = None) -> pd.DataFrame:
    """Return a normalized positions frame with numeric CNY value columns."""
    risk_limits = risk_limits or {}
    data_cfg = risk_limits.get("data", {}) or {}
    cash_types = {str(x).lower() for x in data_cfg.get("cash_asset_types", ["cash"])}
    liability_types = {
        str(x).lower()
        for x in data_cfg.get("liability_asset_types", ["liability", "credit_card", "debt"])
    }

    df = positions.copy()
    for col, default in {
        "as_of": "",
        "source_type": "manual",
        "account_code": "unknown",
        "account_name": "",
        "owner_ratio": 1.0,
        "symbol": "",
        "asset_id": "",
        "asset_name": "",
        "asset_type": "manual_asset",
        "currency": risk_limits.get("base_currency", "CNY"),
        "quantity": 0.0,
        "price": 0.0,
        "market_value": 0.0,
        "cost": 0.0,
        "fx_rate_to_cny": 1.0,
    }.items():
        if col not in df.columns:
            df[col] = default

    for col in ["owner_ratio", "quantity", "price", "market_value", "cost", "fx_rate_to_cny"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    blank_asset_id = df["asset_id"].astype(str).str.strip() == ""
    df.loc[blank_asset_id, "asset_id"] = df.loc[blank_asset_id, "symbol"]
    blank_asset_id = df["asset_id"].astype(str).str.strip() == ""
    df.loc[blank_asset_id, "asset_id"] = df.loc[blank_asset_id, "asset_name"]

    if "market_value_cny" not in df.columns:
        df["market_value_cny"] = df["market_value"] * df["fx_rate_to_cny"] * df["owner_ratio"]
    else:
        df["market_value_cny"] = pd.to_numeric(df["market_value_cny"], errors="coerce").fillna(0.0)

    if "cost_cny" not in df.columns:
        df["cost_cny"] = df["cost"] * df["fx_rate_to_cny"] * df["owner_ratio"]
    else:
        df["cost_cny"] = pd.to_numeric(df["cost_cny"], errors="coerce").fillna(0.0)

    if "is_cash" not in df.columns:
        asset_type = df["asset_type"].astype(str).str.lower()
        asset_id = df["asset_id"].astype(str).str.upper()
        df["is_cash"] = asset_type.isin(cash_types) | asset_id.str.startswith("CASH")
    else:
        df["is_cash"] = df["is_cash"].astype(bool)

    if "is_liability" not in df.columns:
        asset_type = df["asset_type"].astype(str).str.lower()
        df["is_liability"] = asset_type.isin(liability_types)
    else:
        df["is_liability"] = df["is_liability"].astype(bool)

    return df


def total_nav(positions: pd.DataFrame, risk_limits: dict[str, Any] | None = None) -> float:
    """Calculate total net asset value in CNY after owner-ratio adjustment."""
    df = ensure_positions(positions, risk_limits)
    return float(df["market_value_cny"].sum())


def gross_exposure(positions: pd.DataFrame, risk_limits: dict[str, Any] | None = None, *, nav_override: float | None = None) -> dict[str, float | None]:
    """Calculate non-cash gross exposure and its ratio to total NAV."""
    df = ensure_positions(positions, risk_limits)
    nav = total_nav(df, risk_limits) if nav_override is None else nav_override
    risky = ~df["is_cash"] & ~df["is_liability"]
    gross_cny = float(df.loc[risky, "market_value_cny"].abs().sum())
    return {
        "gross_exposure_cny": gross_cny,
        "gross_exposure_ratio": gross_cny / nav if math.isfinite(nav) and nav > 0 else None,
    }


def negative_cash_ratio(positions: pd.DataFrame, risk_limits: dict[str, Any] | None = None, *, nav_override: float | None = None) -> dict[str, float | None]:
    """Calculate the negative cash balance ratio relative to total NAV."""
    df = ensure_positions(positions, risk_limits)
    nav = total_nav(df, risk_limits) if nav_override is None else nav_override
    neg_cash_cny = float(df.loc[df["is_cash"] & (df["market_value_cny"] < 0), "market_value_cny"].abs().sum())
    return {
        "negative_cash_cny": neg_cash_cny,
        "negative_cash_ratio": neg_cash_cny / nav if math.isfinite(nav) and nav > 0 else None,
    }


def account_weight(positions: pd.DataFrame, risk_limits: dict[str, Any] | None = None, *, nav_override: float | None = None) -> pd.DataFrame:
    """Calculate account-level NAV weights in CNY."""
    df = ensure_positions(positions, risk_limits)
    nav = total_nav(df, risk_limits) if nav_override is None else nav_override
    grouped = (
        df.groupby(["account_code", "account_name"], dropna=False)["market_value_cny"]
        .sum()
        .reset_index(name="account_value_cny")
    )
    grouped["account_weight"] = grouped["account_value_cny"] / nav if math.isfinite(nav) and nav > 0 else None
    return grouped.sort_values("account_value_cny", ascending=False, ignore_index=True)


def position_weight(positions: pd.DataFrame, risk_limits: dict[str, Any] | None = None, *, nav_override: float | None = None) -> pd.DataFrame:
    """Calculate single-asset weights in CNY, excluding cash-like assets."""
    df = ensure_positions(positions, risk_limits)
    nav = total_nav(df, risk_limits) if nav_override is None else nav_override
    non_cash = df.loc[~df["is_cash"] & ~df["is_liability"]].copy()
    if non_cash.empty:
        return pd.DataFrame(
            columns=["asset_id", "asset_name", "asset_type", "position_value_cny", "position_weight"]
        )

    grouped = (
        non_cash.groupby(["asset_id", "asset_name", "asset_type"], dropna=False)["market_value_cny"]
        .sum()
        .reset_index(name="position_value_cny")
    )
    grouped["position_weight"] = grouped["position_value_cny"].abs() / nav if math.isfinite(nav) and nav > 0 else None
    return grouped.sort_values("position_weight", ascending=False, ignore_index=True)


def factor_exposure(
    positions: pd.DataFrame,
    factor_map: dict[str, Any],
    risk_limits: dict[str, Any] | None = None,
    *, nav_override: float | None = None) -> pd.DataFrame:
    """Calculate aggregate factor exposure ratios in CNY."""
    df = ensure_positions(positions, risk_limits)
    nav = total_nav(df, risk_limits) if nav_override is None else nav_override
    rows = factor_exposure_rows(df.loc[~df["is_liability"]].copy(), factor_map)
    if not rows:
        return pd.DataFrame(columns=["factor", "exposure_cny", "exposure_ratio"])

    expanded = pd.DataFrame(rows)
    grouped = expanded.groupby("factor", dropna=False)["exposure_cny"].sum().reset_index()
    grouped["exposure_ratio"] = grouped["exposure_cny"] / nav if math.isfinite(nav) and nav > 0 else None
    grouped["abs_exposure_ratio"] = grouped["exposure_ratio"].abs() if math.isfinite(nav) and nav > 0 else None
    return grouped.sort_values("abs_exposure_ratio", ascending=False, ignore_index=True)


def stress_loss(
    positions: pd.DataFrame,
    factor_map: dict[str, Any],
    stress_tests: dict[str, Any],
    risk_limits: dict[str, Any] | None = None,
    *, nav_override: float | None = None) -> pd.DataFrame:
    """Estimate scenario losses from factor shocks; positive loss means NAV declines."""
    df = ensure_positions(positions, risk_limits)
    nav = total_nav(df, risk_limits) if nav_override is None else nav_override
    scenario_rows: list[dict[str, Any]] = []

    scenario_df = df.loc[~df["is_liability"]].copy()
    for scenario_id, scenario in (stress_tests or {}).items():
        shocks = scenario.get("shocks", {}) if isinstance(scenario, dict) else {}
        pnl_cny = 0.0
        for _, row in scenario_df.iterrows():
            value_cny = float(row.get("market_value_cny", 0) or 0)
            factor_weights = factors_for_position(row, factor_map)
            shock = sum(float(shocks.get(factor, 0.0) or 0.0) * weight for factor, weight in factor_weights.items())
            pnl_cny += value_cny * shock
        loss_cny = max(0.0, -pnl_cny)
        scenario_rows.append(
            {
                "scenario": scenario_id,
                "label": scenario.get("label", scenario_id) if isinstance(scenario, dict) else scenario_id,
                "loss_cny": loss_cny,
                "loss_ratio": loss_cny / nav if math.isfinite(nav) and nav > 0 else None,
                "pnl_cny": pnl_cny,
            }
        )

    return pd.DataFrame(scenario_rows).sort_values("loss_ratio", ascending=False, ignore_index=True)


def _threshold_level(value: float, threshold: dict[str, Any] | None) -> str:
    threshold = threshold or {}
    red = float(threshold.get("red", float("inf")))
    yellow = float(threshold.get("yellow", float("inf")))
    if value >= red:
        return "red"
    if value >= yellow:
        return "yellow"
    return "green"


def _alert(rule: str, level: str, metric: float | None, threshold: float | None, message: str) -> dict[str, Any]:
    return {
        "rule": rule,
        "level": level,
        "metric": metric,
        "threshold": threshold,
        "message": message,
    }


def risk_alerts(
    positions: pd.DataFrame,
    risk_limits: dict[str, Any],
    factor_map: dict[str, Any],
    *, nav_override: float | None = None) -> list[dict[str, Any]]:
    """Generate red/yellow/green alerts from configured portfolio risk limits."""
    df = ensure_positions(positions, risk_limits)
    limits = risk_limits.get("limits", {}) or {}
    alerts: list[dict[str, Any]] = []

    nav = total_nav(df, risk_limits) if nav_override is None else nav_override
    if not math.isfinite(nav) or nav <= 0:
        return [_alert("total_nav", "red", nav if math.isfinite(nav) else None, 0,
                       "Total NAV is non-positive or unavailable; risk ratios cannot be calculated")]
    alerts.append(
        _alert(
            "total_nav",
            "green" if nav > 0 else "red",
            nav,
            0,
            f"Total NAV is {nav:,.2f} CNY" if nav > 0 else "Total NAV is non-positive",
        )
    )

    gross = gross_exposure(df, risk_limits, nav_override=nav)
    gross_ratio = gross["gross_exposure_ratio"]
    assert gross_ratio is not None  # positive finite NAV was checked above
    gross_limit = limits.get("gross_exposure", {})
    level = _threshold_level(gross_ratio, gross_limit)
    alerts.append(
        _alert(
            "gross_exposure",
            level,
            gross_ratio,
            gross_limit.get(level) if level != "green" else gross_limit.get("yellow"),
            f"Gross exposure is {gross_ratio:.2%}",
        )
    )

    neg_cash = negative_cash_ratio(df, risk_limits, nav_override=nav)
    neg_cash_ratio = neg_cash["negative_cash_ratio"]
    assert neg_cash_ratio is not None  # positive finite NAV was checked above
    neg_cash_limit = limits.get("negative_cash_ratio", {})
    level = _threshold_level(neg_cash_ratio, neg_cash_limit)
    alerts.append(
        _alert(
            "negative_cash_ratio",
            level,
            neg_cash_ratio,
            neg_cash_limit.get(level) if level != "green" else neg_cash_limit.get("yellow"),
            f"Negative cash ratio is {neg_cash_ratio:.2%}",
        )
    )

    account_limits = limits.get("account_weight", {}) or {}
    default_account_limit = account_limits.get("default", {})
    account_overrides = account_limits.get("by_account", {}) or {}
    account_rows = account_weight(df, risk_limits, nav_override=nav)
    emitted_account = False
    for _, row in account_rows.iterrows():
        account_code = str(row["account_code"])
        threshold = account_overrides.get(account_code, default_account_limit)
        level = _threshold_level(float(row["account_weight"]), threshold)
        if level != "green":
            emitted_account = True
            alerts.append(
                _alert(
                    f"account_weight:{account_code}",
                    level,
                    float(row["account_weight"]),
                    threshold.get(level),
                    f"Account {account_code} weight is {float(row['account_weight']):.2%}",
                )
            )
    if not emitted_account:
        alerts.append(_alert("account_weight", "green", 0.0, default_account_limit.get("yellow"), "Account weights are within limits"))

    position_limits = limits.get("position_weight", {}) or {}
    default_position_limit = position_limits.get("default", {})
    type_overrides = position_limits.get("by_asset_type", {}) or {}
    position_rows = position_weight(df, risk_limits, nav_override=nav)
    emitted_position = False
    for _, row in position_rows.iterrows():
        asset_type = str(row["asset_type"]).lower()
        threshold = type_overrides.get(asset_type, default_position_limit)
        level = _threshold_level(float(row["position_weight"]), threshold)
        if level != "green":
            emitted_position = True
            alerts.append(
                _alert(
                    f"position_weight:{row['asset_id']}",
                    level,
                    float(row["position_weight"]),
                    threshold.get(level),
                    f"Position {row['asset_id']} weight is {float(row['position_weight']):.2%}",
                )
            )
    if not emitted_position:
        alerts.append(_alert("position_weight", "green", 0.0, default_position_limit.get("yellow"), "Single-asset weights are within limits"))

    factor_limits = limits.get("factor_exposure", {}) or {}
    factor_rows = factor_exposure(df, factor_map, risk_limits, nav_override=nav)
    emitted_factor = False
    for _, row in factor_rows.iterrows():
        factor = str(row["factor"])
        threshold = factor_limits.get(factor)
        if not threshold:
            continue
        metric = abs(float(row["exposure_ratio"]))
        level = _threshold_level(metric, threshold)
        if level != "green":
            emitted_factor = True
            alerts.append(
                _alert(
                    f"factor_exposure:{factor}",
                    level,
                    metric,
                    threshold.get(level),
                    f"Factor {factor} exposure is {metric:.2%}",
                )
            )
    if not emitted_factor:
        alerts.append(_alert("factor_exposure", "green", 0.0, None, "Factor exposures are within limits"))

    stress_rows = stress_loss(df, factor_map, risk_limits.get("stress_tests", {}), risk_limits, nav_override=nav)
    stress_limit = limits.get("stress_loss", {})
    emitted_stress = False
    for _, row in stress_rows.iterrows():
        level = _threshold_level(float(row["loss_ratio"]), stress_limit)
        if level != "green":
            emitted_stress = True
            alerts.append(
                _alert(
                    f"stress_loss:{row['scenario']}",
                    level,
                    float(row["loss_ratio"]),
                    stress_limit.get(level),
                    f"Stress scenario {row['label']} loss is {float(row['loss_ratio']):.2%}",
                )
            )
    if not emitted_stress:
        alerts.append(_alert("stress_loss", "green", 0.0, stress_limit.get("yellow"), "Stress losses are within limits"))

    return sorted(alerts, key=lambda x: LEVEL_ORDER.get(x["level"], 0), reverse=True)


def rebalance_suggestions(
    positions: pd.DataFrame,
    risk_limits: dict[str, Any],
    factor_map: dict[str, Any],
    *, nav_override: float | None = None) -> list[dict[str, Any]]:
    """Create simple V0 rebalance suggestions from non-green alerts."""
    suggestions: list[dict[str, Any]] = []
    for alert in risk_alerts(positions, risk_limits, factor_map, nav_override=nav_override):
        if alert["level"] == "green":
            continue
        threshold = alert.get("threshold")
        metric = float(alert.get("metric") or 0.0)
        excess = metric - float(threshold or 0.0) if threshold is not None else None
        action = "reduce" if alert["rule"].startswith(("account_weight", "position_weight", "factor_exposure", "gross_exposure")) else "review"
        suggestions.append(
            {
                "level": alert["level"],
                "rule": alert["rule"],
                "action": action,
                "message": alert["message"],
                "excess_ratio": excess,
            }
        )
    if not suggestions:
        suggestions.append(
            {
                "level": "green",
                "rule": "rebalance",
                "action": "hold",
                "message": "No forced rebalance is suggested by V0 limits.",
                "excess_ratio": 0.0,
            }
        )
    return suggestions
