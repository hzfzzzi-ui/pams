"""Portfolio return attribution built on the unified exposure model.

This module keeps return facts separate from classification rules.  The
exposure engine resolves asset type, industry, theme, market, region,
currency, and lookthrough holdings; this module only allocates PnL across
those already-normalized dimensions.
"""

from __future__ import annotations

import os
from collections import defaultdict
from datetime import date as Date
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import yaml

from services.exposure_engine import (
    UNMAPPED_INDUSTRY,
    _identity_keys,
    _is_theme_eligible,
    _num,
    buildLookthroughHoldings,
    normalizeHoldings,
)
from services.holdings_view import (
    build_holdings_by_category_payload,
    build_holdings_latest_payload,
    latest_complete_snapshot_date,
)
from services.investment_view_date import daily_pnl_window_date
from services.investment_returns import build_section_return_series_payload
from services.lookthrough_state import (
    _find_lookthrough,
    _find_mapping,
    _load_sw_l1_lookup,
    derive_lookthrough_payload,
    load_lookthrough_config,
)
from services.portfolio_valuation_state import apply_holdings_category_pnl_to_section_return_series_payload
from services.portfolio_state import (
    _asset_type,
    _excluded_accounts,
    _load_accounts_config,
    _owner_ratio,
    _table_exists,
    load_account_balances,
    load_investment_positions,
)


AttributionUnit = dict[str, Any]


REPORTED_RETURNS_CONFIG_PATH = Path(os.environ.get("PAMS_INVESTMENT_RETURNS_CONFIG", "/app/config/investment_returns.yaml"))
LOCAL_REPORTED_RETURNS_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "investment_returns.yaml"
RETURN_ATTRIBUTION_START_DATE = Date(2026, 1, 1)
SECTION_ACCOUNT_MAP = {
    "a_stock": {"tonghuashun"},
    "a_fund": {"ccb_yhs300_fund", "tencent_licaitong"},
    "us_stock": {"ibkr"},
    "precious_metal": {"ccb_gold_accumulation"},
}
SECTION_CORRECTION_LABELS = {
    "a_stock": "A\u80a1\u8d26\u6237\u53e3\u5f84\u6821\u51c6",
    "a_fund": "\u57fa\u91d1\u6536\u76ca\u53e3\u5f84\u6821\u51c6",
    "us_stock": "\u7f8e\u80a1/\u6d77\u5916\u6536\u76ca\u53e3\u5f84\u6821\u51c6",
    "precious_metal": "\u8d35\u91d1\u5c5e\u6536\u76ca\u53e3\u5f84\u6821\u51c6",
}
CANONICAL_RECONCILIATION_SECTIONS = {"a_stock"}
RETURN_ATTRIBUTION_MAJOR_CATEGORIES = {"A股", "美股", "ESOP", "贵金属", "加密"}
RETURN_ATTRIBUTION_ASSET_TYPES = {
    "a_share_stock",
    "a_share_fund",
    "fund",
    "us_stock",
    "precious_metal",
    "esop",
    "crypto",
}


A_SHARE_NAME_FALLBACK = {
    "002185": "华天科技",
    "002281": "光迅科技",
    "002300": "太阳电缆",
    "002432": "九安医疗",
    "002594": "比亚迪",
    "003035": "南网能源",
    "600396": "华电辽能",
    "600522": "中天科技",
    "600584": "长电科技",
    "600759": "ST洲际",
    "601016": "节能风电",
    "601138": "工业富联",
    "603508": "思维列控",
    "603538": "美诺华",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _repair_mojibake_text(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    try:
        repaired = text.encode("gbk").decode("utf-8")
    except UnicodeError:
        return text
    return repaired or text


def _metric_value(row: dict[str, Any], *keys: str) -> float:
    for key in keys:
        if row.get(key) not in (None, ""):
            return _num(row.get(key))
    return 0.0


def _position_metrics(position: dict[str, Any]) -> dict[str, float]:
    daily_price = _metric_value(position, "daily_price_pnl_cny", "daily_pnl_cny", "daily_pnl")
    daily_fx = _metric_value(position, "daily_fx_pnl_cny", "fx_daily_pnl_cny", "daily_fx_pnl", "fx_pnl_cny")
    explicit_daily_total = _metric_value(position, "daily_total_pnl_cny", "total_daily_pnl_cny")
    daily_total = explicit_daily_total if explicit_daily_total else daily_price + daily_fx
    return {
        "market_value_cny": _metric_value(position, "market_value_cny", "marketValue", "market_value"),
        "cost_cny": _metric_value(position, "cost_cny", "cost"),
        "pnl_cny": _metric_value(position, "holding_pnl_cny", "total_pnl_cny", "pnl_cny", "holding_pnl"),
        "daily_pnl_cny": daily_total,
        "daily_price_pnl_cny": daily_price,
        "daily_fx_pnl_cny": daily_fx,
    }


def _merge_metrics(target: dict[str, float], metrics: dict[str, float], scale: float = 1.0) -> None:
    for key, value in metrics.items():
        target[key] = target.get(key, 0.0) + value * scale


def _account_of(row: dict[str, Any]) -> str:
    return _text(row.get("account") or row.get("account_name") or row.get("account_code"))


def _keys_for_position(position: dict[str, Any]) -> set[str]:
    return _identity_keys(
        position.get("id"),
        position.get("asset_id"),
        position.get("symbol"),
        position.get("asset_name"),
        position.get("name"),
    )


def _daily_row_to_position(row: dict[str, Any]) -> dict[str, Any]:
    daily_price = _num(row.get("daily_pnl_cny"))
    daily_fx = _num(row.get("daily_fx_pnl_cny"))
    daily_total = daily_price + daily_fx
    major = _repair_mojibake_text(row.get("major_category"))
    minor = _repair_mojibake_text(row.get("minor_category"))
    symbol = row.get("symbol")
    name = row.get("name") or symbol
    asset_row = {**row, "major_category": major, "minor_category": minor, "asset_liability": "资产"}
    return {
        "account_code": row.get("account") or row.get("account_code"),
        "asset_id": symbol or name,
        "symbol": symbol,
        "asset_name": name,
        "asset_type": _asset_type(asset_row),
        "major_category": major,
        "minor_category": minor,
        "quantity": row.get("quantity"),
        "market_value": row.get("market_value_native", row.get("market_value")),
        "currency": row.get("currency") or "CNY",
        "market_value_cny": row.get("market_value_cny"),
        "cost_cny": row.get("cost_cny"),
        "holding_pnl_cny": row.get("holding_pnl_cny"),
        "daily_pnl_cny": daily_price,
        "daily_price_pnl_cny": daily_price,
        "daily_fx_pnl_cny": daily_fx,
        "daily_total_pnl_cny": daily_total,
        "realized_trade_pnl_cny": row.get("realized_trade_pnl_cny"),
        "daily_realized_trade_pnl_cny": row.get("daily_realized_trade_pnl_cny"),
        "daily_includes_realized_trade_pnl": row.get("daily_includes_realized_trade_pnl"),
        "as_of": row.get("as_of") or row.get("snapshot_as_of"),
        "source_type": "holdings_view",
    }


def _is_return_attribution_row(row: dict[str, Any]) -> bool:
    """Keep return attribution on the same investment universe as lookthrough."""
    asset_liability = _repair_mojibake_text(row.get("asset_liability") or "资产")
    if asset_liability in {"负债", "liability", "debt"}:
        return False
    major = _repair_mojibake_text(row.get("major_category"))
    if major in RETURN_ATTRIBUTION_MAJOR_CATEGORIES or "基金" in major:
        return True
    asset_row = {
        **row,
        "major_category": major,
        "minor_category": _repair_mojibake_text(row.get("minor_category")),
        "asset_liability": asset_liability,
    }
    return _asset_type(asset_row) in RETURN_ATTRIBUTION_ASSET_TYPES


def _symbol_digits(value: Any) -> str:
    return "".join(ch for ch in _text(value) if ch.isdigit())


def _name_needs_enrichment(symbol: Any, name: Any) -> bool:
    text = _text(name)
    if not text:
        return True
    symbol_text = _text(symbol)
    digits = _symbol_digits(symbol)
    return text.casefold() == symbol_text.casefold() or bool(digits and text == digits)


def _symbol_name_keys(symbol: Any) -> set[str]:
    raw = _text(symbol)
    if not raw:
        return set()
    keys = {raw, raw.upper(), raw.lower()}
    digits = _symbol_digits(raw)
    if len(digits) == 6:
        exchange = "SH" if digits.startswith("6") else "SZ"
        keys.update(
            {
                digits,
                f"{digits}.{exchange}",
                f"{digits}.{exchange.lower()}",
                f"{exchange.lower()}{digits}",
                f"{exchange}{digits}",
                f"SSE:{digits}" if exchange == "SH" else f"SZSE:{digits}",
            }
        )
    return {key.casefold() for key in keys if key}


def _put_asset_name(lookup: dict[str, str], symbol: Any, name: Any) -> None:
    label = _text(name)
    if not label or _name_needs_enrichment(symbol, label):
        return
    for key in _symbol_name_keys(symbol):
        lookup.setdefault(key, label)


def _asset_name_lookup(cur, symbols: set[str]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    query_symbols = sorted(set().union(*(_symbol_name_keys(symbol) for symbol in symbols)))
    digits = sorted({_symbol_digits(symbol) for symbol in symbols if len(_symbol_digits(symbol)) == 6})
    if not query_symbols and not digits:
        return lookup

    if _table_exists(cur, "pams_holdings_snapshot"):
        cur.execute(
            """
            SELECT symbol, name
            FROM pams_holdings_snapshot
            WHERE COALESCE(name, '') <> ''
              AND (
                LOWER(symbol) = ANY(%s::text[])
                OR regexp_replace(COALESCE(symbol, ''), '[^0-9]', '', 'g') = ANY(%s::text[])
              )
            ORDER BY as_of DESC
            """,
            (query_symbols, digits),
        )
        for row in cur.fetchall():
            _put_asset_name(lookup, row.get("symbol"), row.get("name"))

    if _table_exists(cur, "pit_security_master"):
        cur.execute(
            """
            SELECT symbol, ts_code, name
            FROM pit_security_master
            WHERE COALESCE(name, '') <> ''
              AND (
                LOWER(symbol) = ANY(%s::text[])
                OR LOWER(ts_code) = ANY(%s::text[])
                OR regexp_replace(COALESCE(symbol, ''), '[^0-9]', '', 'g') = ANY(%s::text[])
                OR regexp_replace(COALESCE(ts_code, ''), '[^0-9]', '', 'g') = ANY(%s::text[])
              )
            """,
            (query_symbols, query_symbols, digits, digits),
        )
        for row in cur.fetchall():
            _put_asset_name(lookup, row.get("symbol"), row.get("name"))
            _put_asset_name(lookup, row.get("ts_code"), row.get("name"))

    if _table_exists(cur, "pams_investment_transactions"):
        cur.execute(
            """
            SELECT symbol, name
            FROM pams_investment_transactions
            WHERE COALESCE(name, '') <> ''
              AND (
                LOWER(symbol) = ANY(%s::text[])
                OR regexp_replace(COALESCE(symbol, ''), '[^0-9]', '', 'g') = ANY(%s::text[])
              )
            ORDER BY trade_date DESC, id DESC
            """,
            (query_symbols, digits),
        )
        for row in cur.fetchall():
            _put_asset_name(lookup, row.get("symbol"), row.get("name"))

    for symbol, name in A_SHARE_NAME_FALLBACK.items():
        if not digits or symbol in digits:
            _put_asset_name(lookup, symbol, name)
    return lookup


def _enrich_daily_position_names(cur, rows: list[dict[str, Any]]) -> None:
    missing_symbols = {
        _text(row.get("symbol"))
        for row in rows
        if _name_needs_enrichment(row.get("symbol"), row.get("name"))
    }
    if not missing_symbols:
        return
    lookup = _asset_name_lookup(cur, missing_symbols)
    for row in rows:
        if not _name_needs_enrichment(row.get("symbol"), row.get("name")):
            continue
        for key in _symbol_name_keys(row.get("symbol")):
            name = lookup.get(key)
            if name:
                row["name"] = name
                break


def _position_key(position: dict[str, Any]) -> tuple[str, str]:
    identity = _text(position.get("symbol") or position.get("asset_id") or position.get("asset_name") or position.get("name")).casefold()
    return (_account_of(position), identity)


def _latest_snapshot_date(cur) -> Date:
    value = latest_complete_snapshot_date(cur, view_as_of=Date.today())
    if hasattr(value, "date"):
        return value.date()
    if hasattr(value, "isoformat"):
        return value
    try:
        return Date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return Date.today()


def _owner_ratios(account_cfg: dict[str, Any]) -> tuple[dict[str, Decimal], Decimal]:
    accounts = account_cfg.get("accounts") or {}
    defaults = account_cfg.get("defaults") or {}
    default_owner_ratio = Decimal(str(defaults.get("owner_ratio", 1)))
    return ({code: _owner_ratio(account_cfg, code) for code in accounts}, default_owner_ratio)


def _load_reported_returns_config() -> dict[str, Any]:
    path = REPORTED_RETURNS_CONFIG_PATH if REPORTED_RETURNS_CONFIG_PATH.exists() else LOCAL_REPORTED_RETURNS_CONFIG_PATH
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _account_owner_ratio_lookup(account_cfg: dict[str, Any]) -> Callable[[str], Decimal]:
    def lookup(account_code: str) -> Decimal:
        return _owner_ratio(account_cfg, str(account_code or ""))

    return lookup


def _owner_ratio_case_sql(account_cfg: dict[str, Any]) -> Callable[[str], tuple[str, list[Any]]]:
    accounts = account_cfg.get("accounts") or {}
    defaults = account_cfg.get("defaults") or {}
    default_owner_ratio = Decimal(str(defaults.get("owner_ratio", 1)))

    def build(alias: str = "h") -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for account_code, cfg in accounts.items():
            ratio = Decimal(str((cfg or {}).get("owner_ratio", default_owner_ratio)))
            clauses.append(f"WHEN {alias}.account_code = %s THEN %s")
            params.extend([str(account_code), float(ratio)])
        if not clauses:
            return "%s", [float(default_owner_ratio)]
        return f"(CASE {' '.join(clauses)} ELSE %s END)", [*params, float(default_owner_ratio)]

    return build


def _account_initial_capital_cny(account_cfg: dict[str, Any]) -> Callable[[str], Decimal | None]:
    accounts = account_cfg.get("accounts") or {}

    def lookup(account_code: str) -> Decimal | None:
        cfg = accounts.get(str(account_code or "")) or {}
        if cfg.get("capital_base") not in (None, ""):
            return Decimal(str(cfg["capital_base"]))
        if cfg.get("personal_capital") not in (None, ""):
            ratio = _owner_ratio(account_cfg, str(account_code or ""))
            if ratio:
                return Decimal(str(cfg["personal_capital"])) / ratio
            return Decimal(str(cfg["personal_capital"]))
        return None

    return lookup


def _overview_payload_from_account_balances(cur, account_cfg: dict[str, Any], base_currency: str) -> dict[str, Any]:
    balances = load_account_balances(cur, base_currency, account_cfg)
    accounts = []
    for row in balances:
        accounts.append(
            {
                "account_code": row.get("account_code"),
                "include_in_nav": row.get("include_in_nav"),
                "primary_balance": {
                    "amount": row.get("amount_cny"),
                    "currency": base_currency,
                    "as_of": row.get("as_of"),
                    "source": row.get("truth_source"),
                },
            }
        )
    return {"accounts": accounts}


def _unit_section(unit: AttributionUnit) -> str | None:
    account = _text(unit.get("account"))
    for section, accounts in SECTION_ACCOUNT_MAP.items():
        if account in accounts:
            return section
    return None


def _section_target_payloads(
    cur,
    *,
    account_cfg: dict[str, Any],
    base_currency: str,
    view_as_of: Date,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    reported_returns_cfg = _load_reported_returns_config()
    overview_payload = _overview_payload_from_account_balances(cur, account_cfg, base_currency)
    owner_ratios, default_owner_ratio = _owner_ratios(account_cfg)
    excluded_accounts = _excluded_accounts(account_cfg)
    targets: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    for section in SECTION_ACCOUNT_MAP:
        savepoint = f"return_attr_section_{section}"
        cur.execute(f"SAVEPOINT {savepoint}")
        try:
            payload = build_section_return_series_payload(
                cur,
                section=section,
                start_date=RETURN_ATTRIBUTION_START_DATE,
                interval="daily",
                base_currency=base_currency,
                view_as_of=view_as_of,
                reported_returns_cfg=reported_returns_cfg,
                overview_payload=overview_payload,
                owner_ratio_case_sql=_owner_ratio_case_sql(account_cfg),
                account_owner_ratio=_account_owner_ratio_lookup(account_cfg),
                excluded_holding_accounts=excluded_accounts,
                today_label_date=view_as_of,
            )
            if section == "a_fund":
                holdings_payload = build_holdings_by_category_payload(
                    cur,
                    category="\u0041\u80a1",
                    limit=5000,
                    minor_category="\u0041\u80a1\u57fa\u91d1",
                    include_closed=True,
                    view_as_of=view_as_of,
                    excluded_accounts=excluded_accounts,
                    owner_ratios=owner_ratios,
                    default_owner_ratio=default_owner_ratio,
                    daily_pnl_start_as_of=daily_pnl_window_date(),
                )
                payload = apply_holdings_category_pnl_to_section_return_series_payload(
                    payload,
                    holdings_payload,
                    source="holdings.by_category.a_fund",
                )
        except Exception as exc:  # pragma: no cover - defensive live-data guard
            cur.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            cur.execute(f"RELEASE SAVEPOINT {savepoint}")
            warnings.append(f"{section}: {exc}")
            continue
        cur.execute(f"RELEASE SAVEPOINT {savepoint}")
        rows = payload.get("rows") or []
        latest = rows[-1] if rows else {}
        today = payload.get("today_window") or {}
        targets[section] = {
            "market_value_cny": _num(latest.get("market_value")),
            "pnl_cny": _num((payload.get("summary") or {}).get("total_return")),
            "daily_pnl_cny": _num(today.get("total_return")),
        }
    return targets, warnings


def _return_reconciliation_units(
    instrument_units: list[AttributionUnit],
    targets: dict[str, dict[str, Any]],
) -> tuple[list[AttributionUnit], dict[str, Any]]:
    actual: dict[str, dict[str, float]] = {
        section: {
            "market_value_cny": 0.0,
            "pnl_cny": 0.0,
            "daily_pnl_cny": 0.0,
            "daily_price_pnl_cny": 0.0,
            "daily_fx_pnl_cny": 0.0,
        }
        for section in SECTION_ACCOUNT_MAP
    }
    for unit in instrument_units:
        section = _unit_section(unit)
        if not section:
            continue
        actual[section]["market_value_cny"] += _num(unit.get("market_value_cny"))
        actual[section]["pnl_cny"] += _num(unit.get("pnl_cny"))
        actual[section]["daily_pnl_cny"] += _num(unit.get("daily_pnl_cny"))
        actual[section]["daily_price_pnl_cny"] += _num(unit.get("daily_price_pnl_cny"))
        actual[section]["daily_fx_pnl_cny"] += _num(unit.get("daily_fx_pnl_cny"))

    units: list[AttributionUnit] = []
    details: list[dict[str, Any]] = []
    for section, target in targets.items():
        base = actual.get(section) or {}
        market_delta = _num(target.get("market_value_cny")) - _num(base.get("market_value_cny"))
        pnl_delta = _num(target.get("pnl_cny")) - _num(base.get("pnl_cny"))
        daily_delta = _num(target.get("daily_pnl_cny")) - _num(base.get("daily_pnl_cny"))
        if max(abs(market_delta), abs(pnl_delta), abs(daily_delta)) <= 1e-6:
            continue
        label = SECTION_CORRECTION_LABELS.get(section, section)
        account = sorted(SECTION_ACCOUNT_MAP.get(section) or [""])[0]
        unit = {
            "name": label,
            "symbol": f"{section}:RETURN_RECONCILIATION",
            "account": account,
            "asset_type": "\u5176\u4ed6\u8d44\u4ea7",
            "industry_bucket": UNMAPPED_INDUSTRY,
            "theme_exposures": [],
            "market": "\u5176\u4ed6",
            "region": "\u5176\u4ed6",
            "currency": "CNY",
            "source": "return_reconciliation",
            "source_label": "\u6536\u76ca\u53e3\u5f84\u6821\u51c6",
            "source_asset_name": label,
            "source_symbol": f"{section}:RETURN_RECONCILIATION",
            "underlying_name": "",
            "underlying_symbol": "",
            "lookthrough_type": "RECONCILIATION",
            "market_value_cny": market_delta,
            "cost_cny": 0.0,
            "pnl_cny": pnl_delta,
            "daily_pnl_cny": daily_delta,
            "daily_price_pnl_cny": daily_delta,
            "daily_fx_pnl_cny": 0.0,
        }
        units.append(unit)
        details.append(
            {
                "section": section,
                "name": label,
                "actual_market_value_cny": base.get("market_value_cny", 0.0),
                "target_market_value_cny": target.get("market_value_cny", 0.0),
                "market_value_delta_cny": market_delta,
                "actual_pnl_cny": base.get("pnl_cny", 0.0),
                "target_pnl_cny": target.get("pnl_cny", 0.0),
                "pnl_delta_cny": pnl_delta,
                "actual_daily_pnl_cny": base.get("daily_pnl_cny", 0.0),
                "target_daily_pnl_cny": target.get("daily_pnl_cny", 0.0),
                "daily_pnl_delta_cny": daily_delta,
            }
        )
    return units, {"sections": details}


def _load_daily_attribution_positions(cur, account_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    owner_ratios, default_owner_ratio = _owner_ratios(account_cfg)
    view_as_of = _latest_snapshot_date(cur)
    specs = [
        ("A股", "A股", True),
        ("A股", "A股基金", False),
        ("美股", None, True),
        ("贵金属", None, False),
        ("ESOP", None, False),
        ("其他", None, False),
    ]
    rows: list[dict[str, Any]] = []
    for category, minor_category, include_closed in specs:
        payload = build_holdings_by_category_payload(
            cur,
            category=category,
            limit=5000,
            minor_category=minor_category,
            include_closed=include_closed,
            view_as_of=view_as_of,
            excluded_accounts=_excluded_accounts(account_cfg),
            owner_ratios=owner_ratios,
            default_owner_ratio=default_owner_ratio,
        )
        rows.extend(payload.get("rows") or [])
    _enrich_daily_position_names(cur, rows)
    return [_daily_row_to_position(row) for row in rows]


def _merge_daily_positions(base_positions: list[dict[str, Any]], daily_positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    daily_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for position in daily_positions:
        key = _position_key(position)
        if key[1]:
            daily_by_key[key] = position

    used: set[tuple[str, str]] = set()
    merged: list[dict[str, Any]] = []
    for position in base_positions:
        key = _position_key(position)
        daily = daily_by_key.get(key)
        if daily:
            used.add(key)
            item = dict(position)
            for field in (
                "daily_pnl_cny",
                "daily_price_pnl_cny",
                "daily_fx_pnl_cny",
                "daily_total_pnl_cny",
                "realized_trade_pnl_cny",
                "daily_realized_trade_pnl_cny",
            ):
                if daily.get(field) not in (None, ""):
                    item[field] = daily[field]
            if daily.get("daily_includes_realized_trade_pnl"):
                item["daily_includes_realized_trade_pnl"] = True
            merged.append(item)
        else:
            merged.append(position)

    for key, position in daily_by_key.items():
        if key in used:
            continue
        # Unmatched positive-value rows are usually duplicate current holdings
        # whose display name/account key differs between loaders.  Only append
        # zero-value closed rows so realized PnL is not dropped.
        if abs(_num(position.get("market_value_cny"))) <= 1e-9 and (
            abs(_num(position.get("holding_pnl_cny"))) > 1e-9
            or abs(_num(position.get("daily_total_pnl_cny"))) > 1e-9
        ):
            merged.append(position)
    return merged


def _load_realized_trade_fact_positions(
    cur,
    account_cfg: dict[str, Any],
    view_as_of: Date,
) -> list[dict[str, Any]]:
    if not _table_exists(cur, "pams_investment_trade_fact"):
        return []

    owner_ratios, default_owner_ratio = _owner_ratios(account_cfg)
    excluded = _excluded_accounts(account_cfg)
    cur.execute(
        """
        SELECT
            account_code,
            symbol,
            MAX(name) AS name,
            MAX(market) AS market,
            MAX(currency) AS currency,
            MAX(trade_date) AS as_of,
            SUM(COALESCE(realized_pnl_cny, realized_pnl, 0)) AS realized_pnl_cny,
            SUM(
                CASE
                    WHEN trade_date = %s::date THEN COALESCE(realized_pnl_cny, realized_pnl, 0)
                    ELSE 0
                END
            ) AS daily_realized_pnl_cny
        FROM pams_investment_trade_fact
        WHERE source_table = 'pams_investment_transactions'
          AND realized_pnl IS NOT NULL
          AND trade_date <= %s::date
          AND COALESCE(LOWER(review_status), 'auto') NOT IN ('cancelled', 'canceled', 'void')
        GROUP BY account_code, symbol
        HAVING ABS(SUM(COALESCE(realized_pnl_cny, realized_pnl, 0))) > 0.000001
        """,
        (view_as_of, view_as_of),
    )
    positions: list[dict[str, Any]] = []
    for row in cur.fetchall():
        account = _text(row.get("account_code"))
        if account in excluded:
            continue
        ratio = _owner_ratio(account_cfg, account) if account else default_owner_ratio
        pnl = _num(row.get("realized_pnl_cny")) * float(ratio)
        daily = _num(row.get("daily_realized_pnl_cny")) * float(ratio)
        market = row.get("market") or "A股"
        positions.append(
            {
                "account_code": account,
                "asset_id": row.get("symbol") or row.get("name"),
                "symbol": row.get("symbol"),
                "asset_name": row.get("name") or row.get("symbol"),
                "major_category": market,
                "minor_category": market,
                "asset_type": "stock_cn" if market == "A股" else None,
                "quantity": 0,
                "market_value": 0,
                "market_value_cny": 0,
                "cost_cny": 0,
                "holding_pnl_cny": pnl,
                "daily_pnl_cny": daily,
                "daily_price_pnl_cny": daily,
                "daily_fx_pnl_cny": 0,
                "daily_total_pnl_cny": daily,
                "realized_pnl_cny": pnl,
                "currency": row.get("currency") or "CNY",
                "as_of": row.get("as_of"),
                "is_closed": True,
                "source_type": "trade_fact_realized",
            }
        )
    return positions


def _merge_realized_trade_positions(
    base_positions: list[dict[str, Any]],
    realized_positions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not realized_positions:
        return base_positions

    by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for position in realized_positions:
        by_key[_position_key(position)].append(position)

    used: set[int] = set()
    merged: list[dict[str, Any]] = []
    for position in base_positions:
        item = dict(position)
        key = _position_key(item)
        matches = by_key.get(key) or []
        daily_already_has_realized = bool(item.get("daily_includes_realized_trade_pnl")) or (
            abs(_metric_value(item, "daily_realized_trade_pnl_cny")) > 1e-9
        )
        for realized in matches:
            used.add(id(realized))
            for field in (
                "holding_pnl_cny",
                "daily_pnl_cny",
                "daily_price_pnl_cny",
                "daily_total_pnl_cny",
                "realized_pnl_cny",
            ):
                if daily_already_has_realized and field in (
                    "daily_pnl_cny",
                    "daily_price_pnl_cny",
                    "daily_total_pnl_cny",
                ):
                    continue
                item[field] = _metric_value(item, field) + _metric_value(realized, field)
            item["source_type"] = "holdings_view+trade_fact_realized"
        merged.append(item)

    for position in realized_positions:
        if id(position) not in used:
            merged.append(position)
    return merged


def _build_position_metric_maps(positions: list[dict[str, Any]]) -> tuple[dict[tuple[str, str], dict[str, float]], dict[str, dict[str, float]]]:
    by_pair: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    by_key: dict[str, dict[str, float]] = defaultdict(dict)
    for position in positions:
        metrics = _position_metrics(position)
        account = _account_of(position)
        for key in _keys_for_position(position):
            _merge_metrics(by_pair[(key, account)], metrics)
            _merge_metrics(by_key[key], metrics)
    return dict(by_pair), dict(by_key)


def _position_metric_candidates(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "keys": _keys_for_position(position),
            "account": _account_of(position),
            "metrics": _position_metrics(position),
            "used": False,
        }
        for position in positions
    ]


def _candidate_score(candidate: dict[str, Any], account: str, fallback_market_value: float) -> float:
    metrics = candidate.get("metrics") or {}
    score = abs(_num(metrics.get("market_value_cny")) - fallback_market_value)
    if account and candidate.get("account") and candidate.get("account") != account:
        score += max(abs(fallback_market_value), abs(_num(metrics.get("market_value_cny"))), 1.0) * 0.01
    return score


def _find_position_metrics(
    candidates: list[dict[str, Any]],
    *,
    keys: set[str],
    account: str,
    fallback_market_value: float,
    consume: bool,
) -> dict[str, float] | None:
    matches = [
        candidate
        for candidate in candidates
        if (not consume or not candidate.get("used"))
        and keys
        and keys.intersection(candidate.get("keys") or set())
    ]
    if not matches:
        return None
    chosen = min(matches, key=lambda candidate: _candidate_score(candidate, account, fallback_market_value))
    if consume:
        chosen["used"] = True
    return dict(chosen.get("metrics") or {})


def _metrics_for_identity(
    *,
    keys: set[str],
    account: str,
    by_pair: dict[tuple[str, str], dict[str, float]],
    by_key: dict[str, dict[str, float]],
    fallback_market_value: float,
) -> dict[str, float]:
    out: dict[str, float] = {}
    for key in keys:
        found = by_pair.get((key, account))
        if found:
            _merge_metrics(out, found)
            break
    if not out:
        for key in keys:
            found = by_key.get(key)
            if found:
                _merge_metrics(out, found)
                break
    if not out:
        out = {
            "market_value_cny": fallback_market_value,
            "cost_cny": 0.0,
            "pnl_cny": 0.0,
            "daily_pnl_cny": 0.0,
            "daily_price_pnl_cny": 0.0,
            "daily_fx_pnl_cny": 0.0,
        }
    elif not out.get("market_value_cny"):
        out["market_value_cny"] = fallback_market_value
    return out


def _attach_sw_l1(positions: list[dict[str, Any]], industry_l1_by_symbol: dict[str, str] | None) -> list[dict[str, Any]]:
    if not industry_l1_by_symbol:
        return [dict(p) for p in positions]
    out: list[dict[str, Any]] = []
    for position in positions:
        item = dict(position)
        symbol = _text(item.get("symbol") or item.get("asset_id"))
        keys = {symbol, symbol.upper(), symbol.lower()}
        digits = "".join(ch for ch in symbol if ch.isdigit())
        if len(digits) == 6:
            keys.update({digits, f"{digits}.SH", f"{digits}.SZ", f"sh{digits}", f"sz{digits}"})
        if not item.get("sw_l1"):
            for key in keys:
                found = industry_l1_by_symbol.get(key) or industry_l1_by_symbol.get(key.lower())
                if found:
                    item["sw_l1"] = found
                    break
        out.append(item)
    return out


def _holding_unit(holding: dict[str, Any], metrics: dict[str, float]) -> AttributionUnit:
    return {
        "name": holding["name"],
        "symbol": holding.get("symbol"),
        "account": holding.get("account") or "",
        "asset_type": holding.get("assetType"),
        "industry_bucket": holding.get("industryBucket") or UNMAPPED_INDUSTRY,
        "theme_exposures": holding.get("themeExposures") or [],
        "market": holding.get("market") or "其他",
        "region": holding.get("region") or "其他",
        "currency": holding.get("currency") or "其他",
        "source": "direct",
        "source_label": "直接持仓",
        "source_asset_name": holding["name"],
        "source_symbol": holding.get("symbol"),
        "underlying_name": "",
        "underlying_symbol": "",
        "lookthrough_type": "DIRECT",
        **metrics,
    }


def _underlying_unit(underlying: dict[str, Any], parent_metrics: dict[str, float]) -> AttributionUnit:
    weight = max(_num(underlying.get("underlyingWeightInFund")), 0.0)
    metrics: dict[str, float] = {}
    _merge_metrics(metrics, parent_metrics, weight)
    metrics["market_value_cny"] = _num(underlying.get("effectiveMarketValue"))
    return {
        "name": underlying.get("underlyingName") or "未穿透残差",
        "symbol": underlying.get("underlyingSymbol"),
        "account": underlying.get("account") or "",
        "asset_type": underlying.get("assetType"),
        "industry_bucket": underlying.get("industryBucket") or UNMAPPED_INDUSTRY,
        "theme_exposures": underlying.get("themeExposures") or [],
        "market": underlying.get("market") or "其他",
        "region": underlying.get("region") or "其他",
        "currency": underlying.get("currency") or "其他",
        "source": "fund_lookthrough",
        "source_label": "基金穿透",
        "source_asset_name": underlying.get("parentName") or "",
        "source_symbol": underlying.get("parentHoldingId") or "",
        "underlying_name": underlying.get("underlyingName") or "",
        "underlying_symbol": underlying.get("underlyingSymbol") or "",
        "lookthrough_type": underlying.get("lookthroughType") or "FUND_LOOKTHROUGH",
        "underlying_weight_in_fund": weight,
        **metrics,
    }


def _mapped_position_for_config(position: dict[str, Any], cfg: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    item = dict(position)
    mapping = _find_mapping(item, cfg) if cfg else None
    if mapping and mapping.get("asset_class"):
        item["raw_asset_type"] = item.get("asset_type")
        item["asset_type"] = mapping.get("asset_class")
        item["mapped_asset_class"] = mapping.get("asset_class")
        item["asset_class"] = mapping.get("asset_class")
        item["region"] = item.get("region") or mapping.get("region")
        item["currency"] = item.get("currency") or mapping.get("currency")
        item["industry"] = item.get("industry") or mapping.get("industry")
        item["theme"] = item.get("theme") or mapping.get("theme")
    return item, mapping


def _lookthrough_rows_for_mapped_position(
    position: dict[str, Any],
    mapping: dict[str, Any] | None,
    cfg: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if not cfg:
        return []
    rows = _find_lookthrough(position, cfg)
    if rows or not mapping:
        return rows
    proxy_position = {
        **position,
        "symbol": mapping.get("symbol") or position.get("symbol"),
        "asset_id": mapping.get("symbol") or position.get("asset_id"),
        "asset_name": mapping.get("asset_name") or position.get("asset_name"),
    }
    return _find_lookthrough(proxy_position, cfg)


def _closed_lookthrough_underlyings(
    holding: dict[str, Any],
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not rows:
        return []
    parent_id = holding.get("id") or holding.get("symbol") or holding.get("name")
    parent_name = holding.get("name") or holding.get("symbol") or ""
    normalized_rows: list[dict[str, Any]] = []
    weight_sum = 0.0
    for row in rows:
        weight = max(_num(row.get("underlying_weight")), 0.0)
        if weight <= 0:
            continue
        weight_sum += weight
        normalized_rows.append(
            {
                **row,
                "parentHoldingId": parent_id,
                "parentName": parent_name,
                "parentMarketValue": 0.0,
            }
        )
    underlyings = buildLookthroughHoldings([holding], normalized_rows)
    residual = max(1.0 - weight_sum, 0.0)
    if residual > 0.001:
        underlyings.append(
            {
                "parentHoldingId": parent_id,
                "parentName": parent_name,
                "underlyingSymbol": "",
                "underlyingName": "\u672a\u7a7f\u900f\u6b8b\u5dee",
                "underlyingWeightInFund": residual,
                "effectiveWeightInPortfolio": 0.0,
                "effectiveMarketValue": 0.0,
                "assetType": holding.get("assetType") or "ETF / \u57fa\u91d1",
                "industryBucket": UNMAPPED_INDUSTRY,
                "industryLeaf": None,
                "industrySubBucket": None,
                "themeExposures": [],
                "aiSubThemes": [],
                "market": holding.get("market") or "\u57fa\u91d1",
                "region": holding.get("region") or "\u5176\u4ed6",
                "currency": holding.get("currency") or "\u5176\u4ed6",
                "account": holding.get("account") or "",
                "lookthroughType": "PRODUCT_TAG",
                "confidence": "LOW",
            }
        )
    return underlyings


def _build_units(
    normalized: list[dict[str, Any]],
    underlyings: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    cfg: dict[str, Any] | None = None,
) -> tuple[list[AttributionUnit], list[AttributionUnit]]:
    by_pair, by_key = _build_position_metric_maps(positions)
    candidates = _position_metric_candidates(positions)
    instrument_units: list[AttributionUnit] = []
    exposure_units: list[AttributionUnit] = []
    lookthrough_parent_keys: set[str] = set()

    for holding in normalized:
        keys = _identity_keys(holding.get("id"), holding.get("symbol"), holding.get("name"))
        fallback_value = _num(holding.get("marketValue"))
        metrics = _find_position_metrics(
            candidates,
            keys=keys,
            account=holding.get("account") or "",
            fallback_market_value=fallback_value,
            consume=True,
        ) or _metrics_for_identity(
            keys=keys,
            account=holding.get("account") or "",
            by_pair=by_pair,
            by_key=by_key,
            fallback_market_value=fallback_value,
        )
        metrics["market_value_cny"] = fallback_value
        unit = _holding_unit(holding, metrics)
        instrument_units.append(unit)
        if holding.get("assetType") == "ETF / 基金" and holding.get("lookthroughStatus") == "lookthrough_available":
            lookthrough_parent_keys.update(keys)
        else:
            exposure_units.append(unit)

    for underlying in underlyings:
        if _text(underlying.get("lookthroughType")).upper() == "DIRECT":
            continue
        parent_keys = _identity_keys(underlying.get("parentHoldingId"), underlying.get("parentName"))
        fallback_parent_value = _num(underlying.get("effectiveMarketValue")) / max(_num(underlying.get("underlyingWeightInFund")), 1e-9)
        parent_metrics = _find_position_metrics(
            candidates,
            keys=parent_keys,
            account=underlying.get("account") or "",
            fallback_market_value=fallback_parent_value,
            consume=False,
        ) or _metrics_for_identity(
            keys=parent_keys,
            account=underlying.get("account") or "",
            by_pair=by_pair,
            by_key=by_key,
            fallback_market_value=fallback_parent_value,
        )
        exposure_units.append(_underlying_unit(underlying, parent_metrics))
        lookthrough_parent_keys.update(parent_keys)

    # Include closed or zero-value rows that still carry realized/holding PnL.
    seen_keys = set().union(*(_identity_keys(u.get("symbol"), u.get("name")) for u in instrument_units), set())
    closed_positions = [
        p
        for p in positions
        if _num(p.get("market_value_cny")) <= 0
        and abs(_position_metrics(p).get("pnl_cny", 0.0)) > 1e-9
        and not (_keys_for_position(p) & seen_keys)
    ]
    closed_pairs = []
    closed_for_normalization: list[dict[str, Any]] = []
    for position in closed_positions:
        mapped_position, mapping = _mapped_position_for_config(position, cfg)
        lookthrough_rows = _lookthrough_rows_for_mapped_position(mapped_position, mapping, cfg)
        if lookthrough_rows:
            mapped_position["has_lookthrough"] = True
        closed_pairs.append((mapped_position, lookthrough_rows))
        closed_for_normalization.append(mapped_position)

    for (position, lookthrough_rows), holding in zip(closed_pairs, normalizeHoldings(closed_for_normalization)):
        keys = _identity_keys(holding.get("id"), holding.get("symbol"), holding.get("name"))
        metrics = _metrics_for_identity(
            keys=keys,
            account=holding.get("account") or "",
            by_pair=by_pair,
            by_key=by_key,
            fallback_market_value=0.0,
        )
        unit = _holding_unit(holding, metrics)
        unit["source_label"] = "已清仓/零市值"
        instrument_units.append(unit)
        if keys & lookthrough_parent_keys:
            continue
        closed_underlyings = _closed_lookthrough_underlyings(holding, lookthrough_rows)
        if closed_underlyings:
            for underlying in closed_underlyings:
                exposure_units.append(_underlying_unit(underlying, metrics))
            lookthrough_parent_keys.update(keys)
        else:
            exposure_units.append(unit)

    return instrument_units, exposure_units


def _metric_sum(units: list[AttributionUnit], key: str) -> float:
    return sum(_num(unit.get(key)) for unit in units)


def _industry_attribution_eligible(unit: AttributionUnit) -> bool:
    return unit.get("asset_type") in {"A股个股", "港股个股", "美股个股", "欧股个股", "韩股个股", "ETF / 基金"}


def _source_detail(unit: AttributionUnit, scale: float = 1.0) -> dict[str, Any]:
    label = unit.get("source_asset_name") or unit.get("name")
    if unit.get("underlying_name") and unit.get("source") == "fund_lookthrough":
        label = f"{unit.get('source_asset_name')} / {unit.get('underlying_name')}"
    return {
        "name": label,
        "source_asset_name": unit.get("source_asset_name") or unit.get("name"),
        "source_symbol": unit.get("source_symbol") or unit.get("symbol"),
        "underlying_name": unit.get("underlying_name") or "",
        "underlying_symbol": unit.get("underlying_symbol") or "",
        "account": unit.get("account") or "",
        "source": unit.get("source_label") or unit.get("source"),
        "lookthrough_type": unit.get("lookthrough_type"),
        "pnl_cny": _num(unit.get("pnl_cny")) * scale,
        "daily_pnl_cny": _num(unit.get("daily_pnl_cny")) * scale,
        "daily_price_pnl_cny": _num(unit.get("daily_price_pnl_cny")) * scale,
        "daily_fx_pnl_cny": _num(unit.get("daily_fx_pnl_cny")) * scale,
        "market_value_cny": _num(unit.get("market_value_cny")) * scale,
        "weight": unit.get("underlying_weight_in_fund"),
    }


def _aggregate_dimension(
    units: list[AttributionUnit],
    label_fn: Callable[[AttributionUnit], str | None],
    *,
    total_pnl: float,
    total_daily_pnl: float,
    total_market_value: float,
    score_fn: Callable[[AttributionUnit], float] | None = None,
    extra_fn: Callable[[AttributionUnit], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for unit in units:
        label = label_fn(unit)
        if not label:
            continue
        score = score_fn(unit) if score_fn else 1.0
        if score <= 0:
            continue
        item = grouped.setdefault(
            label,
            {
                "name": label,
                "display_name": label,
                "pnl_cny": 0.0,
                "daily_pnl_cny": 0.0,
                "daily_price_pnl_cny": 0.0,
                "daily_fx_pnl_cny": 0.0,
                "market_value_cny": 0.0,
                "source_details": [],
            },
        )
        item["pnl_cny"] += _num(unit.get("pnl_cny")) * score
        item["daily_pnl_cny"] += _num(unit.get("daily_pnl_cny")) * score
        item["daily_price_pnl_cny"] += _num(unit.get("daily_price_pnl_cny")) * score
        item["daily_fx_pnl_cny"] += _num(unit.get("daily_fx_pnl_cny")) * score
        item["market_value_cny"] += _num(unit.get("market_value_cny")) * score
        item["source_details"].append(_source_detail(unit, score))
        if extra_fn:
            item.update({k: v for k, v in extra_fn(unit).items() if v not in (None, "")})

    rows: list[dict[str, Any]] = []
    for item in grouped.values():
        details = sorted(item["source_details"], key=lambda d: -abs(_num(d.get("pnl_cny"))))[:20]
        item["source_details"] = details
        item["top_sources"] = details[:5]
        item["value_cny"] = item["pnl_cny"]
        item["total_pnl_cny"] = item["pnl_cny"]
        item["pnl_contribution"] = item["pnl_cny"] / total_pnl if abs(total_pnl) > 1e-9 else 0.0
        item["daily_pnl_contribution"] = item["daily_pnl_cny"] / total_daily_pnl if abs(total_daily_pnl) > 1e-9 else 0.0
        item["exposure_weight"] = item["market_value_cny"] / total_market_value if total_market_value > 0 else 0.0
        item["weight"] = item["exposure_weight"]
        rows.append(item)
    return sorted(rows, key=lambda r: -abs(_num(r.get("pnl_cny"))))


def _theme_rows(units: list[AttributionUnit], *, total_pnl: float, total_daily_pnl: float, total_market_value: float) -> list[dict[str, Any]]:
    exploded: list[AttributionUnit] = []
    for unit in units:
        if not _is_theme_eligible(unit.get("asset_type") or ""):
            continue
        for theme in unit.get("theme_exposures") or []:
            item = dict(unit)
            item["_theme_name"] = theme.get("themeName")
            item["_theme_id"] = theme.get("themeId")
            item["_family"] = theme.get("family")
            item["_theme_score"] = _num(theme.get("score"))
            item["_confidence"] = theme.get("confidence")
            exploded.append(item)
    rows = _aggregate_dimension(
        exploded,
        lambda u: u.get("_theme_name"),
        total_pnl=total_pnl,
        total_daily_pnl=total_daily_pnl,
        total_market_value=total_market_value,
        score_fn=lambda u: _num(u.get("_theme_score")),
        extra_fn=lambda u: {
            "theme_id": u.get("_theme_id"),
            "family": u.get("_family"),
            "confidence": u.get("_confidence"),
        },
    )
    for row in rows:
        row["overlap_allowed"] = True
    return rows


def _underlying_rows(units: list[AttributionUnit], *, total_pnl: float, total_daily_pnl: float, total_market_value: float) -> list[dict[str, Any]]:
    rows = _aggregate_dimension(
        units,
        lambda u: _text(u.get("name") or "未命名底层"),
        total_pnl=total_pnl,
        total_daily_pnl=total_daily_pnl,
        total_market_value=total_market_value,
        extra_fn=lambda u: {
            "symbol": u.get("symbol"),
            "asset_type": u.get("asset_type"),
            "industry_bucket": u.get("industry_bucket"),
            "theme_names": [t.get("themeName") for t in (u.get("theme_exposures") or [])],
            "market": u.get("market"),
            "region": u.get("region"),
            "currency": u.get("currency"),
            "source": u.get("source_label"),
        },
    )
    return rows[:200]


def build_return_attribution_from_positions(
    positions: list[dict[str, Any]],
    config: dict[str, Any] | None = None,
    *,
    as_of: Any | None = None,
    industry_l1_by_symbol: dict[str, str] | None = None,
    asset_master_cur: Any | None = None,
    sync_asset_master: bool = False,
    account_cfg: dict[str, Any] | None = None,
    reconciliation_cur: Any | None = None,
    base_currency: str = "CNY",
    portfolio_nav_cny: float | Decimal | None = None,
) -> dict[str, Any]:
    """Build return attribution from normalized investment positions."""
    enriched_positions = _attach_sw_l1(positions, industry_l1_by_symbol)
    cfg = load_lookthrough_config(config)
    lookthrough = derive_lookthrough_payload(
        enriched_positions,
        config,
        as_of=as_of,
        industry_l1_by_symbol=industry_l1_by_symbol,
        asset_master_cur=asset_master_cur,
        sync_asset_master=sync_asset_master,
        portfolio_nav_cny=portfolio_nav_cny,
    )
    normalized = list(lookthrough.get("normalized_holdings") or [])
    underlyings = list(lookthrough.get("underlying_holdings") or [])
    instrument_units, exposure_units = _build_units(normalized, underlyings, enriched_positions, cfg=cfg)
    reconciliation_meta: dict[str, Any] = {"sections": []}
    reconciliation_warnings: list[str] = []
    reconciliation_units: list[AttributionUnit] = []
    if reconciliation_cur is not None and account_cfg is not None:
        targets, reconciliation_warnings = _section_target_payloads(
            reconciliation_cur,
            account_cfg=account_cfg,
            base_currency=base_currency,
            view_as_of=_latest_snapshot_date(reconciliation_cur),
        )
        targets = {
            section: target
            for section, target in targets.items()
            if section in CANONICAL_RECONCILIATION_SECTIONS
        }
        reconciliation_units, reconciliation_meta = _return_reconciliation_units(instrument_units, targets)

    total_market_value = _metric_sum(instrument_units, "market_value_cny")
    total_pnl = _metric_sum(instrument_units, "pnl_cny")
    total_daily_pnl = _metric_sum(instrument_units, "daily_pnl_cny")
    total_daily_price = _metric_sum(instrument_units, "daily_price_pnl_cny")
    total_daily_fx = _metric_sum(instrument_units, "daily_fx_pnl_cny")
    canonical_total_pnl = total_pnl + _metric_sum(reconciliation_units, "pnl_cny")
    canonical_daily_pnl = total_daily_pnl + _metric_sum(reconciliation_units, "daily_pnl_cny")
    nav_value = _num(portfolio_nav_cny) if portfolio_nav_cny is not None else _num(lookthrough.get("portfolio_nav_cny"))

    dimensions = {
        "holding": _underlying_rows(
            instrument_units,
            total_pnl=total_pnl,
            total_daily_pnl=total_daily_pnl,
            total_market_value=total_market_value,
        ),
        "asset_type": _aggregate_dimension(
            instrument_units,
            lambda u: u.get("asset_type") or "其他资产",
            total_pnl=total_pnl,
            total_daily_pnl=total_daily_pnl,
            total_market_value=total_market_value,
        ),
        "industry": _aggregate_dimension(
            [u for u in exposure_units if _industry_attribution_eligible(u)],
            lambda u: u.get("industry_bucket") or UNMAPPED_INDUSTRY,
            total_pnl=total_pnl,
            total_daily_pnl=total_daily_pnl,
            total_market_value=total_market_value,
        ),
        "theme": _theme_rows(
            exposure_units,
            total_pnl=total_pnl,
            total_daily_pnl=total_daily_pnl,
            total_market_value=total_market_value,
        ),
        "region": _aggregate_dimension(
            exposure_units,
            lambda u: u.get("region") or "其他",
            total_pnl=total_pnl,
            total_daily_pnl=total_daily_pnl,
            total_market_value=total_market_value,
        ),
        "market": _aggregate_dimension(
            exposure_units,
            lambda u: u.get("market") or "其他",
            total_pnl=total_pnl,
            total_daily_pnl=total_daily_pnl,
            total_market_value=total_market_value,
        ),
        "currency": _aggregate_dimension(
            instrument_units,
            lambda u: u.get("currency") or "其他",
            total_pnl=total_pnl,
            total_daily_pnl=total_daily_pnl,
            total_market_value=total_market_value,
        ),
        "underlying": _underlying_rows(
            exposure_units,
            total_pnl=total_pnl,
            total_daily_pnl=total_daily_pnl,
            total_market_value=total_market_value,
        ),
    }

    quality = dict(lookthrough.get("quality") or {})
    if reconciliation_meta.get("sections") or reconciliation_warnings:
        quality["return_reconciliation"] = {
            **reconciliation_meta,
            "warnings": reconciliation_warnings,
        }

    notes = [
        "行业、地区、市场、底层归因使用穿透后的 exposure units；资产类型和币种按实际持有工具归因。",
        "主题归因按 theme_score 加权，主题可重叠，合计不要求等于组合总收益。",
        "日收益、价格收益、汇率收益字段已预留；当前后端持仓快照缺少拆分时显示为 0。",
    ]
    if lookthrough.get("quality", {}).get("uncovered_value_cny"):
        notes.append("未穿透残差会承接基金收益，但不进入主题主图。")

    payload = {
        "status": "ok" if positions else "empty",
        "as_of": lookthrough.get("as_of"),
        "portfolio_nav_cny": nav_value,
        "gross_exposure_cny": total_market_value,
        "total_market_value_cny": total_market_value,
        "total_pnl_cny": total_pnl,
        "canonical_total_pnl_cny": canonical_total_pnl,
        "daily_pnl_cny": total_daily_pnl,
        "canonical_daily_pnl_cny": canonical_daily_pnl,
        "daily_price_pnl_cny": total_daily_price,
        "daily_fx_pnl_cny": total_daily_fx,
        "dimensions": dimensions,
        "quality": quality,
        "notes": notes,
        "model_version": "return_attribution_v1",
    }
    return payload


def build_return_attribution_payload(
    cur,
    *,
    base_currency: str = "CNY",
    sync_asset_master: bool = False,
) -> dict[str, Any]:
    """Build the /api/analysis/return-attribution payload from current holdings."""
    account_cfg = _load_accounts_config()
    account_balances = load_account_balances(cur, base_currency, account_cfg)
    portfolio_nav_cny = sum(
        (Decimal(str(row.get("amount_cny") or 0)) for row in account_balances if row.get("include_in_nav")),
        Decimal("0"),
    )
    view_as_of = _latest_snapshot_date(cur)
    owner_ratios, default_owner_ratio = _owner_ratios(account_cfg)
    holdings = build_holdings_latest_payload(
        cur,
        limit=5000,
        include_closed=False,
        view_as_of=view_as_of,
        excluded_accounts=_excluded_accounts(account_cfg),
        owner_ratios=owner_ratios,
        default_owner_ratio=default_owner_ratio,
        daily_pnl_start_as_of=daily_pnl_window_date(),
    )
    positions = [
        _daily_row_to_position(row)
        for row in holdings.get("rows") or []
        if _is_return_attribution_row(row)
    ]
    daily_positions = [
        position
        for position in _load_daily_attribution_positions(cur, account_cfg)
        if _is_return_attribution_row(position)
    ]
    positions = _merge_daily_positions(positions, daily_positions)
    industry_l1_by_symbol = _load_sw_l1_lookup(cur)
    return build_return_attribution_from_positions(
        positions,
        as_of=holdings.get("as_of") or view_as_of,
        industry_l1_by_symbol=industry_l1_by_symbol,
        asset_master_cur=cur,
        sync_asset_master=sync_asset_master,
        account_cfg=account_cfg,
        reconciliation_cur=cur if (account_cfg.get("accounts") or {}) else None,
        base_currency=base_currency,
        portfolio_nav_cny=portfolio_nav_cny,
    )
