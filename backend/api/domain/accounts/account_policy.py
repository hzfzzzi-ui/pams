"""Shared ownership and coverage rules for account reads."""

from decimal import Decimal
from functools import partial


def excluded_accounts(config):
    defaults = config.get("defaults") or {}
    return {str(code).strip() for code in
            [*(config.get("excluded_accounts") or ()), *(defaults.get("excluded_accounts") or ())]
            if str(code).strip()}


def default_owner_ratio(config):
    value = (config.get("defaults") or {}).get("owner_ratio")
    return Decimal(1) if value is None else Decimal(str(value))


def owner_ratios(config):
    history = (config.get("defaults") or {}).get("historical_owner_ratios") or {}
    return {**{str(code): Decimal(str(value)) for code, value in history.items()},
            **{str(code): Decimal(str(cfg["owner_ratio"])) for code, cfg in
            (config.get("accounts") or {}).items()
            if cfg and cfg.get("owner_ratio") is not None}}


def owner_ratio(config, code):
    cfg = (config.get("accounts") or {}).get(str(code)) or {}
    value = cfg.get("owner_ratio")
    if value is None:
        value = ((config.get("defaults") or {}).get("historical_owner_ratios") or {}).get(str(code))
    return default_owner_ratio(config) if value is None else Decimal(str(value))


def owner_ratio_lookup(config):
    return partial(owner_ratio, config)


def ratio_case_sql(alias, ratios, default):
    clauses = [f"WHEN {alias}.account_code = %s THEN %s" for _ in ratios]
    if not clauses:
        return "%s", [float(default)]
    params = [value for code, ratio in ratios.items() for value in (code, float(ratio))]
    return f"(CASE {' '.join(clauses)} ELSE %s END)", [*params, float(default)]


def owner_ratio_case_sql(config):
    def build(alias="h"):
        return ratio_case_sql(alias, owner_ratios(config), default_owner_ratio(config))
    return build


NON_INVESTMENT_ACCOUNT_TYPES = {
    "benefit",
    "bridge",
    "credit_card",
    "funding",
    "liability",
    "payment",
    "receivable",
    "wallet",
}


def is_investment_account(account):
    if account.get("include_in_investments") is not None:
        return bool(account["include_in_investments"])
    return str(account.get("account_type") or "").lower() not in NON_INVESTMENT_ACCOUNT_TYPES
