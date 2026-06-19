from __future__ import annotations

from typing import Any

import pandas as pd

from src.core.factors import factor_exposure_rows, factors_for_position


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


def gross_exposure(positions: pd.DataFrame, risk_limits: dict[str, Any] | None = None) -> dict[str, float]:
    """Calculate non-cash gross exposure and its ratio to total NAV."""
    df = ensure_positions(positions, risk_limits)
    nav = total_nav(df, risk_limits)
    risky = ~df["is_cash"] & ~df["is_liability"]
    gross_cny = float(df.loc[risky, "market_value_cny"].abs().sum())
    return {
        "gross_exposure_cny": gross_cny,
        "gross_exposure_ratio": gross_cny / nav if nav > 0 else 0.0,
    }


def negative_cash_ratio(positions: pd.DataFrame, risk_limits: dict[str, Any] | None = None) -> dict[str, float]:
    """Calculate the negative cash balance ratio relative to total NAV."""
    df = ensure_positions(positions, risk_limits)
    nav = total_nav(df, risk_limits)
    neg_cash_cny = float(df.loc[df["is_cash"] & (df["market_value_cny"] < 0), "market_value_cny"].abs().sum())
    return {
        "negative_cash_cny": neg_cash_cny,
        "negative_cash_ratio": neg_cash_cny / nav if nav > 0 else 0.0,
    }


def account_weight(positions: pd.DataFrame, risk_limits: dict[str, Any] | None = None) -> pd.DataFrame:
    """Calculate account-level NAV weights in CNY."""
    df = ensure_positions(positions, risk_limits)
    nav = total_nav(df, risk_limits)
    grouped = (
        df.groupby(["account_code", "account_name"], dropna=False)["market_value_cny"]
        .sum()
        .reset_index(name="account_value_cny")
    )
    grouped["account_weight"] = grouped["account_value_cny"] / nav if nav > 0 else 0.0
    return grouped.sort_values("account_value_cny", ascending=False, ignore_index=True)


def position_weight(positions: pd.DataFrame, risk_limits: dict[str, Any] | None = None) -> pd.DataFrame:
    """Calculate single-asset weights in CNY, excluding cash-like assets."""
    df = ensure_positions(positions, risk_limits)
    nav = total_nav(df, risk_limits)
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
    grouped["position_weight"] = grouped["position_value_cny"].abs() / nav if nav > 0 else 0.0
    return grouped.sort_values("position_weight", ascending=False, ignore_index=True)


def factor_exposure(
    positions: pd.DataFrame,
    factor_map: dict[str, Any],
    risk_limits: dict[str, Any] | None = None,
) -> pd.DataFrame:
    """Calculate aggregate factor exposure ratios in CNY."""
    df = ensure_positions(positions, risk_limits)
    nav = total_nav(df, risk_limits)
    rows = factor_exposure_rows(df.loc[~df["is_liability"]].copy(), factor_map)
    if not rows:
        return pd.DataFrame(columns=["factor", "exposure_cny", "exposure_ratio"])

    expanded = pd.DataFrame(rows)
    grouped = expanded.groupby("factor", dropna=False)["exposure_cny"].sum().reset_index()
    grouped["exposure_ratio"] = grouped["exposure_cny"] / nav if nav > 0 else 0.0
    grouped["abs_exposure_ratio"] = grouped["exposure_ratio"].abs()
    return grouped.sort_values("abs_exposure_ratio", ascending=False, ignore_index=True)


def stress_loss(
    positions: pd.DataFrame,
    factor_map: dict[str, Any],
    stress_tests: dict[str, Any],
    risk_limits: dict[str, Any] | None = None,
) -> pd.DataFrame:
    """Estimate scenario losses from factor shocks; positive loss means NAV declines."""
    df = ensure_positions(positions, risk_limits)
    nav = total_nav(df, risk_limits)
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
                "loss_ratio": loss_cny / nav if nav > 0 else 0.0,
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


def _alert(rule: str, level: str, metric: float, threshold: float | None, message: str) -> dict[str, Any]:
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
) -> list[dict[str, Any]]:
    """Generate red/yellow/green alerts from configured portfolio risk limits."""
    df = ensure_positions(positions, risk_limits)
    limits = risk_limits.get("limits", {}) or {}
    alerts: list[dict[str, Any]] = []

    nav = total_nav(df, risk_limits)
    alerts.append(
        _alert(
            "total_nav",
            "green" if nav > 0 else "red",
            nav,
            0,
            f"Total NAV is {nav:,.2f} CNY" if nav > 0 else "Total NAV is non-positive",
        )
    )

    gross = gross_exposure(df, risk_limits)
    gross_limit = limits.get("gross_exposure", {})
    level = _threshold_level(gross["gross_exposure_ratio"], gross_limit)
    alerts.append(
        _alert(
            "gross_exposure",
            level,
            gross["gross_exposure_ratio"],
            gross_limit.get(level) if level != "green" else gross_limit.get("yellow"),
            f"Gross exposure is {gross['gross_exposure_ratio']:.2%}",
        )
    )

    neg_cash = negative_cash_ratio(df, risk_limits)
    neg_cash_limit = limits.get("negative_cash_ratio", {})
    level = _threshold_level(neg_cash["negative_cash_ratio"], neg_cash_limit)
    alerts.append(
        _alert(
            "negative_cash_ratio",
            level,
            neg_cash["negative_cash_ratio"],
            neg_cash_limit.get(level) if level != "green" else neg_cash_limit.get("yellow"),
            f"Negative cash ratio is {neg_cash['negative_cash_ratio']:.2%}",
        )
    )

    account_limits = limits.get("account_weight", {}) or {}
    default_account_limit = account_limits.get("default", {})
    account_overrides = account_limits.get("by_account", {}) or {}
    account_rows = account_weight(df, risk_limits)
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
    position_rows = position_weight(df, risk_limits)
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
    factor_rows = factor_exposure(df, factor_map, risk_limits)
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

    stress_rows = stress_loss(df, factor_map, risk_limits.get("stress_tests", {}), risk_limits)
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


def _subtract_cash_for_buy(df: pd.DataFrame, account_code: str, amount_cny: float) -> pd.DataFrame:
    out = df.copy()
    remaining = amount_cny
    cash_idx = out.index[out["is_cash"] & (out["market_value_cny"] > 0) & (out["account_code"].astype(str) == account_code)].tolist()
    cash_idx += out.index[out["is_cash"] & (out["market_value_cny"] > 0) & ~out.index.isin(cash_idx)].tolist()

    for idx in cash_idx:
        available = float(out.at[idx, "market_value_cny"])
        use = min(available, remaining)
        out.at[idx, "market_value_cny"] = available - use
        remaining -= use
        if remaining <= 1e-9:
            break

    if remaining > 1e-9:
        out = pd.concat(
            [
                out,
                pd.DataFrame(
                    [
                        {
                            "as_of": "",
                            "source_type": "pretrade",
                            "account_code": account_code,
                            "account_name": account_code,
                            "owner_ratio": 1.0,
                            "symbol": "CASH_CNY",
                            "asset_id": "CASH_CNY",
                            "asset_name": "Pre-trade negative cash",
                            "asset_type": "cash",
                            "currency": "CNY",
                            "quantity": 1.0,
                            "price": -remaining,
                            "market_value": -remaining,
                            "cost": -remaining,
                            "fx_rate_to_cny": 1.0,
                            "market_value_cny": -remaining,
                            "cost_cny": -remaining,
                            "is_cash": True,
                        }
                    ]
                ),
            ],
            ignore_index=True,
        )
    return out


def simulate_buy(
    positions: pd.DataFrame,
    buy: dict[str, Any],
    risk_limits: dict[str, Any],
) -> pd.DataFrame:
    """Simulate a cash-funded buy using CNY risk amount, preserving total NAV."""
    df = ensure_positions(positions, risk_limits)
    amount_cny = max(0.0, float(buy.get("amount_cny", 0.0) or 0.0))
    account_code = str(buy.get("account_code") or "manual")
    asset_id = str(buy.get("asset_id") or buy.get("symbol") or "NEW_ASSET")
    asset_type = str(buy.get("asset_type") or "manual_asset")
    asset_name = str(buy.get("asset_name") or asset_id)

    funded = _subtract_cash_for_buy(df, account_code, amount_cny)
    new_position = pd.DataFrame(
        [
            {
                "as_of": "",
                "source_type": "pretrade",
                "account_code": account_code,
                "account_name": account_code,
                "owner_ratio": 1.0,
                "symbol": asset_id,
                "asset_id": asset_id,
                "asset_name": asset_name,
                "asset_type": asset_type,
                "currency": "CNY",
                "quantity": 1.0,
                "price": amount_cny,
                "market_value": amount_cny,
                "cost": amount_cny,
                "fx_rate_to_cny": 1.0,
                "market_value_cny": amount_cny,
                "cost_cny": amount_cny,
                "is_cash": False,
            }
        ]
    )
    return pd.concat([funded, new_position], ignore_index=True)


def max_buy_amount(
    positions: pd.DataFrame,
    buy: dict[str, Any],
    risk_limits: dict[str, Any],
    factor_map: dict[str, Any],
    iterations: int = 32,
) -> dict[str, Any]:
    """Check a proposed buy and estimate the largest amount allowed before red limits."""
    blocking_level = str((risk_limits.get("limits", {}) or {}).get("blocking_level", "red"))
    blocking_rank = LEVEL_ORDER.get(blocking_level, 2)

    proposed_amount = max(0.0, float(buy.get("amount_cny", 0.0) or 0.0))

    def blocking_alerts(amount: float) -> list[dict[str, Any]]:
        simulated = simulate_buy(positions, {**buy, "amount_cny": amount}, risk_limits)
        return [
            alert
            for alert in risk_alerts(simulated, risk_limits, factor_map)
            if LEVEL_ORDER.get(alert["level"], 0) >= blocking_rank
        ]

    proposed_alerts = blocking_alerts(proposed_amount)
    if blocking_alerts(0.0):
        max_allowed = 0.0
    else:
        nav = total_nav(positions, risk_limits)
        hi = max(proposed_amount, nav * 2, 1.0)
        lo = 0.0
        for _ in range(iterations):
            mid = (lo + hi) / 2
            if blocking_alerts(mid):
                hi = mid
            else:
                lo = mid
        max_allowed = lo

    return {
        "proposed_amount_cny": proposed_amount,
        "allowed": not proposed_alerts,
        "max_buy_amount_cny": round(max_allowed, 2),
        "blocking_alerts": proposed_alerts,
        "post_buy_alerts": risk_alerts(simulate_buy(positions, buy, risk_limits), risk_limits, factor_map),
    }


def rebalance_suggestions(
    positions: pd.DataFrame,
    risk_limits: dict[str, Any],
    factor_map: dict[str, Any],
) -> list[dict[str, Any]]:
    """Create simple V0 rebalance suggestions from non-green alerts."""
    suggestions: list[dict[str, Any]] = []
    for alert in risk_alerts(positions, risk_limits, factor_map):
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
