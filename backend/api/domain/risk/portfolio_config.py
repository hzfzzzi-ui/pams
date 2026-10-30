"""Portfolio allocation rule assembly for the investment config page."""
from __future__ import annotations

import re
from typing import Any

import psycopg2.extras

from domain.assets.lookthrough_state import build_lookthrough_payload
from domain.risk.portfolio_risk_disclosure import build_portfolio_config_input_summary
from domain.investments.portfolio_state import get_conn
from domain.risk.risk_state import build_risk_state


DEFAULT_PLAN = {
    "esop_add_cny": 400000.0,
    "esop_debt_cny": 320000.0,
    "esop_cash_cny": 80000.0,
    "debt_rate": 0.03,
}

INDEX_SYMBOLS = {"QQQ", "SPY", "VOO", "VTI", "IVV", "SCHB", "SPLG", "VT", "ACWI"}
CODE_PREFIX_RE = re.compile(r"^([A-Z]{1,8}(?:\.[A-Z]{1,4})?|\d{4,6}(?:\.[A-Z]{1,4})?)\s*[-–]\s*(.+)$", re.I)
PUBLIC_EQUITY_ASSET_TYPES = {"A股个股", "港股个股", "美股个股", "欧股个股", "韩股个股", "ETF / 基金"}
PUBLIC_EQUITY_MARKETS = {"A股", "港股", "美股", "欧股", "韩股"}
NON_PUBLIC_EQUITY_ASSET_TYPES = {
    "非上市股权",
    "黄金 / 商品",
    "现金",
    "债券 / 货币基金",
    "房地产 / 实物资产",
    "应收款 / 待到账",
}

CONFIG_GROUPS = [
    {
        "id": "primary_allocation",
        "name": "第一层配置",
        "description": "按当前配置方案直接管理的配置项。",
    },
]


def _n(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if parsed == parsed else 0.0


def _pct(value: float) -> str:
    return f"{_n(value) * 100:.1f}%"


def _money(value: float) -> str:
    return f"¥{abs(_n(value)):,.0f}"


def _money_signed(value: float) -> str:
    parsed = _n(value)
    prefix = "-" if parsed < 0 else ""
    return f"{prefix}¥{abs(parsed):,.0f}"


def _rows(payload: dict[str, Any], dimension: str) -> list[dict[str, Any]]:
    rows = (payload.get("exposures") or {}).get(dimension)
    return rows if isinstance(rows, list) else []


def _name(row: dict[str, Any]) -> str:
    return str(
        row.get("display_name")
        or row.get("name")
        or row.get("label")
        or row.get("bucket")
        or row.get("key")
        or ""
    )


def _value(row: dict[str, Any]) -> float:
    return _n(
        row.get("value_cny")
        or row.get("market_value_cny")
        or row.get("value")
        or row.get("amount_cny")
        or row.get("marketValue")
    )


def _weight(row: dict[str, Any], total: float) -> float:
    explicit = _n(row.get("weight") or row.get("total_weight") or row.get("weight_pct"))
    if explicit > 0:
        return explicit / 100 if explicit > 1 else explicit
    value = _value(row)
    return value / total if total > 0 else 0.0


def _find_exposure(payload: dict[str, Any], dimension: str, names: str | list[str]) -> dict[str, Any] | None:
    targets = names if isinstance(names, list) else [names]
    for row in _rows(payload, dimension):
        name = _name(row)
        if any(target == name or target in name for target in targets):
            return row
    return None


def _exposure_weight(payload: dict[str, Any], dimension: str, names: str | list[str], total: float) -> float:
    row = _find_exposure(payload, dimension, names)
    return _weight(row, total) if row else 0.0


def _exposure_value(payload: dict[str, Any], dimension: str, names: str | list[str], total: float | None = None) -> float:
    targets = names if isinstance(names, list) else [names]
    value_total = 0.0
    for target in targets:
        row = _find_exposure(payload, dimension, target)
        if row:
            value = _value(row)
            if value <= 0 and total is not None:
                value = _weight(row, total) * total
            value_total += value
    return value_total


def _source_name(row: dict[str, Any]) -> str:
    return str(row.get("display_name") or row.get("name") or row.get("asset_name") or row.get("symbol") or "")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _lookup_key(value: Any) -> str:
    return _text(value).casefold()


def _source_symbol(row: dict[str, Any]) -> str:
    symbol = _text(
        row.get("symbol")
        or row.get("primary_symbol")
        or row.get("asset_id")
        or row.get("id")
        or row.get("underlyingSymbol")
        or row.get("parentHoldingId")
    )
    if symbol:
        return symbol
    match = CODE_PREFIX_RE.match(_source_name(row))
    return match.group(1).strip() if match else ""


def _clean_asset_display_name(name: Any, symbol: Any = "") -> str:
    text = re.sub(r"\s+", " ", _text(name))
    sym = _text(symbol)
    if not text:
        return sym
    match = CODE_PREFIX_RE.match(text)
    if match and (not sym or match.group(1).casefold() == sym.casefold()):
        text = match.group(2).strip()
    elif sym and text.casefold().startswith(f"{sym.casefold()} - "):
        text = text[len(sym) + 3 :].strip()
    return text or sym


def _better_display_name(current: str, candidate: str) -> str:
    if not current:
        return candidate
    if not candidate:
        return current
    if any("\u4e00" <= char <= "\u9fff" for char in candidate) and not any(
        "\u4e00" <= char <= "\u9fff" for char in current
    ):
        return candidate
    return candidate if len(candidate) > len(current) else current


def _asset_lookup_item(asset_label_lookup: dict[str, dict[str, str]], *values: Any) -> dict[str, str] | None:
    for value in values:
        key = _lookup_key(value)
        if key and key in asset_label_lookup:
            return asset_label_lookup[key]
    return None


def _asset_label_source(
    asset_label_lookup: dict[str, dict[str, str]],
    *,
    symbol: Any = "",
    name: Any = "",
    fallback_name: Any = "",
) -> dict[str, Any]:
    lookup = _asset_lookup_item(asset_label_lookup, symbol, name, fallback_name)
    source_symbol = _text(symbol)
    display_name = _text(name or fallback_name or source_symbol)
    if lookup:
        source_symbol = lookup.get("symbol") or source_symbol
        display_name = lookup.get("display_name") or display_name
    return {
        "name": _clean_asset_display_name(display_name, source_symbol),
        "symbol": source_symbol,
    }


def _source_value(row: dict[str, Any]) -> float:
    return _n(row.get("value_cny") or row.get("market_value_cny") or row.get("position_value_cny") or row.get("market_value"))


def _children_from_sources(
    sources: list[dict[str, Any]],
    nav: float,
    *,
    limit: int = 16,
    total_cny: float | None = None,
    residual_name: str = "其他合计",
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for source in sources or []:
        symbol = _source_symbol(source)
        name = _clean_asset_display_name(_source_name(source), symbol)
        value = _source_value(source)
        if not name or value <= 0:
            continue
        key = f"symbol:{symbol.casefold()}" if symbol else f"name:{name.casefold()}"
        item = merged.setdefault(key, {"name": name, "symbol": symbol, "amount_cny": 0.0})
        item["name"] = _better_display_name(item.get("name", ""), name)
        if symbol and not item.get("symbol"):
            item["symbol"] = symbol
        item["amount_cny"] += value
    sorted_children = sorted(merged.values(), key=lambda row: -_n(row["amount_cny"]))
    children = sorted_children[:limit]
    known_total = sum(_n(row.get("amount_cny")) for row in sorted_children)
    target_total = _n(total_cny) if total_cny is not None else known_total
    residual_value = target_total - sum(_n(row.get("amount_cny")) for row in children)
    if residual_value > 1:
        children.append({"name": residual_name, "amount_cny": residual_value})
    for child in children:
        if child.get("symbol"):
            child["symbol"] = str(child["symbol"])
        child["amount_label"] = _money(child["amount_cny"])
        child["weight"] = child["amount_cny"] / nav if nav > 0 else 0.0
        child["weight_label"] = _pct(child["weight"])
    return children


def _children_from_signed_sources(
    sources: list[dict[str, Any]],
    nav: float,
    *,
    limit: int = 16,
    total_cny: float | None = None,
    residual_name: str = "其他合计",
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for source in sources or []:
        symbol = _source_symbol(source)
        name = _clean_asset_display_name(_source_name(source), symbol)
        value = _source_value(source)
        if not name or abs(value) <= 1:
            continue
        key = f"symbol:{symbol.casefold()}" if symbol else f"name:{name.casefold()}"
        item = merged.setdefault(key, {"name": name, "symbol": symbol, "amount_cny": 0.0})
        item["name"] = _better_display_name(item.get("name", ""), name)
        if symbol and not item.get("symbol"):
            item["symbol"] = symbol
        item["amount_cny"] += value
    sorted_children = sorted(merged.values(), key=lambda row: -abs(_n(row["amount_cny"])))
    children = sorted_children[:limit]
    known_total = sum(_n(row.get("amount_cny")) for row in sorted_children)
    target_total = _n(total_cny) if total_cny is not None else known_total
    residual_value = target_total - sum(_n(row.get("amount_cny")) for row in children)
    if abs(residual_value) > 1:
        children.append({"name": residual_name, "amount_cny": residual_value})
    for child in children:
        if child.get("symbol"):
            child["symbol"] = str(child["symbol"])
        child["amount_label"] = _money_signed(child["amount_cny"])
        child["weight"] = child["amount_cny"] / nav if nav > 0 else 0.0
        child["weight_label"] = _pct(child["weight"])
    return children


def _allocation_child(name: str, amount_cny: float, nav: float, *, rule_id: str | None = None) -> dict[str, Any]:
    child = {
        "name": name,
        "amount_cny": _n(amount_cny),
        "amount_label": _money(amount_cny),
        "weight": _n(amount_cny) / nav if nav > 0 else 0.0,
        "weight_label": _pct(_n(amount_cny) / nav if nav > 0 else 0.0),
    }
    if rule_id:
        child["rule_id"] = rule_id
    return child


def _allocation_child_signed(name: str, amount_cny: float, nav: float, *, rule_id: str | None = None) -> dict[str, Any]:
    child = {
        "name": name,
        "amount_cny": _n(amount_cny),
        "amount_label": _money_signed(amount_cny),
        "weight": _n(amount_cny) / nav if nav > 0 else 0.0,
        "weight_label": _pct(_n(amount_cny) / nav if nav > 0 else 0.0),
    }
    if rule_id:
        child["rule_id"] = rule_id
    return child


def _children_from_exposure(
    payload: dict[str, Any],
    dimension: str,
    names: str | list[str],
    nav: float,
    *,
    limit: int = 16,
) -> list[dict[str, Any]]:
    targets = names if isinstance(names, list) else [names]
    sources: list[dict[str, Any]] = []
    target_total = 0.0
    for target in targets:
        row = _find_exposure(payload, dimension, target)
        if not row:
            continue
        target_total += _value(row)
        row_sources = row.get("main_sources") or row.get("top_sources") or []
        if row_sources:
            sources.extend(row_sources)
        else:
            sources.append({"name": _name(row), "value_cny": _value(row)})
    return _children_from_sources(sources, nav, limit=limit, total_cny=target_total)


def _children_from_rows(rows: list[dict[str, Any]], nav: float, *, limit: int = 16) -> list[dict[str, Any]]:
    sources = []
    total = 0.0
    for row in rows:
        total += _value(row) or _source_value(row)
        sources.append({
            "name": _name(row) or _source_name(row),
            "value_cny": _value(row) or _source_value(row),
        })
    return _children_from_sources(sources, nav, limit=limit, total_cny=total)


def _holdings(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("normalized_holdings")
    return rows if isinstance(rows, list) else []


def _holding_value(row: dict[str, Any]) -> float:
    return _n(
        row.get("marketValue")
        or row.get("market_value_cny")
        or row.get("position_value_cny")
        or row.get("market_value")
    )


def _holding_identity(row: dict[str, Any]) -> str:
    return str(row.get("id") or row.get("symbol") or row.get("asset_id") or "").strip().upper()


def _underlyings(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("underlying_holdings")
    return rows if isinstance(rows, list) else []


def _underlying_value(row: dict[str, Any]) -> float:
    return _n(row.get("effectiveMarketValue") or row.get("effective_market_value_cny") or row.get("value_cny"))


def _underlying_parent_identity(row: dict[str, Any]) -> str:
    return str(row.get("parentHoldingId") or row.get("parent_symbol") or "").strip().upper()


def _asset_lookup_candidates(payload: dict[str, Any]) -> set[str]:
    candidates: set[str] = set()
    for row in [*_underlyings(payload), *_holdings(payload)]:
        for key in (
            "symbol",
            "primary_symbol",
            "asset_id",
            "id",
            "name",
            "display_name",
            "asset_name",
            "parentHoldingId",
            "parentName",
            "underlyingSymbol",
            "underlyingName",
        ):
            value = _text(row.get(key))
            if value:
                candidates.add(value)
    return candidates


def _put_asset_label(
    lookup: dict[str, dict[str, str]],
    key: Any,
    *,
    symbol: Any,
    display_name: Any,
) -> None:
    normalized = _lookup_key(key)
    name = _text(display_name)
    sym = _text(symbol)
    if not normalized or not name:
        return
    lookup[normalized] = {"symbol": sym, "display_name": name}


def _asset_label_lookup(cur, keys: set[str]) -> dict[str, dict[str, str]]:
    clean_keys = sorted({_text(key) for key in keys if _text(key)})
    if not clean_keys:
        return {}
    lookup: dict[str, dict[str, str]] = {}
    lower_keys = [key.casefold() for key in clean_keys]

    cur.execute(
        """
        SELECT
            am.asset_id,
            am.primary_symbol,
            COALESCE(NULLIF(am.display_name, ''), NULLIF(am.canonical_name, ''), am.primary_symbol) AS display_name
        FROM pams_asset_master am
        WHERE lower(am.asset_id) = ANY(%s::text[])
           OR lower(am.primary_symbol) = ANY(%s::text[])
           OR lower(COALESCE(am.display_name, '')) = ANY(%s::text[])
           OR lower(COALESCE(am.canonical_name, '')) = ANY(%s::text[])
        """,
        (lower_keys, lower_keys, lower_keys, lower_keys),
    )
    for row in cur.fetchall():
        for key in (row.get("asset_id"), row.get("primary_symbol"), row.get("display_name")):
            _put_asset_label(lookup, key, symbol=row.get("primary_symbol"), display_name=row.get("display_name"))

    cur.execute(
        """
        SELECT
            al.alias_value,
            am.asset_id,
            am.primary_symbol,
            COALESCE(NULLIF(am.display_name, ''), NULLIF(am.canonical_name, ''), am.primary_symbol) AS display_name
        FROM pams_asset_alias al
        JOIN pams_asset_master am ON am.asset_id = al.asset_id
        WHERE lower(al.alias_value) = ANY(%s::text[])
        """,
        (lower_keys,),
    )
    for row in cur.fetchall():
        for key in (row.get("alias_value"), row.get("asset_id"), row.get("primary_symbol"), row.get("display_name")):
            _put_asset_label(lookup, key, symbol=row.get("primary_symbol"), display_name=row.get("display_name"))
    return lookup


def _config_market(row: dict[str, Any]) -> str:
    market = str(row.get("market") or "")
    if market in PUBLIC_EQUITY_MARKETS:
        return market
    asset_type = str(row.get("assetType") or row.get("asset_type") or "")
    region = str(row.get("region") or "")
    currency = str(row.get("currency") or "")
    text = " ".join(
        str(row.get(key) or "")
        for key in ("name", "parentName", "underlyingName", "symbol", "underlyingSymbol", "assetSubtype")
    )
    if "韩国" in region or currency == "KRW" or re.search(r"(韩国|KOSPI|KOSDAQ|\.KS\b)", text, re.I):
        return "韩股"
    if "欧洲" in region or "瑞典" in region or currency == "SEK" or re.search(r"(欧洲|欧股|瑞典|Sweden|Sverige|Stockholm|SEK)", text, re.I):
        return "欧股"
    if asset_type == "A股个股":
        return "A股"
    if asset_type == "港股个股":
        return "港股"
    if asset_type == "美股个股":
        return "美股"
    if asset_type == "欧股个股":
        return "欧股"
    if asset_type == "韩股个股":
        return "韩股"
    if re.search(r"(恒生|港股|港股通|香港|H股)", text, re.I):
        return "港股"
    if re.search(r"(Nasdaq|纳斯达克|标普|S&P|美股|美国|QQQ|VOO|VTI|SPY|IVV)", text, re.I):
        return "美股"
    if re.search(r"(韩国|KOSPI|KOSDAQ)", text, re.I):
        return "韩股"
    if "中国" in region or currency == "CNY" or re.search(r"(中证|沪深|创业|科创|A股)", text, re.I):
        return "A股"
    return market or "其他"


def _equity_bucket_for_market(market: str) -> str:
    if market == "美股":
        return "us_equity"
    if market in {"A股", "港股"}:
        return "cn_hk_equity"
    if market in {"韩股", "欧股"}:
        return "kr_other_equity"
    return "other_equity"


def _is_public_equity_row(row: dict[str, Any]) -> bool:
    asset_type = str(row.get("assetType") or row.get("asset_type") or "")
    if asset_type in NON_PUBLIC_EQUITY_ASSET_TYPES:
        return False
    if asset_type in PUBLIC_EQUITY_ASSET_TYPES:
        return True
    return str(row.get("market") or "") in PUBLIC_EQUITY_MARKETS


def _underlying_source_name(row: dict[str, Any]) -> str:
    parent = str(row.get("parentName") or "").strip()
    underlying = str(row.get("underlyingName") or "").strip()
    symbol = str(row.get("underlyingSymbol") or "").strip()
    if underlying == "未穿透残差":
        return f"{parent} 未穿透残差" if parent else underlying
    if str(row.get("lookthroughType") or "").upper() == "DIRECT" and parent:
        return parent
    if underlying:
        return f"{symbol} - {underlying}" if symbol and symbol not in underlying else underlying
    return parent or symbol


def _underlying_source(
    row: dict[str, Any],
    asset_label_lookup: dict[str, dict[str, str]],
) -> dict[str, Any]:
    parent = _text(row.get("parentName"))
    underlying = _text(row.get("underlyingName"))
    symbol = _text(row.get("underlyingSymbol"))
    parent_id = _text(row.get("parentHoldingId"))
    if underlying == "未穿透残差":
        parent_label = _asset_label_source(
            asset_label_lookup,
            symbol=parent_id,
            name=parent,
            fallback_name=parent,
        )
        return {
            "name": f"{parent_label['name']} 未穿透残差" if parent_label["name"] else underlying,
            "symbol": parent_label.get("symbol") or parent_id,
        }
    fallback = _underlying_source_name(row)
    return _asset_label_source(
        asset_label_lookup,
        symbol=symbol or parent_id,
        name=underlying or parent,
        fallback_name=fallback,
    )


def _holding_source(
    row: dict[str, Any],
    asset_label_lookup: dict[str, dict[str, str]],
) -> dict[str, Any]:
    return _asset_label_source(
        asset_label_lookup,
        symbol=row.get("symbol") or row.get("asset_id") or row.get("id"),
        name=row.get("display_name") or row.get("name") or row.get("asset_name"),
        fallback_name=_source_name(row),
    )


def _equity_market_model(
    payload: dict[str, Any],
    asset_label_lookup: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    asset_label_lookup = asset_label_lookup or {}
    buckets: dict[str, list[dict[str, Any]]] = {
        "us_equity": [],
        "cn_hk_equity": [],
        "kr_other_equity": [],
        "other_equity": [],
    }
    market_amounts = {"A股": 0.0, "港股": 0.0, "美股": 0.0, "欧股": 0.0, "韩股": 0.0, "其他": 0.0}
    covered_parents: set[str] = set()

    for row in _underlyings(payload):
        value = _underlying_value(row)
        if value <= 0 or not _is_public_equity_row(row):
            continue
        market = _config_market(row)
        bucket = _equity_bucket_for_market(market)
        source = _underlying_source(row, asset_label_lookup)
        buckets[bucket].append({
            "name": source["name"],
            "symbol": source.get("symbol") or "",
            "value_cny": value,
        })
        market_amounts[market if market in market_amounts else "其他"] += value
        parent_id = _underlying_parent_identity(row)
        if parent_id:
            covered_parents.add(parent_id)

    for holding in _holdings(payload):
        value = _holding_value(holding)
        identity = _holding_identity(holding)
        if value <= 0 or not _is_public_equity_row(holding) or (identity and identity in covered_parents):
            continue
        market = _config_market(holding)
        bucket = _equity_bucket_for_market(market)
        source = _holding_source(holding, asset_label_lookup)
        buckets[bucket].append({
            "name": source["name"],
            "symbol": source.get("symbol") or "",
            "value_cny": value,
        })
        market_amounts[market if market in market_amounts else "其他"] += value

    amounts = {
        key: sum(_source_value(row) for row in rows)
        for key, rows in buckets.items()
    }
    amounts["total"] = sum(amounts.values())
    return {"buckets": buckets, "amounts": amounts, "market_amounts": market_amounts}


def _is_us_broad_fund(row: dict[str, Any]) -> bool:
    symbol = str(row.get("symbol") or row.get("asset_id") or "").upper()
    name = str(row.get("name") or row.get("display_name") or row.get("asset_name") or "")
    if symbol in INDEX_SYMBOLS:
        return True
    return bool(re.search(r"(S&P|Nasdaq|纳斯达克|标普|全市场|Total Market|QQQ|VOO|VTI|SPY|IVV)", name, re.I))


def _broad_us_weight(payload: dict[str, Any], total: float) -> float:
    if total <= 0:
        return 0.0
    return sum(_holding_value(row) for row in _holdings(payload) if _is_us_broad_fund(row)) / total


def _broad_us_value(payload: dict[str, Any]) -> float:
    return sum(_holding_value(row) for row in _holdings(payload) if _is_us_broad_fund(row))


def _broad_us_children(payload: dict[str, Any], nav: float) -> list[dict[str, Any]]:
    rows = []
    for row in _holdings(payload):
        if _is_us_broad_fund(row):
            rows.append({
                "name": row.get("asset_name") or row.get("name") or row.get("display_name") or row.get("asset_id"),
                "value_cny": _holding_value(row),
            })
    return _children_from_sources(rows, nav)


def _esop_weight(risk: dict[str, Any], lookthrough: dict[str, Any], total: float) -> float:
    positions = risk.get("positions") or []
    direct = sum(
        _n(row.get("position_value_cny"))
        for row in positions
        if str(row.get("asset_id") or "").upper() == "HUAWEI_ESOP"
        or "esop" in str(row.get("asset_type") or "").lower()
        or "ESOP" in str(row.get("asset_name") or "").upper()
    )
    if direct > 0 and total > 0:
        return direct / total
    return _exposure_weight(lookthrough, "asset_type", "非上市股权", total)


def _esop_value(risk: dict[str, Any], lookthrough: dict[str, Any]) -> float:
    positions = risk.get("positions") or []
    direct = sum(
        _n(row.get("position_value_cny"))
        for row in positions
        if str(row.get("asset_id") or "").upper() == "HUAWEI_ESOP"
        or "esop" in str(row.get("asset_type") or "").lower()
        or "ESOP" in str(row.get("asset_name") or "").upper()
    )
    return direct if direct > 0 else _exposure_value(lookthrough, "asset_type", "非上市股权")


def _esop_children(risk: dict[str, Any], lookthrough: dict[str, Any], nav: float) -> list[dict[str, Any]]:
    positions = risk.get("positions") or []
    rows = [
        {
            "name": row.get("asset_name") or row.get("asset_id"),
            "value_cny": _n(row.get("position_value_cny")),
        }
        for row in positions
        if str(row.get("asset_id") or "").upper() == "HUAWEI_ESOP"
        or "esop" in str(row.get("asset_type") or "").lower()
        or "ESOP" in str(row.get("asset_name") or "").upper()
    ]
    if rows:
        return _children_from_sources(rows, nav)
    return _children_from_exposure(lookthrough, "asset_type", "非上市股权", nav)


def _position_value(row: dict[str, Any]) -> float:
    return _n(
        row.get("position_value_cny")
        or row.get("market_value_cny")
        or row.get("value_cny")
        or row.get("amount_cny")
    )


def _cash_balance_sources(risk: dict[str, Any]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for row in (risk.get("holdings") or []):
        asset_type = str(row.get("asset_type") or "").lower()
        symbol = str(row.get("symbol") or row.get("asset_id") or "").upper()
        name = str(row.get("asset_name") or row.get("name") or "")
        account_name = str(row.get("account_name") or row.get("account_code") or "").strip()
        if asset_type != "cash" and not symbol.startswith("CASH") and "现金" not in name and "Cash" not in name:
            continue
        amount = _position_value(row)
        if abs(amount) <= 1:
            continue
        display_name = name or account_name or "现金"
        if account_name and account_name not in display_name:
            display_name = f"{account_name} · {display_name}"
        sources.append({
            "name": display_name,
            "symbol": "",
            "value_cny": amount,
        })
    return sources


def _is_short_debt_name(name: str) -> bool:
    return any(token in name for token in ["债券 / 货币基金", "货币基金", "短债", "短久期"])


def _cash_short_sources(risk: dict[str, Any], lookthrough: dict[str, Any]) -> list[dict[str, Any]]:
    sources = _cash_balance_sources(risk)
    has_account_cash = bool(sources)
    for row in _rows(lookthrough, "asset_type"):
        name = _name(row)
        include_cash_exposure = "现金" in name and not has_account_cash
        if not include_cash_exposure and not _is_short_debt_name(name):
            continue
        row_sources = row.get("main_sources") or row.get("top_sources") or []
        if row_sources:
            sources.extend(
                {
                    "name": _source_name(source),
                    "symbol": _source_symbol(source),
                    "value_cny": _source_value(source),
                }
                for source in row_sources
            )
        else:
            sources.append({"name": name, "value_cny": _value(row)})
    return sources


def _cash_short_asset_sources(risk: dict[str, Any], lookthrough: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in _cash_short_sources(risk, lookthrough) if _source_value(row) > 1]


def _cash_short_value(risk: dict[str, Any], lookthrough: dict[str, Any]) -> float:
    return sum(_source_value(row) for row in _cash_short_asset_sources(risk, lookthrough))


def _cash_short_children(risk: dict[str, Any], lookthrough: dict[str, Any], nav: float, total_cny: float) -> list[dict[str, Any]]:
    return _children_from_sources(_cash_short_asset_sources(risk, lookthrough), nav, total_cny=total_cny)


def _negative_cash_cny(risk: dict[str, Any]) -> float:
    summary_value = _n((risk.get("summary") or {}).get("negative_cash_cny"))
    if summary_value:
        return abs(summary_value)
    return sum(
        abs(_source_value(row))
        for row in _cash_balance_sources(risk)
        if _source_value(row) < 0
    )


def _financing_metrics_from_debt(current_debt_cny: float, plan: dict[str, Any]) -> dict[str, float]:
    rate = _n(plan.get("debt_rate"))
    planned_debt = max(0.0, _n(plan.get("esop_debt_cny")))
    current_debt = max(0.0, abs(_n(current_debt_cny)))
    post_debt = current_debt + planned_debt
    current_annual = current_debt * rate
    post_annual = post_debt * rate
    return {
        "rate": rate,
        "current_debt_cny": current_debt,
        "planned_debt_cny": planned_debt,
        "post_debt_cny": post_debt,
        "current_annual_interest_cny": current_annual,
        "post_annual_interest_cny": post_annual,
        "post_monthly_interest_cny": post_annual / 12 if post_annual else 0.0,
    }


def _financing_metrics(risk: dict[str, Any], plan: dict[str, Any]) -> dict[str, float]:
    return _financing_metrics_from_debt(_negative_cash_cny(risk), plan)


def _financing_children(risk: dict[str, Any], nav: float, total_debt_cny: float) -> list[dict[str, Any]]:
    sources = [row for row in _cash_balance_sources(risk) if _source_value(row) < -1]
    if not sources and total_debt_cny > 1:
        sources = [{"name": "\u878d\u8d44\u8d1f\u503a", "value_cny": -abs(total_debt_cny)}]
    return _children_from_signed_sources(sources, nav, total_cny=-abs(total_debt_cny))


def _financing_cost_children(metrics: dict[str, float], nav: float) -> list[dict[str, Any]]:
    planned_annual = metrics["planned_debt_cny"] * metrics["rate"]
    return [
        _allocation_child("当前年息", metrics["current_annual_interest_cny"], nav),
        _allocation_child("新增融资年息", planned_annual, nav),
    ]


def _non_allocation_asset_sources(risk: dict[str, Any], lookthrough: dict[str, Any]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    commodity = _find_exposure(lookthrough, "asset_type", "黄金 / 商品")
    if commodity:
        commodity_sources = commodity.get("main_sources") or commodity.get("top_sources") or []
        if commodity_sources:
            sources.extend(commodity_sources)
        else:
            sources.append({"name": _name(commodity), "value_cny": _value(commodity)})

    for row in risk.get("positions") or []:
        asset_type = _text(row.get("asset_type")).lower()
        name = _text(row.get("asset_name") or row.get("asset_id"))
        value = _n(row.get("position_value_cny"))
        if value <= 1:
            continue
        if asset_type == "manual_asset" or asset_type in {"receivable", "benefit"}:
            sources.append({"name": name, "value_cny": value})
    return sources


def _non_allocation_asset_value(risk: dict[str, Any], lookthrough: dict[str, Any]) -> float:
    return sum(_source_value(row) for row in _non_allocation_asset_sources(risk, lookthrough))


def _single_tradeable_position(risk: dict[str, Any]) -> dict[str, Any] | None:
    ignored = {"HUAWEI_ESOP", "ccb_receivable_loan"}
    candidates = []
    for row in risk.get("positions") or []:
        asset_id = str(row.get("asset_id") or "")
        asset_type = str(row.get("asset_type") or "").lower()
        if asset_id in ignored:
            continue
        if any(token in asset_type for token in ["manual", "cash", "liability", "esop"]):
            continue
        if _n(row.get("position_weight")) > 0:
            candidates.append(row)
    return max(candidates, key=lambda row: _n(row.get("position_weight")), default=None)


def _target_label(rule: dict[str, Any]) -> str:
    if rule.get("target_label"):
        return str(rule["target_label"])
    if rule.get("target_low") is not None and rule.get("target_high") is not None:
        return f"{_pct(rule['target_low'])}-{_pct(rule['target_high'])}"
    if rule.get("target_low") is not None:
        return f">={_pct(rule['target_low'])}"
    if rule.get("target_high") is not None:
        return f"<={_pct(rule['target_high'])}"
    return "--"


def _limit_label(rule: dict[str, Any]) -> str:
    if rule.get("limit_label"):
        return str(rule["limit_label"])
    if rule.get("hard_high") is not None:
        return f"<={_pct(rule['hard_high'])}"
    if rule.get("hard_low") is not None:
        return f">={_pct(rule['hard_low'])}"
    return "--"


def _target_amount_label(rule: dict[str, Any]) -> str:
    base = _n(rule.get("amount_base_cny"))
    if base <= 0:
        return "--"
    if rule.get("target_low") is not None and rule.get("target_high") is not None:
        return f"{_money(_n(rule['target_low']) * base)}-{_money(_n(rule['target_high']) * base)}"
    if rule.get("target_low") is not None:
        return f">={_money(_n(rule['target_low']) * base)}"
    if rule.get("target_high") is not None:
        return f"<={_money(_n(rule['target_high']) * base)}"
    return "--"


def _limit_amount_label(rule: dict[str, Any]) -> str:
    base = _n(rule.get("amount_base_cny"))
    if base <= 0:
        return "--"
    if rule.get("hard_high") is not None:
        return f"<={_money(_n(rule['hard_high']) * base)}"
    if rule.get("hard_low") is not None:
        return f">={_money(_n(rule['hard_low']) * base)}"
    return "--"


def _with_amounts(rule: dict[str, Any], amount_base_cny: float) -> dict[str, Any]:
    out = dict(rule)
    base = _n(amount_base_cny)
    current_amount = out.get("current_amount_cny")
    if current_amount is None:
        current_amount = _n(out.get("current")) * base
    out["amount_base_cny"] = base
    out["current_amount_cny"] = _n(current_amount)
    out["target_low_amount_cny"] = (
        _n(out["target_low"]) * base if out.get("target_low") is not None else None
    )
    out["target_high_amount_cny"] = (
        _n(out["target_high"]) * base if out.get("target_high") is not None else None
    )
    out["hard_low_amount_cny"] = (
        _n(out["hard_low"]) * base if out.get("hard_low") is not None else None
    )
    out["hard_high_amount_cny"] = (
        _n(out["hard_high"]) * base if out.get("hard_high") is not None else None
    )
    out["current_amount_label"] = (
        _money_signed(out["current_amount_cny"]) if out.get("signed_current_amount") else _money(out["current_amount_cny"])
    )
    out["target_amount_label"] = _target_amount_label(out)
    out["limit_amount_label"] = _limit_amount_label(out)
    return out


def _tone(current: float, rule: dict[str, Any]) -> tuple[str, str]:
    if rule.get("hard_high") is not None and current > _n(rule["hard_high"]):
        return "risk", "超上限"
    if rule.get("hard_low") is not None and current < _n(rule["hard_low"]):
        return "warn", "需补"
    if rule.get("target_low") is not None and current < _n(rule["target_low"]):
        return "info", str(rule.get("low_label") or "需增配")
    if rule.get("target_high") is not None and current > _n(rule["target_high"]):
        return "warn", "压降"
    if rule.get("bias_high_at") is not None and current >= _n(rule["bias_high_at"]):
        return "warn", "偏高"
    return "ok", "目标内"


def _rule(config: dict[str, Any]) -> dict[str, Any]:
    tone, status = _tone(_n(config.get("current")), config)
    out = dict(config)
    out["tone"] = tone
    out["status"] = config.get("status") or status
    out["target_label"] = _target_label(out)
    out["limit_label"] = _limit_label(out)
    return out


def build_portfolio_config_payload_from_inputs(
    risk: dict[str, Any],
    lookthrough: dict[str, Any],
    *,
    plan: dict[str, Any] | None = None,
    base_currency: str = "CNY",
    asset_label_lookup: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Build target allocation rules from risk and lookthrough state."""

    plan = {**DEFAULT_PLAN, **(plan or {})}
    nav = _n((risk.get("summary") or {}).get("total_net_worth_cny")) or _n(lookthrough.get("total_market_value_cny"))
    look_total = _n(lookthrough.get("total_market_value_cny")) or nav
    esop_amount = _esop_value(risk, lookthrough)
    cash_amount = _cash_short_value(risk, lookthrough)
    financing = _financing_metrics(risk, plan)
    commodity_gold_amount = _exposure_value(lookthrough, "asset_type", "黄金 / 商品", look_total)
    non_allocation_amount = _non_allocation_asset_value(risk, lookthrough)
    bond_amount = _exposure_value(lookthrough, "asset_type", ["债券", "中长期债券", "利率债", "信用债"], look_total)
    equity_model = _equity_market_model(lookthrough, asset_label_lookup)
    equity_sources = equity_model["buckets"]
    equity_amounts = equity_model["amounts"]
    market_amounts = equity_model["market_amounts"]
    public_equity_amount = _n(equity_amounts.get("total"))
    us_market_amount = _n(equity_amounts.get("us_equity"))
    cn_hk_amount = _n(equity_amounts.get("cn_hk_equity"))
    kr_other_amount = _n(equity_amounts.get("kr_other_equity"))
    other_equity_amount = _n(equity_amounts.get("other_equity"))
    us_broad_amount = _broad_us_value(lookthrough)
    overseas_stock_types = ["美股个股", "欧股个股", "韩股个股"]
    overseas_stock_amount = _exposure_value(lookthrough, "asset_type", overseas_stock_types, look_total)
    semiconductor_amount = _exposure_value(lookthrough, "theme", "半导体", look_total)
    ai_compute_amount = _exposure_value(lookthrough, "theme", "AI / 算力", look_total)
    resource_theme_amount = _exposure_value(lookthrough, "theme", "贵金属与铜", look_total)
    semiconductor = semiconductor_amount / nav if nav > 0 else 0.0
    ai_compute = ai_compute_amount / nav if nav > 0 else 0.0
    resource_theme = resource_theme_amount / nav if nav > 0 else 0.0
    a_share = _n(market_amounts.get("A股")) / nav if nav > 0 else 0.0
    h_share = _n(market_amounts.get("港股")) / nav if nav > 0 else 0.0
    single = _single_tradeable_position(risk)
    single_name = single.get("asset_name") or single.get("asset_id") if single else "单一可交易标的"
    stock_children = [
        _allocation_child("美国权益", us_market_amount, nav, rule_id="us_equity"),
        _allocation_child("中国/港股权益", cn_hk_amount, nav, rule_id="cn_hk_equity"),
        _allocation_child("韩国/欧洲/其他权益", kr_other_amount, nav, rule_id="kr_other_equity"),
    ]
    if other_equity_amount > 1:
        stock_children.append(_allocation_child("其他/待映射权益", other_equity_amount, nav, rule_id="other_equity"))
    debt_children = [
        _allocation_child_signed("当前融资", -financing["current_debt_cny"], nav, rule_id="debt_current"),
        _allocation_child("ESOP后付息压力", financing["post_annual_interest_cny"], nav, rule_id="debt_cost"),
    ]

    rules = [
        _rule({
            "id": "esop",
            "group_id": "primary_allocation",
            "name": "非上市权益",
            "current": esop_amount / nav if nav > 0 else 0.0,
            "current_amount_cny": esop_amount,
            "target_low": 0.60,
            "target_high": 0.67,
            "hard_high": 0.70,
            "low_label": "待增配",
            "detail": f"计划增配 {_money(plan['esop_add_cny'])}，其中融资 {_money(plan['esop_debt_cny'])}、现金 {_money(plan['esop_cash_cny'])}。",
            "children": _esop_children(risk, lookthrough, nav),
        }),
        _rule({
            "id": "stock",
            "group_id": "primary_allocation",
            "name": "股票",
            "current": public_equity_amount / nav if nav > 0 else 0.0,
            "current_amount_cny": public_equity_amount,
            "target_low": 0.55,
            "target_high": 0.70,
            "hard_high": 0.80,
            "detail": "第一层只按股票资产大类统计；地区、工具和具体标的在子配置里查看。",
            "children": stock_children,
        }),
        _rule({
            "id": "bond",
            "group_id": "primary_allocation",
            "name": "债券",
            "current": bond_amount / nav if nav > 0 else 0.0,
            "current_amount_cny": bond_amount,
            "target_low": 0.05,
            "target_high": 0.15,
            "hard_low": 0.00,
            "low_label": "需增配",
            "detail": "当前债券类资产不足；后续可用中短久期债券或债券基金补齐组合稳定器。",
            "children": _children_from_exposure(lookthrough, "asset_type", ["债券", "中长期债券", "利率债", "信用债"], nav),
        }),
        _rule({
            "id": "cash",
            "group_id": "primary_allocation",
            "name": "现金短债",
            "current": cash_amount / nav if nav > 0 else 0.0,
            "current_amount_cny": cash_amount,
            "target_low": 0.08,
            "hard_low": 0.05,
            "target_label": ">=8.0%",
            "limit_label": ">=5.0%",
            "detail": "只统计正向现金、货币基金和短债资产；融资负债单独在融资规则里管理。",
            "children": _cash_short_children(risk, lookthrough, nav, cash_amount),
        }),
        _rule({
            "id": "non_allocation_assets",
            "group_id": "primary_allocation",
            "name": "配置外资产",
            "current": non_allocation_amount / nav if nav > 0 else 0.0,
            "current_amount_cny": non_allocation_amount,
            "target_label": "仅披露",
            "limit_label": "不参与再平衡",
            "status": "披露",
            "detail": "黄金/商品、应收款、公积金等不进入目标配置比例，只用于解释总 NAV 与目标配置规则的差额。",
            "children": _children_from_sources(
                _non_allocation_asset_sources(risk, lookthrough),
                nav,
                total_cny=non_allocation_amount,
            ),
        }),
        _rule({
            "id": "us_equity",
            "group_id": "primary_allocation",
            "display": False,
            "name": "美国权益",
            "current": us_market_amount / nav if nav > 0 else 0.0,
            "current_amount_cny": us_market_amount,
            "target_low": 0.25,
            "target_high": 0.35,
            "hard_high": 0.45,
            "low_label": "需增配",
            "detail": "按穿透后的底层持仓和直接持仓兜底统计，不区分宽基或个股。",
            "children": _children_from_sources(equity_sources["us_equity"], nav, total_cny=us_market_amount),
        }),
        _rule({
            "id": "cn_hk_equity",
            "group_id": "primary_allocation",
            "display": False,
            "name": "中国/港股权益",
            "current": cn_hk_amount / nav if nav > 0 else 0.0,
            "current_amount_cny": cn_hk_amount,
            "target_low": 0.15,
            "target_high": 0.25,
            "hard_high": 0.35,
            "detail": f"当前按 A股 + 港股市场合计；A股 {_pct(a_share)}，港股 {_pct(h_share)}。",
            "children": _children_from_sources(equity_sources["cn_hk_equity"], nav, total_cny=cn_hk_amount),
        }),
        _rule({
            "id": "kr_other_equity",
            "group_id": "primary_allocation",
            "display": False,
            "name": "韩国/欧洲/其他权益",
            "current": kr_other_amount / nav if nav > 0 else 0.0,
            "current_amount_cny": kr_other_amount,
            "target_high": 0.10,
            "hard_high": 0.15,
            "target_label": "<=10.0%",
            "detail": "用于约束非中美市场的单一国家或地区暴露。",
            "children": _children_from_sources(equity_sources["kr_other_equity"], nav, total_cny=kr_other_amount),
        }),
        _rule({
            "id": "other_equity",
            "group_id": "primary_allocation",
            "display": False,
            "name": "其他/待映射权益",
            "current": other_equity_amount / nav if nav > 0 else 0.0,
            "current_amount_cny": other_equity_amount,
            "target_high": 0.03,
            "hard_high": 0.05,
            "target_label": "<=3.0%",
            "detail": "用于承接穿透后仍无法归入主要市场的权益资产，正常情况下应接近 0。",
            "children": _children_from_sources(equity_sources["other_equity"], nav, total_cny=other_equity_amount),
        }),
        _rule({
            "id": "us_broad",
            "group_id": "primary_allocation",
            "display": False,
            "name": "美股宽基",
            "current": us_broad_amount / nav if nav > 0 else 0.0,
            "current_amount_cny": us_broad_amount,
            "target_low": 0.20,
            "target_high": 0.25,
            "hard_high": 0.30,
            "low_label": "需增配",
            "detail": "以 S&P 500 / 全市场为主，QQQ 不作为唯一宽基入口。",
            "children": _broad_us_children(lookthrough, nav),
        }),
        _rule({
            "id": "us_stock",
            "group_id": "primary_allocation",
            "display": False,
            "name": "美股/海外个股",
            "current": overseas_stock_amount / nav if nav > 0 else 0.0,
            "current_amount_cny": overseas_stock_amount,
            "target_low": 0.15,
            "target_high": 0.25,
            "hard_high": 0.30,
            "bias_high_at": 0.23,
            "detail": "保留进攻弹性，但新增个股不得继续集中到同一主题；当前包含美股及海外单票。",
            "children": _children_from_exposure(lookthrough, "asset_type", overseas_stock_types, nav),
        }),
        _rule({
            "id": "commodity_gold",
            "group_id": "risk_constraints",
            "display": False,
            "name": "商品/黄金",
            "current": commodity_gold_amount / nav if nav > 0 else 0.0,
            "current_amount_cny": commodity_gold_amount,
            "target_low": 0.00,
            "target_high": 0.05,
            "hard_high": 0.10,
            "detail": "当前不作为第一层配置大类展示，仅作为其他资产暴露跟踪。",
            "children": _children_from_exposure(lookthrough, "asset_type", "黄金 / 商品", nav),
        }),
        _rule({
            "id": "ai_semiconductor",
            "group_id": "risk_constraints",
            "display": False,
            "name": "AI/半导体",
            "current": max(semiconductor, ai_compute),
            "current_amount_cny": max(semiconductor_amount, ai_compute_amount),
            "target_low": 0.25,
            "target_high": 0.32,
            "hard_high": 0.35,
            "bias_high_at": 0.28,
            "detail": f"当前按半导体和 AI/算力两项主题取较高值；半导体 {_pct(semiconductor)}，AI/算力 {_pct(ai_compute)}。",
            "children": _children_from_exposure(lookthrough, "theme", ["半导体", "AI / 算力"], nav),
        }),
        _rule({
            "id": "resource_theme",
            "group_id": "risk_constraints",
            "display": False,
            "name": "资源股暴露",
            "current": resource_theme,
            "current_amount_cny": resource_theme_amount,
            "target_low": 0.05,
            "target_high": 0.08,
            "hard_high": 0.10,
            "detail": "这是主题约束，不是资产大类；紫金、铜陵等资源股在这里监控集中度。",
            "children": _children_from_exposure(lookthrough, "theme", "贵金属与铜", nav),
        }),
        _rule({
            "id": "debt",
            "group_id": "primary_allocation",
            "name": "融资负债",
            "current": financing["current_debt_cny"] / nav if nav > 0 else 0.0,
            "current_amount_cny": -financing["current_debt_cny"],
            "target_high": 0.45,
            "hard_high": 0.50,
            "target_label": "<=45.0%",
            "limit_label": "<=50.0%",
            "detail": (
                f"融资利率 {_pct(financing['rate'])}；当前融资 {_money(financing['current_debt_cny'])}，"
                f"ESOP 后融资约 {_money(financing['post_debt_cny'])}，"
                f"年息约 {_money(financing['post_annual_interest_cny'])}。"
            ),
            "signed_current_amount": True,
            "children": debt_children,
        }),
        _rule({
            "id": "debt_current",
            "group_id": "primary_allocation",
            "display": False,
            "name": "当前融资",
            "current": financing["current_debt_cny"] / nav if nav > 0 else 0.0,
            "current_amount_cny": -financing["current_debt_cny"],
            "target_high": 0.45,
            "hard_high": 0.50,
            "target_label": "<=45.0%",
            "limit_label": "<=50.0%",
            "detail": "只统计负现金账户和融资余额，不进入现金短债缓冲池。",
            "signed_current_amount": True,
            "children": _financing_children(risk, nav, financing["current_debt_cny"]),
        }),
        _rule({
            "id": "debt_cost",
            "group_id": "primary_allocation",
            "display": False,
            "name": "ESOP后付息压力",
            "current": financing["post_annual_interest_cny"] / nav if nav > 0 else 0.0,
            "current_amount_cny": financing["post_annual_interest_cny"],
            "target_high": 0.015,
            "hard_high": 0.020,
            "target_label": "<=1.5%",
            "limit_label": "<=2.0%",
            "detail": f"按 {_pct(financing['rate'])} 年利率估算；ESOP 后月息约 {_money(financing['post_monthly_interest_cny'])}。",
            "children": _financing_cost_children(financing, nav),
        }),
        _rule({
            "id": "single_tradeable",
            "group_id": "risk_constraints",
            "display": False,
            "name": f"单票：{single_name}",
            "current": _n(single.get("position_weight") if single else 0),
            "current_amount_cny": _n(single.get("position_value_cny") if single else 0),
            "target_low": 0.03,
            "target_high": 0.05,
            "hard_high": 0.07,
            "detail": "不含 ESOP、现金、应收款和手工资产。超过上限后只减不加。",
            "children": _children_from_sources([
                {
                    "name": single_name,
                    "value_cny": _n(single.get("position_value_cny") if single else 0),
                }
            ], nav),
        }),
    ]
    rules = [_with_amounts(rule, nav) for rule in rules]
    visible_rules = [rule for rule in rules if rule.get("display", True)]
    drilldown_rules = [rule for rule in rules if not rule.get("display", True)]

    return {
        "as_of": risk.get("as_of") or lookthrough.get("as_of"),
        "base_currency": base_currency,
        "plan": plan,
        "inputs": build_portfolio_config_input_summary(risk, lookthrough),
        "groups": CONFIG_GROUPS,
        "rules": visible_rules,
        "drilldown_rules": drilldown_rules,
        "rebalance": _build_rebalance(rules, nav, plan),
    }


def _gap_amount(rule: dict[str, Any], nav: float, target: float) -> float:
    return max(0.0, (target - _n(rule.get("current"))) * nav)


def _over_amount(rule: dict[str, Any], nav: float, target: float) -> float:
    return max(0.0, (_n(rule.get("current")) - target) * nav)


def _build_rebalance(rules: list[dict[str, Any]], nav: float, plan: dict[str, Any]) -> list[dict[str, Any]]:
    by_id = {rule["id"]: rule for rule in rules}
    rows: list[dict[str, Any]] = []

    if _n(by_id["esop"]["current"]) < _n(by_id["esop"]["target_low"]):
        rows.append({
            "id": "do_esop",
            "title": "执行 ESOP 增配",
            "status": "待执行",
            "tone": "info",
            "text": f"计划 {_money(plan['esop_add_cny'])}，距目标下限约 {_money(_gap_amount(by_id['esop'], nav, by_id['esop']['target_low']))}",
        })
    if _n(by_id["us_broad"]["current"]) < _n(by_id["us_broad"]["target_low"]):
        rows.append({
            "id": "buy_us_broad",
            "title": "增配美股宽基",
            "status": "优先",
            "tone": "info",
            "text": f"距目标下限约 {_money(_gap_amount(by_id['us_broad'], nav, by_id['us_broad']['target_low']))}",
        })
    if _n(by_id["cash"]["current"]) < _n(by_id["cash"]["target_low"]):
        rows.append({
            "id": "fill_cash",
            "title": "补现金/短债",
            "status": "优先",
            "tone": "warn",
            "text": f"距 8% 目标约 {_money(_gap_amount(by_id['cash'], nav, by_id['cash']['target_low']))}",
        })
    financing = _financing_metrics_from_debt(_n(by_id["debt"].get("current_amount_cny")), plan)
    if financing["post_annual_interest_cny"] > 0:
        cash_amount = _n(by_id["cash"].get("current_amount_cny"))
        rows.append({
            "id": "reserve_interest",
            "title": "预留融资成本",
            "status": "约束",
            "tone": "warn" if cash_amount < financing["post_annual_interest_cny"] else "info",
            "text": (
                f"按 {_pct(financing['rate'])} 估算，当前年息 {_money(financing['current_annual_interest_cny'])}；"
                f"ESOP 后约 {_money(financing['post_annual_interest_cny'])}/年、"
                f"{_money(financing['post_monthly_interest_cny'])}/月。"
            ),
        })
    if _n(by_id["single_tradeable"]["current"]) > _n(by_id["single_tradeable"]["hard_high"]):
        rows.append({
            "id": "cut_single",
            "title": "单票降至上限内",
            "status": "生效中",
            "tone": "risk",
            "text": f"超过 7% 上限约 {_money(_over_amount(by_id['single_tradeable'], nav, by_id['single_tradeable']['hard_high']))}",
        })
    if _n(by_id["resource_theme"]["current"]) > _n(by_id["resource_theme"]["hard_high"]):
        rows.append({
            "id": "cut_resource",
            "title": "资源股暴露只减不加",
            "status": "生效中",
            "tone": "risk",
            "text": f"超过 10% 上限约 {_money(_over_amount(by_id['resource_theme'], nav, by_id['resource_theme']['hard_high']))}",
        })
    if _n(by_id["cn_hk_equity"]["current"]) > _n(by_id["cn_hk_equity"]["hard_high"]):
        rows.append({
            "id": "limit_cn_hk",
            "title": "中国/港股新增受限",
            "status": "生效中",
            "tone": "warn",
            "text": f"超过 35% 上限约 {_money(_over_amount(by_id['cn_hk_equity'], nav, by_id['cn_hk_equity']['hard_high']))}",
        })
    if _n(by_id["ai_semiconductor"]["current"]) >= _n(by_id["ai_semiconductor"]["target_low"]):
        rows.append({
            "id": "freeze_ai",
            "title": "AI/半导体不扩大",
            "status": "条件触发",
            "tone": "warn",
            "text": f"当前 {_pct(by_id['ai_semiconductor']['current'])}，高于目标下限",
        })
    return rows[:7]


def build_portfolio_config_payload(*, base_currency: str = "CNY") -> dict[str, Any]:
    """Build the /api/portfolio/config payload from current live services."""

    risk = build_risk_state()
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            lookthrough = build_lookthrough_payload(cur, base_currency=base_currency)
            asset_label_lookup = _asset_label_lookup(cur, _asset_lookup_candidates(lookthrough))
    return build_portfolio_config_payload_from_inputs(
        risk,
        lookthrough,
        base_currency=base_currency,
        asset_label_lookup=asset_label_lookup,
    )
