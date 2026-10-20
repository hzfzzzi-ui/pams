"""Event-level trade attribution for manual trade reviews.

This module deliberately computes attribution on top of trade facts. It does
not mutate trade facts and it does not implement any automated trading action.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date as Date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from services.price_history import _query_ohlcv_rows, normalize_price_instrument


TRADE_ATTRIBUTION_HORIZONS = (1, 5, 20)
EPSILON_QTY = Decimal("0.000001")
AUTO_ANNOTATION_COLUMNS = {
    "buy_reason",
    "sell_reason",
    "review_tags",
    "review_status",
    "initial_position_identity",
    "current_position_identity",
    "identity_confidence",
    "sell_rule_type",
}
AUTO_REVIEW_QUEUE_KINDS = {
    "all",
    "needs_human_review",
    "missing_price",
    "identity_drift",
    "bad",
    "danger",
    "warn",
    "trend_entry_failed",
    "early_bottom_no_validation",
    "add_after_drop_failed",
    "stop_loss_not_executed",
    "closed_stop_loss_buy",
    "risk_reduction_cost",
    "profit_take_too_early",
    "stop_loss_effective",
    "rebalance_effective",
    "sell_missed_gain",
    "sell_avoided_loss",
    "giveback",
}
IDENTITY_SELL_RULE_TYPES = {
    "allocation": "allocation_rebalance",
    "value": "value_thesis",
    "trend": "trend_exit",
    "event": "event_resolution",
    "trial": "trial_review",
    "cash_management": "cash_need",
    "uncertain": "uncertain_reclassify",
    "closed": "closed",
}


def dec(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def norm(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _date(value: Any) -> Date | None:
    if isinstance(value, Date):
        return value
    if hasattr(value, "date"):
        return value.date()
    if value:
        return Date.fromisoformat(str(value)[:10])
    return None


def _pct(change: Decimal, base: Decimal) -> Decimal | None:
    if base <= 0:
        return None
    return (change / base) * Decimal("100")


def _return_pct(price: Decimal | None, base: Decimal | None) -> Decimal | None:
    if price is None or base is None or base <= 0:
        return None
    return (price / base - Decimal("1")) * Decimal("100")


def _market_hint(row: dict[str, Any]) -> str | None:
    currency = _text(row.get("currency")).upper()
    if currency == "HKD":
        return "HK_STOCK"
    if currency == "KRW":
        return "KR_STOCK"
    if currency == "SEK":
        return "SE_STOCK"
    if currency == "USD":
        return "YF"

    raw = " ".join(
        str(row.get(key) or "")
        for key in ("market", "asset_major_category", "asset_minor_category", "portfolio_code", "broker")
    ).upper()
    if any(token in raw for token in ("CN_A", "A股", "沪", "深", "同花顺", "CN_FUND")):
        return "CN_A"
    if any(token in raw for token in ("US", "美股", "IBKR", "NASDAQ", "NYSE")):
        return "YF"
    if any(token in raw for token in ("HK", "港股")):
        return "HK_STOCK"
    if any(token in raw for token in ("KR", "韩股")):
        return "KR_STOCK"
    if any(token in raw for token in ("SE", "SEK", "SWEDEN", "SVERIGE", "STOCKHOLM", "欧股", "瑞典")):
        return "SE_STOCK"
    return row.get("market") or None


def _side_normalized(row: dict[str, Any]) -> str:
    trade_type = str(row.get("trade_type") or "").lower()
    side = str(row.get("side") or "").lower()
    if trade_type in {"buy", "fund_subscription"}:
        return "buy"
    if trade_type in {"sell", "fund_redemption"}:
        return "sell"
    if side in {"buy", "bought"}:
        return "buy"
    if side in {"sell", "sold"}:
        return "sell"
    return side or trade_type or "unknown"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _symbol_digits(value: Any) -> str:
    return "".join(ch for ch in _text(value) if ch.isdigit())


def _name_needs_enrichment(symbol: Any, name: Any) -> bool:
    text = _text(name)
    if not text:
        return True
    symbol_text = _text(symbol)
    if symbol_text and text.casefold() == symbol_text.casefold():
        return True
    digits = _symbol_digits(symbol) or _symbol_digits(text)
    if len(digits) == 6:
        exchange = "SH" if digits.startswith("6") else "SZ"
        raw = text.upper()
        code_like = {
            digits,
            f"{exchange}{digits}",
            f"{digits}.{exchange}",
            f"{digits}.{exchange.lower()}",
            f"SSE:{digits}" if exchange == "SH" else f"SZSE:{digits}",
            f"SHSE:{digits}" if exchange == "SH" else f"SZSE:{digits}",
        }
        if raw in {item.upper() for item in code_like}:
            return True
    return any(token in text for token in ("�", "Ã", "Â"))


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
                f"SHSE:{digits}" if exchange == "SH" else f"SZSE:{digits}",
            }
        )
    return {key.casefold() for key in keys if key}


def _put_asset_name(lookup: dict[str, str], symbol: Any, name: Any) -> None:
    label = _text(name)
    if not label or _name_needs_enrichment(symbol, label):
        return
    for key in _symbol_name_keys(symbol):
        lookup.setdefault(key, label)


def _table_exists(cur, table_name: str) -> bool:
    cur.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = %s
        ) AS exists
        """,
        (table_name,),
    )
    row = cur.fetchone()
    return bool(row and row.get("exists"))


def _asset_name_lookup(cur, symbols: set[str]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    query_symbols = sorted(set().union(*(_symbol_name_keys(symbol) for symbol in symbols)))
    digits = sorted({_symbol_digits(symbol) for symbol in symbols if len(_symbol_digits(symbol)) == 6})
    if not query_symbols and not digits:
        return lookup

    if _table_exists(cur, "pams_asset_master"):
        cur.execute(
            """
            SELECT primary_symbol AS symbol, COALESCE(NULLIF(display_name, ''), canonical_name) AS name
            FROM pams_asset_master
            WHERE COALESCE(primary_symbol, '') <> ''
              AND COALESCE(COALESCE(NULLIF(display_name, ''), canonical_name), '') <> ''
              AND (
                LOWER(primary_symbol) = ANY(%s::text[])
                OR regexp_replace(COALESCE(primary_symbol, ''), '[^0-9]', '', 'g') = ANY(%s::text[])
              )
            ORDER BY confidence DESC NULLS LAST, updated_at DESC NULLS LAST
            """,
            (query_symbols, digits),
        )
        for row in cur.fetchall():
            _put_asset_name(lookup, row.get("symbol"), row.get("name"))

    if _table_exists(cur, "pams_asset_alias") and _table_exists(cur, "pams_asset_master"):
        cur.execute(
            """
            SELECT al.alias_value AS symbol, COALESCE(NULLIF(am.display_name, ''), am.canonical_name) AS name
            FROM pams_asset_alias al
            JOIN pams_asset_master am ON am.asset_id = al.asset_id
            WHERE COALESCE(al.alias_value, '') <> ''
              AND COALESCE(COALESCE(NULLIF(am.display_name, ''), am.canonical_name), '') <> ''
              AND (
                LOWER(al.alias_value) = ANY(%s::text[])
                OR regexp_replace(COALESCE(al.alias_value, ''), '[^0-9]', '', 'g') = ANY(%s::text[])
              )
            ORDER BY al.priority DESC NULLS LAST, al.created_at DESC NULLS LAST
            """,
            (query_symbols, digits),
        )
        for row in cur.fetchall():
            _put_asset_name(lookup, row.get("symbol"), row.get("name"))

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

    return lookup


def _enrich_trade_names(cur, rows: list[dict[str, Any]]) -> None:
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
            label = lookup.get(key)
            if label:
                row["name"] = label
                break


def _asset_identity_keys(row: dict[str, Any]) -> set[str]:
    keys = {_text(row.get("asset_id")), _text(row.get("symbol")), _text(row.get("name"))}
    return {key.casefold() for key in keys if key}


def _put_asset_identity(lookup: dict[str, dict[str, Any]], row: dict[str, Any]) -> None:
    asset_id = _text(row.get("asset_id"))
    if not asset_id:
        return
    identity = {
        "asset_id": asset_id,
        "symbol": _text(row.get("primary_symbol")),
        "name": _text(row.get("name")),
        "market": _text(row.get("market")),
        "currency": _text(row.get("currency")),
    }
    for key in (
        _text(row.get("asset_id")),
        _text(row.get("primary_symbol")),
        _text(row.get("canonical_name")),
        _text(row.get("display_name")),
        _text(row.get("alias_value")),
    ):
        clean = key.casefold()
        if clean:
            lookup.setdefault(clean, identity)


def _asset_identity_lookup(cur, rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    keys = sorted(set().union(*(_asset_identity_keys(row) for row in rows)))
    if not keys or not _table_exists(cur, "pams_asset_master"):
        return lookup

    cur.execute(
        """
        SELECT
            asset_id,
            primary_symbol,
            canonical_name,
            display_name,
            COALESCE(NULLIF(display_name, ''), canonical_name) AS name,
            market,
            currency
        FROM pams_asset_master
        WHERE LOWER(asset_id) = ANY(%s::text[])
           OR LOWER(primary_symbol) = ANY(%s::text[])
           OR LOWER(canonical_name) = ANY(%s::text[])
           OR LOWER(display_name) = ANY(%s::text[])
        ORDER BY confidence DESC NULLS LAST, updated_at DESC NULLS LAST
        """,
        (keys, keys, keys, keys),
    )
    for row in cur.fetchall():
        _put_asset_identity(lookup, row)

    if _table_exists(cur, "pams_asset_alias"):
        cur.execute(
            """
            SELECT
                am.asset_id,
                am.primary_symbol,
                am.canonical_name,
                am.display_name,
                COALESCE(NULLIF(am.display_name, ''), am.canonical_name) AS name,
                am.market,
                am.currency,
                al.alias_value
            FROM pams_asset_alias al
            JOIN pams_asset_master am ON am.asset_id = al.asset_id
            WHERE LOWER(al.alias_value) = ANY(%s::text[])
            ORDER BY al.priority ASC NULLS LAST, al.created_at DESC NULLS LAST
            """,
            (keys,),
        )
        for row in cur.fetchall():
            _put_asset_identity(lookup, row)

    return lookup


def _enrich_trade_asset_identity(cur, rows: list[dict[str, Any]]) -> None:
    candidates = [row for row in rows if not _text(row.get("asset_id")) or not _text(row.get("symbol"))]
    if not candidates:
        return
    lookup = _asset_identity_lookup(cur, candidates)
    for row in candidates:
        identity = None
        for key in _asset_identity_keys(row):
            identity = lookup.get(key)
            if identity:
                break
        if not identity:
            continue
        row.setdefault("asset_id", identity.get("asset_id"))
        if not _text(row.get("asset_id")):
            row["asset_id"] = identity.get("asset_id")
        if not _text(row.get("symbol")) and identity.get("symbol"):
            row["symbol"] = identity["symbol"]
        if not _text(row.get("name")) and identity.get("name"):
            row["name"] = identity["name"]
        if not _text(row.get("market")) and identity.get("market"):
            row["market"] = identity["market"]
        if not _text(row.get("currency")) and identity.get("currency"):
            row["currency"] = identity["currency"]


def _asset_key(row: dict[str, Any]) -> str:
    return (_text(row.get("symbol")) or _text(row.get("name"))).lower()


def _lifecycle_group_key(row: dict[str, Any]) -> tuple[str, str]:
    return (_text(row.get("account_code")).lower(), _asset_key(row))


def _lifecycle_sort_key(row: dict[str, Any]) -> tuple[str, int]:
    return (str(row.get("trade_time") or row.get("trade_date") or ""), int(dec(row.get("trade_id"))))


def _annotate_trade_lifecycle(rows: list[dict[str, Any]]) -> None:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if _asset_key(row):
            grouped[_lifecycle_group_key(row)].append(row)

    for group_rows in grouped.values():
        lots: list[dict[str, Any]] = []
        for row in sorted(group_rows, key=_lifecycle_sort_key):
            side = _side_normalized(row)
            quantity = abs(dec(row.get("quantity")))
            if side == "buy":
                if quantity <= EPSILON_QTY:
                    row["position_lifecycle_status"] = "unknown"
                    continue
                row["position_lifecycle_status"] = "open"
                lots.append(
                    {
                        "row": row,
                        "original": quantity,
                        "remaining": quantity,
                        "closed_quantity": Decimal("0"),
                        "close_value": Decimal("0"),
                        "close_dates": [],
                        "close_reasons": [],
                    }
                )
            elif side == "sell":
                if quantity <= EPSILON_QTY:
                    row["position_lifecycle_status"] = "unknown"
                    continue
                remaining_sell = quantity
                sell_price = dec(row.get("price"))
                sell_date = _date(row.get("trade_date"))
                sell_reason = _text(row.get("sell_reason"))
                for lot in lots:
                    if remaining_sell <= EPSILON_QTY:
                        break
                    if lot["remaining"] <= EPSILON_QTY:
                        continue
                    matched = min(lot["remaining"], remaining_sell)
                    lot["remaining"] -= matched
                    lot["closed_quantity"] += matched
                    if sell_price > 0:
                        lot["close_value"] += matched * sell_price
                    if sell_date:
                        lot["close_dates"].append(sell_date)
                    if sell_reason:
                        lot["close_reasons"].append(sell_reason)
                    remaining_sell -= matched
                open_after = sum((lot["remaining"] for lot in lots), Decimal("0"))
                row["position_lifecycle_status"] = "closed" if open_after <= EPSILON_QTY else "reduced"

        for lot in lots:
            row = lot["row"]
            remaining = lot["remaining"]
            if remaining <= EPSILON_QTY:
                row["position_lifecycle_status"] = "closed"
            elif remaining < lot["original"] - EPSILON_QTY:
                row["position_lifecycle_status"] = "partially_closed"
            else:
                row["position_lifecycle_status"] = "open"
            if lot["closed_quantity"] > EPSILON_QTY:
                if lot["close_dates"]:
                    row["lifecycle_close_date"] = max(lot["close_dates"])
                if lot["close_value"] > 0:
                    row["lifecycle_close_price"] = lot["close_value"] / lot["closed_quantity"]
                if lot["close_reasons"]:
                    row["lifecycle_close_reason"] = _mode(lot["close_reasons"])


def _is_cash_like(row: dict[str, Any]) -> bool:
    text = " ".join(
        _text(row.get(key))
        for key in ("name", "symbol", "asset_major_category", "asset_minor_category")
    ).lower()
    return any(token in text for token in ("现金", "活期", "cash", "money market"))


def _mode(values: list[str]) -> str | None:
    counts: dict[str, int] = defaultdict(int)
    for value in values:
        clean = _text(value)
        if clean:
            counts[clean] += 1
    if not counts:
        return None
    return sorted(counts.items(), key=lambda item: (item[1], item[0]), reverse=True)[0][0]


def _clean_price_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    clean = []
    for row in rows:
        day = _date(row.get("trade_date") or row.get("date"))
        close = dec(row.get("close"))
        if day and close > 0:
            clean.append({"date": day, "close": close})
    clean.sort(key=lambda item: item["date"])
    dedup: dict[Date, dict[str, Any]] = {}
    for item in clean:
        dedup[item["date"]] = item
    return [dedup[day] for day in sorted(dedup)]


def _base_index(rows: list[dict[str, Any]], trade_date: Date) -> int | None:
    for idx, row in enumerate(rows):
        if row["date"] >= trade_date:
            return idx
    return None


def _horizon_item(rows: list[dict[str, Any]], base_idx: int, days: int) -> dict[str, Any] | None:
    idx = base_idx + days
    if idx < len(rows):
        return rows[idx]
    return None


def _price_performance_metrics(
    rows: list[dict[str, Any]],
    *,
    trade_date: Date,
    trade_price: Decimal | None = None,
    exit_date: Date | None = None,
    exit_price: Decimal | None = None,
    attribution_end_reason: str | None = None,
) -> dict[str, Any]:
    clean = _clean_price_rows(rows)
    if exit_date:
        clean = [row for row in clean if row["date"] <= exit_date]
        if exit_price and exit_price > 0:
            clean = [row for row in clean if row["date"] != exit_date]
            clean.append({"date": exit_date, "close": exit_price})
            clean.sort(key=lambda item: item["date"])
    idx = _base_index(clean, trade_date)
    if idx is None:
        return {
            "coverage": "missing",
            "warnings": ["缺少交易日之后的价格序列，无法自动归因。"],
            "horizons": {},
        }
    base_row = clean[idx]
    base_price = trade_price if trade_price and trade_price > 0 else base_row["close"]
    tail = clean[idx:]
    returns = [_return_pct(item["close"], base_price) for item in tail]
    numeric_returns = [item for item in returns if item is not None]
    current_row = tail[-1] if tail else None
    current_return = _return_pct(current_row["close"], base_price) if current_row else None
    horizons: dict[str, Any] = {}
    for days in TRADE_ATTRIBUTION_HORIZONS:
        item = _horizon_item(clean, idx, days)
        horizons[f"t{days}"] = {
            "date": item["date"] if item else None,
            "price": item["close"] if item else None,
            "return_pct": _return_pct(item["close"], base_price) if item else None,
        }
    return {
        "coverage": "ok",
        "base_date": base_row["date"],
        "base_price": base_price,
        "price_source_date": base_row["date"],
        "latest_date": current_row["date"] if current_row else None,
        "latest_price": current_row["close"] if current_row else None,
        "attribution_end_date": current_row["date"] if current_row else None,
        "attribution_end_price": current_row["close"] if current_row else None,
        "attribution_end_reason": attribution_end_reason or "as_of",
        "current_return_pct": current_return,
        "max_adverse_excursion_pct": min(numeric_returns) if numeric_returns else None,
        "max_favorable_excursion_pct": max(numeric_returns) if numeric_returns else None,
        "horizons": horizons,
        "points": len(tail),
        "warnings": [] if len(tail) >= 2 else ["价格点不足，归因只可作参考。"],
    }


def _trade_query(where_sql: str = "") -> str:
    return f"""
        SELECT
            f.trade_id,
            f.trade_date,
            COALESCE(f.trade_time, f.trade_date::timestamp) AS trade_time,
            f.account_code,
            COALESCE(a.display_name, f.account_code) AS account_name,
            f.broker,
            f.portfolio_code,
            f.asset_id,
            f.symbol,
            f.name,
            f.market,
            f.asset_major_category,
            f.asset_minor_category,
            f.currency,
            f.trade_type,
            f.side,
            f.quantity,
            f.price,
            f.gross_amount,
            f.fee_amount,
            f.tax_amount,
            f.net_cash,
            f.fx_rate_to_cny,
            f.net_cash_cny,
            f.realized_pnl,
            f.realized_pnl_cny,
            f.buy_reason,
            f.sell_reason,
            COALESCE(f.review_tags, ARRAY[]::text[]) AS review_tags,
            f.trade_intent,
            f.trade_plan,
            f.review_note,
            f.review_due_date,
            f.initial_position_identity,
            f.current_position_identity,
            f.identity_confidence,
            f.sell_rule_type,
            CASE
                WHEN f.review_status IN ('auto', 'needs_review', 'reviewed', 'ignored') THEN f.review_status
                ELSE 'auto'
            END AS review_status
        FROM pams_investment_trade_fact f
        LEFT JOIN pams_accounts a ON a.account_code = f.account_code
        {where_sql}
    """


def _fetch_trade(cur, trade_id: int, *, enrich_name: bool = True) -> dict[str, Any] | None:
    cur.execute(_trade_query("WHERE f.trade_id = %s"), (trade_id,))
    row = cur.fetchone()
    if not row:
        return None
    rows = [dict(row)]
    if enrich_name:
        _enrich_trade_names(cur, rows)
    _enrich_trade_asset_identity(cur, rows)
    return rows[0]


def _fetch_price_rows(cur, row: dict[str, Any], *, end: Date) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    trade_date = _date(row.get("trade_date"))
    symbol = str(row.get("symbol") or "").strip()
    if not trade_date or not symbol:
        return [], {"coverage": "missing", "warnings": ["交易缺少日期或标的代码，无法自动归因。"]}
    try:
        instrument = normalize_price_instrument(
            symbol,
            market=_market_hint(row),
            label=row.get("name") or symbol,
        )
    except ValueError as exc:
        return [], {"coverage": "missing", "warnings": [str(exc)]}
    start = trade_date - timedelta(days=7)
    rows = _query_ohlcv_rows(cur, instrument, start, end)
    return rows, {
        "requested_symbol": instrument.requested_symbol,
        "symbol": instrument.symbol,
        "query_symbol": instrument.query_symbol,
        "market": instrument.market,
        "label": instrument.label,
        "source": instrument.source,
    }


def _judgements(row: dict[str, Any], metrics: dict[str, Any]) -> list[dict[str, str]]:
    if metrics.get("coverage") != "ok":
        return [{"level": "muted", "label": "数据不足", "message": "价格覆盖不足，暂不判断交易表现。"}]
    side = _side_normalized(row)
    current = metrics.get("current_return_pct")
    mae = metrics.get("max_adverse_excursion_pct")
    t20 = (metrics.get("horizons") or {}).get("t20", {}).get("return_pct")
    out = []
    if side == "buy":
        if current is not None and current > Decimal("5"):
            out.append({"level": "good", "label": "买入有效", "message": "当前价格较交易基准上涨超过 5%。"})
        elif current is not None and current < Decimal("-5"):
            out.append({"level": "bad", "label": "买入承压", "message": "当前价格较交易基准下跌超过 5%。"})
        if mae is not None and mae < Decimal("-8"):
            out.append({"level": "warn", "label": "入场回撤较大", "message": "买入后最大浮亏超过 8%。"})
        if t20 is not None and t20 < Decimal("0"):
            out.append({"level": "warn", "label": "T+20 未验证", "message": "T+20 后仍未取得正收益。"})
    elif side == "sell":
        if current is not None and current > Decimal("5"):
            out.append({"level": "bad", "label": "卖飞", "message": "卖出后标的继续上涨超过 5%。"})
        elif current is not None and current < Decimal("-5"):
            out.append({"level": "good", "label": "避亏", "message": "卖出后标的继续下跌超过 5%。"})
        else:
            out.append({"level": "neutral", "label": "卖出后平稳", "message": "卖出后价格变化不大。"})
    if not out:
        out.append({"level": "neutral", "label": "观察中", "message": "交易后表现尚未形成明显结论。"})
    return out


def _metric_decimal(metrics: dict[str, Any], key: str) -> Decimal | None:
    value = metrics.get(key)
    if value in (None, ""):
        return None
    return dec(value)


def _horizon_return(metrics: dict[str, Any], key: str) -> Decimal | None:
    item = (metrics.get("horizons") or {}).get(key) or {}
    value = item.get("return_pct")
    if value in (None, ""):
        return None
    return dec(value)


def _fmt_pct(value: Decimal | None) -> str:
    if value is None:
        return "--"
    rounded = value.quantize(Decimal("0.01"))
    sign = "+" if rounded > 0 else ""
    return f"{sign}{rounded}%"


def _confidence(metrics: dict[str, Any], current: Decimal | None) -> str:
    points = int(dec(metrics.get("points")))
    if points >= 20 and current is not None and abs(current) >= Decimal("5"):
        return "high"
    if points >= 5:
        return "medium"
    return "low"


def _manual_gaps(row: dict[str, Any], side: str) -> list[str]:
    gaps: list[str] = []
    if side == "buy" and not row.get("buy_reason"):
        gaps.append("买入主原因未填")
    if side == "sell" and not row.get("sell_reason"):
        gaps.append("卖出主原因未填")
    if side == "buy" and not (row.get("initial_position_identity") or row.get("current_position_identity")):
        gaps.append("仓位身份未填")
    if not row.get("review_tags"):
        gaps.append("事后复盘标签未填")
    return gaps


def _dedup_first_three(values: list[str]) -> list[str]:
    seen = set()
    out = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out[:3]


def _has_review_tag(row: dict[str, Any], *tags: str) -> bool:
    values = {_text(tag) for tag in row.get("review_tags") or []}
    return any(tag in values for tag in tags)


def _buy_context(row: dict[str, Any], initial_identity: str, current_identity: str) -> str:
    reason = _text(row.get("buy_reason"))
    identities = {initial_identity, current_identity}
    if reason == "trend_signal" or "trend" in identities:
        return "trend"
    if reason in {"fundamental_valuation", "event_driven"} or identities & {"value", "event"}:
        return "value"
    if reason in {"allocation_adjustment", "plan_execution", "planned_dca"} or _has_review_tag(
        row,
        "insufficient_batch",
        "single_trade_too_heavy",
    ):
        return "add"
    if reason == "trial_observation" or "trial" in identities:
        return "trial"
    return "generic"


def _sell_context(row: dict[str, Any]) -> str:
    reason = _text(row.get("sell_reason"))
    if reason in {"risk_control_stop_loss", "trend_signal_exit", "thesis_invalidated"}:
        return "risk_exit"
    if reason == "risk_reduction_rebalance":
        return "rebalance"
    if reason == "planned_take_profit":
        return "take_profit"
    if reason in {"liquidity_need", "correction"}:
        return "non_investment"
    return "generic"


def _closed_by_risk_exit(row: dict[str, Any]) -> bool:
    return _text(row.get("lifecycle_close_reason")) in {
        "risk_control_stop_loss",
        "trend_signal_exit",
        "thesis_invalidated",
    }


def _low_validation(mfe: Decimal | None) -> bool:
    return mfe is None or mfe < Decimal("3")


def _weak_trend(*returns: Decimal | None) -> bool:
    values = [value for value in returns if value is not None]
    return bool(values) and all(value <= Decimal("0") for value in values)


def build_auto_trade_review(
    row: dict[str, Any],
    metrics: dict[str, Any],
    *,
    opportunity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Derive a non-mutating system review conclusion for one trade event."""
    side = _side_normalized(row)
    coverage = metrics.get("coverage")
    if side not in {"buy", "sell"}:
        return {
            "kind": "not_applicable",
            "level": "muted",
            "label": "不参与自动归因",
            "summary": "非买入 / 卖出交易暂不做价格事件归因。",
            "confidence": "low",
            "needs_human_review": False,
            "queue_kinds": [],
            "manual_gaps": [],
            "suggested_tags": [],
            "evidence": [],
            "action": "无需复盘",
        }
    if coverage != "ok":
        warnings = metrics.get("warnings") or []
        return {
            "kind": "missing_price",
            "level": "muted",
            "label": "等待价格数据",
            "summary": warnings[0] if warnings else "价格覆盖不足，暂不做自动归因。",
            "confidence": "low",
            "needs_human_review": True,
            "queue_kinds": ["missing_price", "needs_human_review"],
            "manual_gaps": _manual_gaps(row, side),
            "suggested_tags": ["data_issue"],
            "evidence": warnings[:3],
            "action": "先补价格或确认标的映射",
        }

    current = _metric_decimal(metrics, "current_return_pct")
    mae = _metric_decimal(metrics, "max_adverse_excursion_pct")
    mfe = _metric_decimal(metrics, "max_favorable_excursion_pct")
    t1 = _horizon_return(metrics, "t1")
    t5 = _horizon_return(metrics, "t5")
    t20 = _horizon_return(metrics, "t20")
    initial_identity = str(row.get("initial_position_identity") or "")
    current_identity = str(row.get("current_position_identity") or "")
    identity_drift = bool(
        initial_identity
        and current_identity
        and current_identity != "closed"
        and initial_identity != current_identity
    )
    trend_to_value = initial_identity == "trend" and current_identity == "value"
    lifecycle_status = str(row.get("position_lifecycle_status") or "")
    is_closed_buy = side == "buy" and lifecycle_status == "closed"
    buy_context = _buy_context(row, initial_identity, current_identity)
    sell_context = _sell_context(row)
    manual_gaps = _manual_gaps(row, side)
    evidence = [
        f"当前相对交易基准 {_fmt_pct(current)}",
        f"最大浮亏 {_fmt_pct(mae)}，最大浮盈 {_fmt_pct(mfe)}",
        f"T+1 {_fmt_pct(t1)} / T+5 {_fmt_pct(t5)} / T+20 {_fmt_pct(t20)}",
    ]
    level = "neutral"
    kind = "watching"
    label = "观察中"
    summary = "价格表现尚未形成强结论。"
    suggested_tags: list[str] = []
    queue_kinds: list[str] = []
    needs_human_review = bool(manual_gaps)

    if is_closed_buy:
        evidence = ["仓位已清仓", *evidence]

    giveback_signal = (
        mfe is not None
        and current is not None
        and mfe >= Decimal("10")
        and mfe - current >= Decimal("10")
        and current <= Decimal("8")
    )
    severe_loss = current is not None and current <= Decimal("-10")
    moderate_loss = current is not None and current <= Decimal("-5")
    closed_risk_exit = is_closed_buy and _closed_by_risk_exit(row)
    stop_loss_signal = (
        current is not None
        and current <= Decimal("-8")
        and (mae is None or mae <= Decimal("-8"))
        and (
            _has_review_tag(row, "ignored_stop_loss", "delayed_stop_loss")
            or buy_context == "trend"
            or _weak_trend(t5, t20)
        )
    )
    closed_prefix = "该买入已清仓；" if is_closed_buy else ""

    if trend_to_value:
        level = "danger"
        kind = "identity_drift"
        label = "高风险身份漂移"
        summary = "趋势仓改成价值仓，需要确认不是亏损后不愿止损。"
        suggested_tags = ["identity_drift", "loss_rationalization"]
        queue_kinds = ["identity_drift", "danger", "needs_human_review"]
        needs_human_review = True
    elif identity_drift:
        level = "warn"
        kind = "identity_drift"
        label = "仓位身份发生变化"
        summary = "身份变化需要核对触发原因、证据、新卖出规则和仓位上限。"
        suggested_tags = ["identity_change_reasonable"]
        queue_kinds = ["identity_drift", "warn", "needs_human_review"]
        needs_human_review = True
    elif side == "buy":
        if giveback_signal and not _low_validation(mfe):
            level = "warn"
            kind = "giveback"
            label = "盈利回吐"
            summary = f"{closed_prefix}买入后曾有较大浮盈，但后续回吐明显，重点复盘止盈或移动止盈纪律。"
            suggested_tags = ["ignored_take_profit", "right_reason_poor_execution"]
            queue_kinds = ["giveback", "warn", "needs_human_review"]
            needs_human_review = True
        elif closed_risk_exit and moderate_loss:
            level = "bad"
            kind = "closed_stop_loss_buy"
            label = "亏损已止损"
            summary = f"{closed_prefix}按清仓价计算为亏损，止损已执行；重点复盘入场信号是否有效。"
            suggested_tags = ["timely_stop_loss", "thesis_wrong", "right_reason_poor_execution"]
            queue_kinds = ["bad", "needs_human_review"]
            needs_human_review = True
        elif severe_loss and _low_validation(mfe):
            level = "danger"
            if buy_context == "trend":
                kind = "trend_entry_failed"
                label = "趋势买入失效"
                summary = f"{closed_prefix}买入后没有延续趋势，也没有形成有效浮盈，重点复盘入场确认和止损执行。"
                suggested_tags = ["chase_high", "ignored_stop_loss", "thesis_wrong"]
            elif buy_context == "add":
                kind = "add_after_drop_failed"
                label = "补仓未验证"
                summary = f"{closed_prefix}买入后没有摊出有效反弹，像是越跌越买或分批节奏失效。"
                suggested_tags = ["insufficient_batch", "ignored_stop_loss", "right_reason_poor_execution"]
            elif buy_context in {"value", "trial"} or _has_review_tag(row, "early_bottom"):
                kind = "early_bottom_no_validation"
                label = "抄底未验证"
                summary = f"{closed_prefix}买入后没有形成有效浮盈，估值/低位判断还没有被价格验证。"
                suggested_tags = ["early_bottom", "delayed_stop_loss", "thesis_wrong"]
            else:
                kind = "failed_buy"
                label = "买入后从未验证"
                summary = f"{closed_prefix}买入后没有形成有效浮盈，且当前回撤较深。"
                suggested_tags = ["early_bottom", "delayed_stop_loss", "thesis_wrong"]
            queue_kinds = ["bad", "danger", "needs_human_review"]
            needs_human_review = True
        elif stop_loss_signal:
            level = "danger"
            kind = "stop_loss_not_executed"
            label = "止损未执行"
            summary = f"{closed_prefix}买入后回撤已经明显扩大，重点复盘是否按原计划止损或降仓。"
            suggested_tags = ["ignored_stop_loss", "delayed_stop_loss", "bad_discipline"]
            queue_kinds = ["bad", "danger", "needs_human_review"]
            needs_human_review = True
        elif moderate_loss:
            level = "bad"
            if buy_context == "trend" and _weak_trend(t5, t20):
                kind = "trend_entry_failed"
                label = "趋势买入失效"
                summary = f"{closed_prefix}买入后中短期趋势没有继续确认，当前已经低于交易基准。"
                suggested_tags = ["chase_high", "ignored_stop_loss", "right_reason_poor_execution"]
            elif buy_context == "add":
                kind = "add_after_drop_failed"
                label = "补仓未验证"
                summary = f"{closed_prefix}补仓后仍低于交易基准，需要复核批次、仓位和补仓触发条件。"
                suggested_tags = ["insufficient_batch", "ignored_stop_loss", "right_reason_poor_execution"]
            elif buy_context in {"value", "trial"} or _has_review_tag(row, "early_bottom"):
                kind = "early_bottom_no_validation"
                label = "抄底未验证"
                summary = f"{closed_prefix}买入后仍低于交易基准，需要复核估值判断是否过早。"
                suggested_tags = ["early_bottom", "ignored_stop_loss", "right_reason_poor_execution"]
            else:
                kind = "buy_under_pressure"
                label = "买入承压"
                summary = f"{closed_prefix}当前价格显著低于交易基准，需要复核买入理由和止损纪律。"
                suggested_tags = ["early_bottom", "ignored_stop_loss", "right_reason_poor_execution"]
            queue_kinds = ["bad", "needs_human_review"]
            needs_human_review = True
        elif current is not None and current >= Decimal("5") and (mae is None or mae > Decimal("-8")):
            level = "good"
            kind = "effective_buy"
            label = "买入有效"
            summary = f"{closed_prefix}买入后收益为正，且期间最大浮亏可控。"
            suggested_tags = ["thesis_correct", "plan_followed"]
        elif current is not None and current >= Decimal("0"):
            level = "neutral"
            kind = "early_positive"
            label = "初步有效"
            summary = f"{closed_prefix}买入后暂为正收益，但样本或波动还不足以下强结论。"
            suggested_tags = ["thesis_correct"]
        elif manual_gaps:
            suggested_tags = ["data_issue"]
        elif is_closed_buy:
            kind = "closed_buy"
            label = "已清仓买入"
            summary = "该买入已清仓，价格表现暂未形成强归因；闭环结果需要和后续卖出一起看。"
    elif side == "sell":
        if current is not None and current >= Decimal("8"):
            if sell_context == "rebalance":
                level = "warn"
                kind = "risk_reduction_cost"
                label = "降风险机会成本"
                summary = "卖出后继续上涨，但若当时目的是降风险，应作为机会成本复盘，不直接判定卖错。"
                suggested_tags = ["correct_rebalance", "right_result_weak_reason"]
                queue_kinds = ["risk_reduction_cost", "warn", "needs_human_review"]
                needs_human_review = True
            elif sell_context == "take_profit":
                level = "bad"
                kind = "profit_take_too_early"
                label = "止盈过早"
                summary = "卖出后标的继续明显上涨，重点复核止盈规则是否过早落袋。"
                suggested_tags = ["sold_too_early", "profit_take_too_early"]
                queue_kinds = ["profit_take_too_early", "bad", "needs_human_review"]
                needs_human_review = True
            else:
                level = "bad"
                kind = "sell_missed_gain"
                label = "卖出后继续上涨"
                summary = "卖出后标的继续明显上涨，优先复核是否卖早。"
                suggested_tags = ["sold_too_early", "profit_take_too_early"]
                queue_kinds = ["sell_missed_gain", "bad", "needs_human_review"]
                needs_human_review = True
        elif current is not None and current >= Decimal("3"):
            level = "warn"
            if sell_context == "rebalance":
                kind = "risk_reduction_cost"
                label = "降风险机会成本"
                summary = "卖出后仍有上涨，但幅度暂不严重；优先复核当时风险预算是否合理。"
                suggested_tags = ["correct_rebalance"]
                queue_kinds = ["risk_reduction_cost", "warn"]
            elif sell_context == "take_profit":
                kind = "profit_take_too_early"
                label = "止盈略早"
                summary = "卖出后仍有上涨，暂不判定严重卖飞，但需要复核止盈节奏。"
                suggested_tags = ["sold_too_early"]
                queue_kinds = ["profit_take_too_early", "warn"]
            else:
                kind = "sell_mild_missed_gain"
                label = "卖出后小幅上涨"
                summary = "卖出后仍有上涨，暂不判定严重卖飞。"
                suggested_tags = ["sold_too_early"]
                queue_kinds = ["sell_missed_gain", "warn"]
        elif current is not None and current <= Decimal("-8"):
            level = "good"
            if sell_context == "risk_exit":
                kind = "stop_loss_effective"
                label = "退出纪律有效"
                summary = "卖出后标的继续下跌，止损、趋势退出或逻辑证伪执行有效。"
                suggested_tags = ["timely_stop_loss", "plan_followed"]
                queue_kinds = ["stop_loss_effective", "sell_avoided_loss"]
            elif sell_context == "rebalance":
                kind = "rebalance_effective"
                label = "降风险有效"
                summary = "卖出后标的继续下跌，降风险或再平衡结果有效。"
                suggested_tags = ["correct_rebalance", "plan_followed"]
                queue_kinds = ["rebalance_effective", "sell_avoided_loss"]
            else:
                kind = "sell_avoided_loss"
                label = "卖出避亏有效"
                summary = "卖出后标的继续下跌，交易避免了后续损失。"
                suggested_tags = ["timely_stop_loss", "plan_followed"]
                queue_kinds = ["sell_avoided_loss"]
        elif current is not None and current <= Decimal("-3"):
            level = "good"
            if sell_context == "rebalance":
                kind = "rebalance_effective"
                label = "降风险有效"
                summary = "卖出后价格走弱，降风险或再平衡暂时有效。"
                suggested_tags = ["correct_rebalance"]
                queue_kinds = ["rebalance_effective", "sell_avoided_loss"]
            else:
                kind = "sell_mild_avoided_loss"
                label = "卖出后小幅下跌"
                summary = "卖出后价格走弱，退出结果暂时有效。"
                suggested_tags = ["plan_followed"]
                queue_kinds = ["sell_avoided_loss"]
        else:
            label = "卖出后平稳"
            summary = "卖出后价格变化不大，结果暂时中性。"
            suggested_tags = ["plan_followed"] if not manual_gaps else []

    if manual_gaps and "needs_human_review" not in queue_kinds:
        queue_kinds.append("needs_human_review")
    if level in {"bad", "danger", "warn"} and level not in queue_kinds:
        queue_kinds.append(level)
    action = "可接受系统标签并补主原因"
    if level == "good" and not manual_gaps:
        action = "可保留系统结论"
    elif kind == "missing_price":
        action = "补价格后再复盘"
    elif needs_human_review:
        action = "建议人工确认并补标签"
    return {
        "kind": kind,
        "level": level,
        "label": label,
        "summary": summary,
        "confidence": _confidence(metrics, current),
        "needs_human_review": needs_human_review,
        "queue_kinds": _dedup_first_three(queue_kinds),
        "manual_gaps": manual_gaps,
        "suggested_tags": _dedup_first_three(suggested_tags),
        "evidence": evidence,
        "action": action,
    }


def build_trade_attribution_payload(
    cur,
    trade_id: int,
    *,
    as_of: Date | None = None,
    enrich_name: bool = True,
    trade_row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    as_of = as_of or Date.today()
    row = dict(trade_row) if trade_row is not None else _fetch_trade(cur, trade_id, enrich_name=enrich_name)
    if not row:
        return {"found": False, "trade_id": trade_id}
    trade_date = _date(row.get("trade_date")) or as_of
    side = _side_normalized(row)
    if side not in {"buy", "sell"}:
        return {
            "found": True,
            "trade": {k: norm(v) for k, v in row.items()},
            "side": side,
            "as_of": as_of.isoformat(),
            "event_metrics": {
                "coverage": "not_applicable",
                "warnings": ["非买入/卖出交易暂不做价格归因。"],
            },
            "judgements": [{"level": "muted", "label": "不适用", "message": "该交易类型不参与自动归因。"}],
            "data_quality": {"level": "muted", "warnings": ["非买入/卖出交易暂不做价格归因。"]},
        }
    attribution_end = as_of
    exit_date = None
    exit_price = None
    attribution_end_reason = None
    if side == "buy" and _text(row.get("position_lifecycle_status")) == "closed":
        close_date = _date(row.get("lifecycle_close_date"))
        close_price = dec(row.get("lifecycle_close_price"))
        if close_date and close_price > 0:
            exit_date = close_date
            exit_price = close_price
            attribution_end = min(as_of, close_date)
            attribution_end_reason = "position_closed"
    rows, instrument = _fetch_price_rows(cur, row, end=attribution_end)
    metrics = _price_performance_metrics(
        rows,
        trade_date=trade_date,
        trade_price=dec(row.get("price")) or None,
        exit_date=exit_date,
        exit_price=exit_price,
        attribution_end_reason=attribution_end_reason,
    )
    opportunity: dict[str, Any] = {}
    if side == "sell" and metrics.get("coverage") == "ok":
        current = metrics.get("current_return_pct")
        opportunity = {
            "missed_gain_pct": current if current is not None and current > 0 else Decimal("0"),
            "avoided_loss_pct": -current if current is not None and current < 0 else Decimal("0"),
        }
    return {
        "found": True,
        "trade": {k: norm(v) for k, v in row.items()},
        "side": side,
        "as_of": as_of.isoformat(),
        "instrument": instrument,
        "event_metrics": {k: norm(v) for k, v in metrics.items()},
        "opportunity": {k: norm(v) for k, v in opportunity.items()},
        "judgements": _judgements(row, metrics),
        "auto_review": build_auto_trade_review(row, metrics, opportunity=opportunity),
        "data_quality": {
            "level": "ok" if metrics.get("coverage") == "ok" else "warn",
            "warnings": metrics.get("warnings") or [],
            "note": "P0 为事件级近似；多次买卖同一标的未做精确 lot matching。",
        },
    }


def _fetch_review_trades(
    cur,
    *,
    start: Date | None,
    end: Date | None,
    limit: int,
) -> list[dict[str, Any]]:
    where = ["f.trade_type IN ('buy', 'sell', 'fund_subscription', 'fund_redemption')"]
    params: list[Any] = []
    if start:
        where.append("f.trade_date >= %s")
        params.append(start)
    if end:
        where.append("f.trade_date <= %s")
        params.append(end)
    sql = _trade_query("WHERE " + " AND ".join(where) + " ORDER BY f.trade_date DESC, f.trade_id DESC LIMIT %s")
    cur.execute(sql, params + [limit])
    rows = [dict(row) for row in cur.fetchall()]
    _enrich_trade_names(cur, rows)
    _enrich_trade_asset_identity(cur, rows)
    _annotate_trade_lifecycle(rows)
    return rows


def _learn_annotation_defaults(rows: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    grouped: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        key = _asset_key(row)
        if not key:
            continue
        side = _side_normalized(row)
        if side == "buy":
            grouped[key]["buy_reason"].append(_text(row.get("buy_reason")))
        elif side == "sell":
            grouped[key]["sell_reason"].append(_text(row.get("sell_reason")))
        grouped[key]["initial_position_identity"].append(_text(row.get("initial_position_identity")))
        grouped[key]["current_position_identity"].append(_text(row.get("current_position_identity")))
    learned: dict[str, dict[str, str]] = {}
    for key, fields in grouped.items():
        defaults = {
            field: value
            for field, value in (
                (field, _mode(values))
                for field, values in fields.items()
            )
            if value
        }
        if defaults:
            learned[key] = defaults
    return learned


def _merge_tags(existing: list[str] | tuple[str, ...] | None, suggested: list[str] | None) -> list[str]:
    tags: list[str] = []
    for tag in list(existing or []) + list(suggested or []):
        clean = _text(tag)
        if clean and clean not in tags:
            tags.append(clean)
    return tags[:3]


def build_auto_trade_annotation(
    row: dict[str, Any],
    payload: dict[str, Any],
    *,
    learned_defaults: dict[str, dict[str, str]] | None = None,
    include_reviewed_tags: bool = True,
) -> dict[str, Any]:
    """Build conservative, non-overwriting review annotations for one trade."""
    side = payload.get("side") or _side_normalized(row)
    auto = payload.get("auto_review") or {}
    metrics = payload.get("event_metrics") or {}
    existing_status = _text(row.get("review_status")) or "auto"
    learned = (learned_defaults or {}).get(_asset_key(row), {})
    updates: dict[str, Any] = {}
    reasons: list[str] = []

    can_fill_manual_fields = existing_status in {"auto", "needs_review", ""}
    can_fill_tags = include_reviewed_tags or can_fill_manual_fields

    existing_tags = list(row.get("review_tags") or [])
    suggested_tags = list(auto.get("suggested_tags") or [])
    if auto.get("kind") != "missing_price" and metrics.get("coverage") == "ok":
        suggested_tags = [tag for tag in suggested_tags if tag != "data_issue"]
    merged_tags = _merge_tags(existing_tags, suggested_tags)
    if can_fill_tags and not existing_tags and merged_tags:
        updates["review_tags"] = merged_tags
        reasons.append("suggested_tags")

    if can_fill_manual_fields:
        if side == "buy" and not row.get("buy_reason"):
            if learned.get("buy_reason"):
                updates["buy_reason"] = learned["buy_reason"]
                reasons.append("learned_buy_reason")
            elif _is_cash_like(row):
                updates["buy_reason"] = "cash_management"
                reasons.append("cash_buy_reason")
        elif side == "sell" and not row.get("sell_reason"):
            if learned.get("sell_reason"):
                updates["sell_reason"] = learned["sell_reason"]
                reasons.append("learned_sell_reason")
            elif _is_cash_like(row):
                updates["sell_reason"] = "liquidity_need"
                reasons.append("cash_sell_reason")

        if not row.get("initial_position_identity"):
            identity = learned.get("initial_position_identity") or ("cash_management" if _is_cash_like(row) else "")
            if identity and identity != "closed":
                updates["initial_position_identity"] = identity
                reasons.append("initial_identity")
        if not row.get("current_position_identity"):
            identity = learned.get("current_position_identity") or ("cash_management" if _is_cash_like(row) else "")
            if identity:
                updates["current_position_identity"] = identity
                reasons.append("current_identity")
        effective_identity = updates.get("current_position_identity") or row.get("current_position_identity")
        if effective_identity and not row.get("sell_rule_type"):
            rule_type = IDENTITY_SELL_RULE_TYPES.get(str(effective_identity))
            if rule_type:
                updates["sell_rule_type"] = rule_type
                reasons.append("sell_rule_type")
        if (
            any(field in updates for field in ("initial_position_identity", "current_position_identity"))
            and not row.get("identity_confidence")
        ):
            updates["identity_confidence"] = "medium"
            reasons.append("identity_confidence")
        if existing_status == "auto" and (updates or auto.get("needs_human_review")):
            updates["review_status"] = "needs_review"
            reasons.append("needs_confirmation")

    return {
        "trade_id": row.get("trade_id"),
        "updates": updates,
        "reasons": reasons,
        "auto_review": auto,
    }


def apply_auto_trade_annotations(
    cur,
    *,
    start: Date | None = None,
    end: Date | None = None,
    limit: int = 1000,
    as_of: Date | None = None,
    dry_run: bool = True,
    include_reviewed_tags: bool = True,
) -> dict[str, Any]:
    """Apply conservative system annotations to trade facts.

    The updater only fills empty fields. Existing manual reasons, identities,
    and review statuses are preserved.
    """
    as_of = as_of or Date.today()
    rows = _fetch_review_trades(cur, start=start, end=end, limit=limit)
    learned_defaults = _learn_annotation_defaults(rows)
    examples: list[dict[str, Any]] = []
    field_counts: dict[str, int] = defaultdict(int)
    changed = 0
    updated = 0
    skipped = 0

    for row in rows:
        payload = build_trade_attribution_payload(
            cur,
            int(row["trade_id"]),
            as_of=as_of,
            enrich_name=False,
            trade_row=row,
        )
        annotation = build_auto_trade_annotation(
            row,
            payload,
            learned_defaults=learned_defaults,
            include_reviewed_tags=include_reviewed_tags,
        )
        updates = annotation["updates"]
        if not updates:
            skipped += 1
            continue
        changed += 1
        for field in updates:
            field_counts[field] += 1
        if len(examples) < 20:
            examples.append(
                {
                    "trade_id": row.get("trade_id"),
                    "trade_date": row.get("trade_date"),
                    "name": row.get("name") or row.get("symbol"),
                    "symbol": row.get("symbol"),
                    "side": payload.get("side"),
                    "updates": updates,
                    "reasons": annotation["reasons"],
                }
            )
        if dry_run:
            continue
        set_parts = []
        params: list[Any] = []
        for field, value in updates.items():
            if field not in AUTO_ANNOTATION_COLUMNS:
                continue
            if field == "review_tags":
                set_parts.append(f"{field} = %s::text[]")
            else:
                set_parts.append(f"{field} = %s")
            params.append(value)
        if not set_parts:
            continue
        params.append(row["trade_id"])
        cur.execute(
            f"""
            UPDATE pams_investment_trade_fact
            SET {", ".join(set_parts)},
                updated_at = NOW()
            WHERE trade_id = %s
            """,
            params,
        )
        updated += cur.rowcount or 0

    return {
        "as_of": as_of.isoformat(),
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
        "limit": limit,
        "dry_run": dry_run,
        "include_reviewed_tags": include_reviewed_tags,
        "scanned_count": len(rows),
        "changed_count": changed,
        "updated_count": updated,
        "skipped_count": skipped,
        "field_counts": dict(field_counts),
        "examples": [{k: norm(v) for k, v in item.items()} for item in examples],
        "data_quality": {
            "note": "Only empty fields are filled. Existing manual reasons, identities, and reviewed/ignored statuses are preserved.",
        },
    }


def build_trade_auto_review_queue_payload(
    cur,
    *,
    kind: str = "needs_human_review",
    start: Date | None = None,
    end: Date | None = None,
    limit: int = 100,
    as_of: Date | None = None,
) -> dict[str, Any]:
    as_of = as_of or Date.today()
    normalized_kind = kind if kind in AUTO_REVIEW_QUEUE_KINDS else "needs_human_review"
    rows = _fetch_review_trades(cur, start=start, end=end, limit=min(max(limit * 4, limit), 1000))
    counts: dict[str, int] = defaultdict(int)
    items: list[dict[str, Any]] = []
    for row in rows:
        payload = build_trade_attribution_payload(
            cur,
            int(row["trade_id"]),
            as_of=as_of,
            enrich_name=False,
            trade_row=row,
        )
        auto = payload.get("auto_review") or {}
        item_kinds = set(auto.get("queue_kinds") or [])
        if auto.get("level"):
            item_kinds.add(str(auto["level"]))
        if auto.get("kind"):
            item_kinds.add(str(auto["kind"]))
        for item_kind in item_kinds:
            counts[item_kind] += 1
        if normalized_kind != "all" and normalized_kind not in item_kinds:
            continue
        metrics = payload.get("event_metrics") or {}
        items.append(
            {
                "trade_id": row.get("trade_id"),
                "trade_date": row.get("trade_date"),
                "name": row.get("name") or row.get("symbol"),
                "symbol": row.get("symbol"),
                "side": payload.get("side"),
                "current_return_pct": metrics.get("current_return_pct"),
                "auto_review": auto,
            }
        )
        if len(items) >= limit:
            break
    return {
        "as_of": as_of.isoformat(),
        "kind": normalized_kind,
        "limit": limit,
        "items": [{k: norm(v) for k, v in item.items()} for item in items],
        "counts": dict(counts),
        "data_quality": {
            "note": "系统初判只做事后归因排队，不自动改写交易事实或交易复盘字段。",
        },
    }


def _summary_bucket(label: str) -> dict[str, Any]:
    return {
        "key": label,
        "label": label,
        "sample_count": 0,
        "win_count": 0,
        "avg_return_pct": None,
        "median_return_pct": None,
        "avg_mae_pct": None,
        "avg_mfe_pct": None,
        "missed_gain_count": 0,
        "avoided_loss_count": 0,
        "gross_exposure_cny": Decimal("0"),
        "estimated_decision_effect_cny": Decimal("0"),
        "_returns": [],
        "_mae": [],
        "_mfe": [],
    }


def _finalize_bucket(bucket: dict[str, Any]) -> dict[str, Any]:
    returns = sorted(bucket.pop("_returns"))
    mae = bucket.pop("_mae")
    mfe = bucket.pop("_mfe")
    if returns:
        bucket["avg_return_pct"] = sum(returns, Decimal("0")) / Decimal(len(returns))
        mid = len(returns) // 2
        bucket["median_return_pct"] = (
            returns[mid]
            if len(returns) % 2
            else (returns[mid - 1] + returns[mid]) / Decimal("2")
        )
        bucket["win_rate"] = Decimal(bucket["win_count"]) / Decimal(len(returns)) * Decimal("100")
    if mae:
        bucket["avg_mae_pct"] = sum(mae, Decimal("0")) / Decimal(len(mae))
    if mfe:
        bucket["avg_mfe_pct"] = sum(mfe, Decimal("0")) / Decimal(len(mfe))
    bucket["quality"] = "low_sample" if bucket["sample_count"] < 5 else "ok"
    return {k: norm(v) for k, v in bucket.items()}


def _add_metric_to_bucket(bucket: dict[str, Any], row: dict[str, Any], payload: dict[str, Any]) -> None:
    metrics = payload.get("event_metrics") or {}
    if metrics.get("coverage") != "ok":
        return
    current = dec(metrics.get("current_return_pct"))
    mae = metrics.get("max_adverse_excursion_pct")
    mfe = metrics.get("max_favorable_excursion_pct")
    side = payload.get("side")
    exposure = _trade_exposure_cny(row)
    effect = _decision_effect_cny(row, payload)
    bucket["sample_count"] += 1
    bucket["_returns"].append(current)
    if effect is not None and effect > 0:
        bucket["win_count"] += 1
    if mae is not None:
        bucket["_mae"].append(dec(mae))
    if mfe is not None:
        bucket["_mfe"].append(dec(mfe))
    if side == "sell":
        if current > Decimal("5"):
            bucket["missed_gain_count"] += 1
        if current < Decimal("-5"):
            bucket["avoided_loss_count"] += 1
    bucket["gross_exposure_cny"] += exposure
    if effect is not None:
        bucket["estimated_decision_effect_cny"] += effect


def _trade_exposure_cny(row: dict[str, Any]) -> Decimal:
    net_cash_cny = abs(dec(row.get("net_cash_cny")))
    if net_cash_cny > 0:
        return net_cash_cny
    gross = abs(dec(row.get("gross_amount")))
    if gross <= 0:
        gross = abs(dec(row.get("net_cash")))
    fx = dec(row.get("fx_rate_to_cny"))
    if gross <= 0:
        return Decimal("0")
    if fx <= 0:
        fx = Decimal("1")
    return gross * fx


def _trade_fee_drag_cny(row: dict[str, Any]) -> Decimal:
    fee = abs(dec(row.get("fee_amount"))) + abs(dec(row.get("tax_amount")))
    if fee <= 0:
        return Decimal("0")
    fx = dec(row.get("fx_rate_to_cny"))
    if fx <= 0:
        fx = Decimal("1")
    return -fee * fx


def _trade_realized_pnl_cny(row: dict[str, Any]) -> Decimal:
    realized_cny = dec(row.get("realized_pnl_cny"))
    if realized_cny:
        return realized_cny
    realized = dec(row.get("realized_pnl"))
    if not realized:
        return Decimal("0")
    fx = dec(row.get("fx_rate_to_cny"))
    if fx <= 0:
        fx = Decimal("1")
    return realized * fx


def _decision_effect_cny(row: dict[str, Any], payload: dict[str, Any]) -> Decimal | None:
    metrics = payload.get("event_metrics") or {}
    if metrics.get("coverage") != "ok":
        return None
    current = _metric_decimal(metrics, "current_return_pct")
    exposure = _trade_exposure_cny(row)
    if current is None or exposure <= 0:
        return None
    price_move = exposure * current / Decimal("100")
    return -price_move if payload.get("side") == "sell" else price_move


def _review_priority_score(row: dict[str, Any], payload: dict[str, Any], effect_cny: Decimal | None) -> int:
    auto = payload.get("auto_review") or {}
    level_score = {
        "danger": 100,
        "bad": 82,
        "warn": 58,
        "muted": 42,
        "neutral": 24,
        "good": 10,
    }.get(str(auto.get("level") or ""), 20)
    if auto.get("needs_human_review"):
        level_score += 24
    if auto.get("kind") == "missing_price":
        level_score += 16
    if auto.get("kind") == "identity_drift":
        level_score += 18
    manual_gaps = auto.get("manual_gaps") or []
    level_score += min(len(manual_gaps) * 5, 20)
    if effect_cny is not None:
        level_score += min(int(abs(effect_cny) / Decimal("1000")), 30)
    return level_score


def _asset_trade_contribution_reason(record: dict[str, Any], effect: Decimal) -> dict[str, Any]:
    side = _text(record.get("side")).lower()
    current = record.get("current_return_pct")
    has_return = current not in (None, "")
    current_pct = dec(current) if has_return else None
    mae = record.get("max_adverse_excursion_pct")
    mfe = record.get("max_favorable_excursion_pct")
    mae_pct = dec(mae) if mae not in (None, "") else None
    mfe_pct = dec(mfe) if mfe not in (None, "") else None
    auto = record.get("auto_review") or {}
    judgement = record.get("judgement") or {}
    auto_label = auto.get("label") or judgement.get("label")
    auto_summary = auto.get("summary") or judgement.get("message")
    auto_kind = _text(auto.get("kind"))
    is_closed_buy = side == "buy" and _text(record.get("position_lifecycle_status")) == "closed"
    is_closed_attribution = is_closed_buy and _text(record.get("attribution_end_reason")) == "position_closed"
    closed_note = "已清仓；" if is_closed_buy else ""
    basis_label = "按清仓价估算" if is_closed_attribution else "按复盘日价格估算"
    end_date = record.get("attribution_end_date") or record.get("lifecycle_close_date")
    end_note = f"截至 {end_date}，" if is_closed_attribution and end_date else ""

    def pct_text(value: Decimal | None, *, absolute: bool = False) -> str:
        if value is None:
            return "--"
        display = abs(value) if absolute else value
        return f"{display:.1f}%"

    def validation_text() -> str:
        if mfe_pct is None:
            return "最高浮盈缺少覆盖"
        if mfe_pct < Decimal("3"):
            return f"最高浮盈不足 3%（{pct_text(mfe_pct)}）"
        return f"最高浮盈 {pct_text(mfe_pct)}"

    if effect > Decimal("0.01"):
        tone = "gain"
        label = "正贡献"
    elif effect < Decimal("-0.01"):
        tone = "loss"
        label = "负贡献"
    else:
        tone = "flat"
        label = "中性"

    if not has_return:
        reason = "价格覆盖不足，暂时不能解释这笔交易的贡献方向。"
    elif side == "buy":
        if auto_kind == "trend_entry_failed":
            reason = (
                f"{closed_note}{end_note}{basis_label}，买入后较基准下跌 {pct_text(current_pct, absolute=True)}，"
                f"{validation_text()}；趋势买入没有被验证，重点看入场确认和止损执行。"
            )
        elif auto_kind == "early_bottom_no_validation":
            reason = (
                f"{closed_note}{end_note}{basis_label}，买入后较基准下跌 {pct_text(current_pct, absolute=True)}，"
                f"{validation_text()}；更像抄底或估值判断过早。"
            )
        elif auto_kind == "add_after_drop_failed":
            reason = (
                f"{closed_note}{end_note}{basis_label}，补仓后较基准下跌 {pct_text(current_pct, absolute=True)}，"
                f"{validation_text()}；重点看是否越跌越买以及批次控制。"
            )
        elif auto_kind == "closed_stop_loss_buy":
            reason = (
                f"{closed_note}{end_note}{basis_label}，买入到清仓下跌 {pct_text(current_pct, absolute=True)}；"
                "止损已经执行，问题主要在入场信号或买入时点。"
            )
        elif auto_kind == "stop_loss_not_executed":
            reason = (
                f"{closed_note}{end_note}买入后最大回撤 {pct_text(mae_pct, absolute=True)}，"
                f"{basis_label}仍低于基准 {pct_text(current_pct, absolute=True)}；重点核对止损或降仓是否拖延。"
            )
        elif auto_kind == "giveback":
            reason = (
                f"{closed_note}买入后一度浮盈 {pct_text(mfe_pct)}，当前相对基准 {pct_text(current_pct)}；"
                "主要问题是盈利回吐和止盈执行。"
            )
        elif auto_kind == "effective_buy":
            reason = f"{closed_note}买入后价格较交易基准上涨 {current_pct:.1f}%，这笔买入形成正贡献。"
        elif effect > Decimal("0.01"):
            reason = f"买入后价格较交易基准上涨 {current_pct:.1f}%，这笔买入形成正贡献。"
        elif effect < Decimal("-0.01"):
            reason = (
                f"{closed_note}{end_note}{basis_label}，买入后价格较交易基准下跌 {abs(current_pct):.1f}%，"
                "这笔买入时点贡献为负；闭环结果需结合后续卖出。"
            )
        else:
            reason = f"{closed_note}买入后价格变化很小，对组合贡献接近中性。"
    elif side == "sell":
        if auto_kind in {"stop_loss_effective", "sell_avoided_loss", "sell_mild_avoided_loss"}:
            reason = (
                f"卖出后价格较交易基准下跌 {pct_text(current_pct, absolute=True)}，"
                "退出保护了组合；重点保留当时的触发规则。"
            )
        elif auto_kind == "rebalance_effective":
            reason = (
                f"卖出后价格较交易基准下跌 {pct_text(current_pct, absolute=True)}，"
                "降风险或再平衡结果有效。"
            )
        elif auto_kind == "risk_reduction_cost":
            reason = (
                f"卖出后价格较交易基准上涨 {pct_text(current_pct)}，这是降风险后的机会成本；"
                "需按当时风险预算复盘，不直接等同卖错。"
            )
        elif auto_kind in {"profit_take_too_early", "sell_missed_gain", "sell_mild_missed_gain"}:
            reason = (
                f"卖出后价格较交易基准上涨 {pct_text(current_pct)}，可能止盈或退出偏早；"
                "重点看卖出规则是否过紧。"
            )
        elif effect > Decimal("0.01"):
            reason = f"卖出后价格较交易基准下跌 {abs(current_pct):.1f}%，这笔卖出避免了后续损失。"
        elif effect < Decimal("-0.01"):
            reason = f"卖出后价格较交易基准上涨 {current_pct:.1f}%，这笔卖出错过了后续涨幅。"
        else:
            reason = "卖出后价格变化很小，对组合贡献接近中性。"
    else:
        reason = "非买入/卖出动作，只按事件级金额影响展示。"

    return {
        "contribution_tone": tone,
        "contribution_label": label,
        "contribution_reason": reason,
        "auto_label": auto_label,
        "auto_summary": auto_summary,
        "auto_level": auto.get("level") or judgement.get("level"),
        "auto_kind": auto_kind,
    }


def _event_review_record(row: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    metrics = payload.get("event_metrics") or {}
    auto = payload.get("auto_review") or {}
    effect = _decision_effect_cny(row, payload)
    return {
        "trade_id": row.get("trade_id"),
        "trade_date": row.get("trade_date"),
        "name": row.get("name") or row.get("symbol"),
        "symbol": row.get("symbol"),
        "side": payload.get("side"),
        "account_code": row.get("account_code"),
        "account_name": row.get("account_name"),
        "currency": row.get("currency"),
        "position_lifecycle_status": row.get("position_lifecycle_status"),
        "lifecycle_close_date": row.get("lifecycle_close_date"),
        "lifecycle_close_price": row.get("lifecycle_close_price"),
        "lifecycle_close_reason": row.get("lifecycle_close_reason"),
        "buy_reason": row.get("buy_reason"),
        "sell_reason": row.get("sell_reason"),
        "review_tags": row.get("review_tags") or [],
        "trade_intent": row.get("trade_intent"),
        "trade_plan": row.get("trade_plan"),
        "review_note": row.get("review_note"),
        "initial_position_identity": row.get("initial_position_identity"),
        "current_position_identity": row.get("current_position_identity"),
        "current_return_pct": metrics.get("current_return_pct"),
        "max_adverse_excursion_pct": metrics.get("max_adverse_excursion_pct"),
        "max_favorable_excursion_pct": metrics.get("max_favorable_excursion_pct"),
        "attribution_end_date": metrics.get("attribution_end_date"),
        "attribution_end_price": metrics.get("attribution_end_price"),
        "attribution_end_reason": metrics.get("attribution_end_reason"),
        "gross_exposure_cny": _trade_exposure_cny(row),
        "estimated_decision_effect_cny": effect,
        "fee_drag_cny": _trade_fee_drag_cny(row),
        "realized_pnl_cny": _trade_realized_pnl_cny(row),
        "auto_review": auto,
        "judgement": (payload.get("judgements") or [{}])[0],
        "priority_score": _review_priority_score(row, payload, effect),
    }


def _asset_lookup_keys(*values: Any) -> set[str]:
    keys: set[str] = set()
    for value in values:
        clean = _text(value)
        if not clean:
            continue
        keys.add(clean.casefold())
        keys.update(_symbol_name_keys(clean))
        if " - " in clean:
            left, right = clean.split(" - ", 1)
            if left.strip():
                keys.add(left.strip().casefold())
                keys.update(_symbol_name_keys(left.strip()))
            if right.strip():
                keys.add(right.strip().casefold())
    return {key for key in keys if key}


def _load_actual_pnl_by_asset(cur, *, base_currency: str = "CNY") -> dict[str, dict[str, Any]]:
    """Map canonical return-attribution PnL to trade attribution assets.

    Trade attribution explains decision quality; return attribution is the
    canonical source for actual holding/closed-position PnL. This bridge keeps
    both numbers visible without treating event-level timing estimates as the
    true account PnL.
    """
    try:
        from services.return_attribution import build_return_attribution_payload

        payload = build_return_attribution_payload(cur, base_currency=base_currency)
    except Exception:
        return {}

    lookup: dict[str, dict[str, Any]] = {}
    rows = ((payload or {}).get("dimensions") or {}).get("holding") or []
    for row in rows:
        item = {
            "actual_total_pnl_cny": dec(row.get("total_pnl_cny") or row.get("pnl_cny") or row.get("value_cny")),
            "actual_daily_pnl_cny": dec(row.get("daily_pnl_cny")),
            "actual_daily_price_pnl_cny": dec(row.get("daily_price_pnl_cny")),
            "actual_daily_fx_pnl_cny": dec(row.get("daily_fx_pnl_cny")),
            "market_value_cny": dec(row.get("market_value_cny")),
            "source": "return_attribution",
        }
        keys = _asset_lookup_keys(row.get("symbol"), row.get("name"), row.get("display_name"))
        for detail in row.get("source_details") or []:
            keys.update(
                _asset_lookup_keys(
                    detail.get("source_symbol"),
                    detail.get("source_asset_name"),
                    detail.get("underlying_symbol"),
                    detail.get("underlying_name"),
                )
            )
        for key in keys:
            lookup.setdefault(key, item)
    return lookup


def _asset_actual_pnl(
    actual_pnl_by_asset: dict[str, dict[str, Any]] | None,
    *,
    key: str,
    name: Any,
    symbol: Any,
) -> dict[str, Any] | None:
    if not actual_pnl_by_asset:
        return None
    for lookup_key in _asset_lookup_keys(key, name, symbol):
        item = actual_pnl_by_asset.get(lookup_key)
        if item:
            return item
    return None


def _asset_attribution_breakdown(bucket: dict[str, Any], actual: dict[str, Any] | None) -> list[dict[str, Any]]:
    effect = dec(bucket.get("estimated_decision_effect_cny"))
    buy_effect = dec(bucket.get("buy_timing_effect_cny"))
    sell_effect = dec(bucket.get("sell_timing_effect_cny"))
    fee_drag = dec(bucket.get("fee_drag_cny"))
    breakdown = [
        {
            "key": "decision_timing",
            "label": "动作时点",
            "value_cny": effect,
            "status": "estimated",
            "note": "买入后上涨记正，卖出后上涨记机会成本。",
        },
        {
            "key": "buy_timing",
            "label": "买入时点",
            "value_cny": buy_effect,
            "status": "estimated",
            "note": "只统计买入动作后的价格表现。",
        },
        {
            "key": "sell_timing",
            "label": "卖出时点",
            "value_cny": sell_effect,
            "status": "estimated",
            "note": "卖出后下跌为避亏，卖出后上涨为卖飞。",
        },
        {
            "key": "fees",
            "label": "税费成本",
            "value_cny": fee_drag,
            "status": "actual",
            "note": "来自交易事实表的手续费和税费。",
        },
    ]
    if actual:
        actual_total = dec(actual.get("actual_total_pnl_cny"))
        unexplained = actual_total - effect - fee_drag
        breakdown.insert(
            0,
            {
                "key": "actual_total",
                "label": "真实总盈亏",
                "value_cny": actual_total,
                "status": "actual",
                "note": "来自收益归因口径，包含已清仓和当前浮盈亏。",
            },
        )
        breakdown.append(
            {
                "key": "unexplained",
                "label": "持有/市场未解释",
                "value_cny": unexplained,
                "status": "needs_benchmark",
                "note": "真实盈亏扣除动作时点和税费后的差额，后续接入基准归因继续拆分。",
            }
        )
    else:
        breakdown.append(
            {
                "key": "actual_total",
                "label": "真实总盈亏",
                "value_cny": None,
                "status": "missing",
                "note": "收益归因中未找到该标的，暂只能看动作时点。",
            }
        )
    return [{k: norm(v) for k, v in item.items()} for item in breakdown]


def _asset_rule_insights(bucket: dict[str, Any], actual: dict[str, Any] | None) -> list[dict[str, Any]]:
    insights: list[dict[str, Any]] = []
    effect = dec(bucket.get("estimated_decision_effect_cny"))
    buy_effect = dec(bucket.get("buy_timing_effect_cny"))
    sell_effect = dec(bucket.get("sell_timing_effect_cny"))
    fee_drag = dec(bucket.get("fee_drag_cny"))
    plan_count = int(dec(bucket.get("planned_trade_count")))
    trade_count = int(dec(bucket.get("trade_count")))
    actual_total = dec(actual.get("actual_total_pnl_cny")) if actual else None

    if actual_total is not None and effect and (actual_total > 0 > effect or actual_total < 0 < effect):
        insights.append(
            {
                "level": "warn",
                "title": "真实盈亏与动作贡献方向不同",
                "detail": "说明后续持有、市场环境、仓位大小或未接入基准因素占主导，不能只按动作时点下结论。",
            }
        )
    if buy_effect < Decimal("-500"):
        insights.append(
            {
                "level": "bad",
                "title": "买入时点拖累",
                "detail": "买入后表现为负，优先检查入场确认、分批规则和止损触发。",
            }
        )
    elif buy_effect > Decimal("500"):
        insights.append(
            {
                "level": "good",
                "title": "买入时点有效",
                "detail": "买入动作贡献为正，可保留当时的入场条件作为复盘样本。",
            }
        )
    if sell_effect < Decimal("-500"):
        insights.append(
            {
                "level": "bad",
                "title": "卖出机会成本明显",
                "detail": "卖出后继续上涨，优先检查止盈阈值、减仓节奏和是否过早落袋。",
            }
        )
    elif sell_effect > Decimal("500"):
        insights.append(
            {
                "level": "good",
                "title": "卖出起到保护作用",
                "detail": "卖出后价格下跌，退出纪律或再平衡动作对组合有帮助。",
            }
        )
    if fee_drag < Decimal("-200"):
        insights.append(
            {
                "level": "warn",
                "title": "税费成本需要纳入交易阈值",
                "detail": "该标的交易税费拖累较大，小幅波段需要更高胜率或更宽价差。",
            }
        )
    if trade_count and plan_count < trade_count:
        insights.append(
            {
                "level": "warn",
                "title": "交易计划记录不完整",
                "detail": "部分交易缺少计划或意图字段，系统无法判断是计划内分批还是临时操作。",
            }
        )
    if not insights:
        insights.append(
            {
                "level": "neutral",
                "title": "暂无强信号",
                "detail": "当前样本没有明显动作拖累，后续重点补齐基准归因和交易计划字段。",
            }
        )
    return insights[:4]


def _build_trade_asset_heatmap(
    event_records: list[dict[str, Any]],
    *,
    actual_pnl_by_asset: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    grouped: dict[str, dict[str, Any]] = {}
    directional_trades: list[dict[str, Any]] = []
    for record in event_records:
        effect = record.get("estimated_decision_effect_cny")
        if effect is None:
            continue
        key = _asset_key(record)
        if not key:
            continue
        bucket = grouped.setdefault(
            key,
            {
                "key": key,
                "name": record.get("name") or record.get("symbol"),
                "symbol": record.get("symbol"),
                "trade_count": 0,
                "buy_count": 0,
                "sell_count": 0,
                "positive_count": 0,
                "negative_count": 0,
                "flat_count": 0,
                "estimated_decision_effect_cny": Decimal("0"),
                "buy_timing_effect_cny": Decimal("0"),
                "sell_timing_effect_cny": Decimal("0"),
                "fee_drag_cny": Decimal("0"),
                "realized_pnl_cny": Decimal("0"),
                "gross_exposure_cny": Decimal("0"),
                "planned_trade_count": 0,
                "closed_trade_count": 0,
                "_returns": [],
                "_tags": defaultdict(int),
                "latest_trade_date": None,
                "trades": [],
            },
        )
        side = _text(record.get("side")).lower()
        value = dec(effect)
        bucket["trade_count"] += 1
        if side == "buy":
            bucket["buy_count"] += 1
        elif side == "sell":
            bucket["sell_count"] += 1
        if value > Decimal("0.01"):
            bucket["positive_count"] += 1
        elif value < Decimal("-0.01"):
            bucket["negative_count"] += 1
        else:
            bucket["flat_count"] += 1
        bucket["estimated_decision_effect_cny"] += value
        bucket["gross_exposure_cny"] += dec(record.get("gross_exposure_cny"))
        bucket["fee_drag_cny"] += dec(record.get("fee_drag_cny"))
        bucket["realized_pnl_cny"] += dec(record.get("realized_pnl_cny"))
        if side == "buy":
            bucket["buy_timing_effect_cny"] += value
        elif side == "sell":
            bucket["sell_timing_effect_cny"] += value
        if _text(record.get("trade_plan")) or _text(record.get("trade_intent")):
            bucket["planned_trade_count"] += 1
        if _text(record.get("position_lifecycle_status")) == "closed":
            bucket["closed_trade_count"] += 1
        current_return = record.get("current_return_pct")
        if current_return not in (None, ""):
            bucket["_returns"].append(dec(current_return))
        trade_date = record.get("trade_date")
        if trade_date and (bucket["latest_trade_date"] is None or str(trade_date) > str(bucket["latest_trade_date"])):
            bucket["latest_trade_date"] = trade_date
        for tag in [record.get("buy_reason"), record.get("sell_reason"), *(record.get("review_tags") or [])]:
            clean = _text(tag)
            if clean:
                bucket["_tags"][clean] += 1
        contribution = _asset_trade_contribution_reason(record, value)
        trade_entry = {
            "trade_id": record.get("trade_id"),
            "trade_date": record.get("trade_date"),
            "name": record.get("name") or record.get("symbol"),
            "symbol": record.get("symbol"),
            "side": record.get("side"),
            "position_lifecycle_status": record.get("position_lifecycle_status"),
            "lifecycle_close_date": record.get("lifecycle_close_date"),
            "lifecycle_close_price": record.get("lifecycle_close_price"),
            "lifecycle_close_reason": record.get("lifecycle_close_reason"),
            "buy_reason": record.get("buy_reason"),
            "sell_reason": record.get("sell_reason"),
            "review_tags": record.get("review_tags") or [],
            "trade_intent": record.get("trade_intent"),
            "trade_plan": record.get("trade_plan"),
            "current_return_pct": record.get("current_return_pct"),
            "max_adverse_excursion_pct": record.get("max_adverse_excursion_pct"),
            "max_favorable_excursion_pct": record.get("max_favorable_excursion_pct"),
            "attribution_end_date": record.get("attribution_end_date"),
            "attribution_end_price": record.get("attribution_end_price"),
            "attribution_end_reason": record.get("attribution_end_reason"),
            "estimated_decision_effect_cny": value,
            "fee_drag_cny": record.get("fee_drag_cny"),
            "realized_pnl_cny": record.get("realized_pnl_cny"),
            "label": contribution.get("auto_label")
            or (record.get("auto_review") or {}).get("label")
            or (record.get("judgement") or {}).get("label"),
            **contribution,
        }
        bucket["trades"].append(trade_entry)
        directional_trades.append(
            {
                **trade_entry,
                "asset_key": key,
                "asset_name": bucket["name"],
                "asset_symbol": bucket["symbol"],
            }
        )

    assets = []
    for bucket in grouped.values():
        returns = bucket.pop("_returns")
        tags = bucket.pop("_tags")
        effect = bucket["estimated_decision_effect_cny"]
        exposure = bucket["gross_exposure_cny"]
        trade_count = bucket["trade_count"]
        avg_return = sum(returns, Decimal("0")) / Decimal(len(returns)) if returns else None
        trades = bucket["trades"]
        actual = _asset_actual_pnl(
            actual_pnl_by_asset,
            key=str(bucket.get("key") or ""),
            name=bucket.get("name"),
            symbol=bucket.get("symbol"),
        )
        actual_total = dec(actual.get("actual_total_pnl_cny")) if actual else None
        impact_value = actual_total if actual_total is not None else effect
        trades.sort(
            key=lambda item: (
                abs(dec(item.get("estimated_decision_effect_cny"))),
                str(item.get("trade_date") or ""),
                int(dec(item.get("trade_id"))),
            ),
            reverse=True,
        )
        weight = abs(impact_value)
        assets.append(
            {
                **bucket,
                "avg_return_pct": avg_return,
                "weight_cny": weight,
                "impact_tone": "gain" if impact_value > Decimal("0.01") else "loss" if impact_value < Decimal("-0.01") else "flat",
                "actual_total_pnl_cny": actual.get("actual_total_pnl_cny") if actual else None,
                "actual_daily_pnl_cny": actual.get("actual_daily_pnl_cny") if actual else None,
                "actual_daily_price_pnl_cny": actual.get("actual_daily_price_pnl_cny") if actual else None,
                "actual_daily_fx_pnl_cny": actual.get("actual_daily_fx_pnl_cny") if actual else None,
                "market_value_cny": actual.get("market_value_cny") if actual else None,
                "unexplained_pnl_cny": actual_total - effect - dec(bucket.get("fee_drag_cny")) if actual_total is not None else None,
                "attribution_status": "partial_with_actual_pnl" if actual else "timing_only",
                "missing_components": ["benchmark_market", "industry_relative"],
                "attribution_breakdown": _asset_attribution_breakdown(bucket, actual),
                "rule_insights": _asset_rule_insights(bucket, actual),
                "tags": [
                    {"key": key, "count": count}
                    for key, count in sorted(tags.items(), key=lambda item: (item[1], item[0]), reverse=True)[:5]
                ],
                "trades": [{k: norm(v) for k, v in trade.items()} for trade in trades[:8]],
            }
        )
    assets.sort(
        key=lambda item: (
            abs(dec(item.get("estimated_decision_effect_cny"))),
            dec(item.get("weight_cny")),
            item.get("trade_count") or 0,
        ),
        reverse=True,
    )
    total_effect = sum((dec(item.get("estimated_decision_effect_cny")) for item in assets), Decimal("0"))
    directional_trades.sort(
        key=lambda item: (
            abs(dec(item.get("estimated_decision_effect_cny"))),
            str(item.get("trade_date") or ""),
            int(dec(item.get("trade_id"))),
        ),
        reverse=True,
    )
    positive_action_count = sum(
        1 for item in directional_trades if dec(item.get("estimated_decision_effect_cny")) > Decimal("0.01")
    )
    negative_action_count = sum(
        1 for item in directional_trades if dec(item.get("estimated_decision_effect_cny")) < Decimal("-0.01")
    )
    return {
        "asset_count": len(assets),
        "trade_count": sum((item.get("trade_count") or 0 for item in assets), 0),
        "positive_action_count": positive_action_count,
        "negative_action_count": negative_action_count,
        "flat_action_count": len(directional_trades) - positive_action_count - negative_action_count,
        "estimated_decision_effect_cny": norm(total_effect),
        "assets": [{k: norm(v) for k, v in item.items()} for item in assets],
        "directional_trades": [{k: norm(v) for k, v in item.items()} for item in directional_trades],
        "note": "按标的聚合真实盈亏和事件级交易动作；基准/行业超额仍为待接入项。",
    }


def _event_needs_attention(record: dict[str, Any]) -> bool:
    auto = record.get("auto_review") or {}
    if auto.get("needs_human_review"):
        return True
    if auto.get("level") in {"danger", "bad", "warn"}:
        return True
    if auto.get("kind") in {"missing_price", "sell_missed_gain", "failed_buy", "identity_drift", "giveback"}:
        return True
    return False


def _pattern_takeaway(group_name: str, row: dict[str, Any]) -> str:
    avg = dec(row.get("avg_return_pct"))
    effect = dec(row.get("estimated_decision_effect_cny"))
    missed = int(dec(row.get("missed_gain_count")))
    avoided = int(dec(row.get("avoided_loss_count")))
    if group_name == "sell_reason":
        if missed > avoided:
            return f"卖出后继续上涨 {missed} 次，优先复核卖早。"
        if avoided > 0:
            return f"产生避亏 {avoided} 次，退出纪律有效。"
        return "卖出后走势暂时中性。"
    if effect < 0:
        return "金额加权后拖累组合，优先复核这类操作。"
    if avg < 0:
        return "平均交易后表现为负，但金额影响有限。"
    return "当前样本表现相对有效。"


def _event_matches_pattern(group_name: str, key: str, record: dict[str, Any]) -> bool:
    if group_name == "buy_reason":
        return record.get("side") == "buy" and str(record.get("buy_reason") or "未标注") == key
    if group_name == "sell_reason":
        return record.get("side") == "sell" and str(record.get("sell_reason") or "未标注") == key
    if group_name == "review_tags":
        tags = record.get("review_tags") or []
        if not tags:
            return key == "未标注"
        return key in {str(tag) for tag in tags}
    if group_name == "initial_position_identity":
        return str(record.get("initial_position_identity") or "未标注") == key
    if group_name == "current_position_identity":
        return str(record.get("current_position_identity") or "未标注") == key
    return False


def _pattern_examples(group_name: str, key: str, event_records: list[dict[str, Any]], *, limit: int = 3) -> list[dict[str, Any]]:
    matched = [
        record
        for record in event_records
        if record.get("estimated_decision_effect_cny") is not None and _event_matches_pattern(group_name, key, record)
    ]
    ranked = sorted(
        matched,
        key=lambda item: (
            dec(item.get("estimated_decision_effect_cny")),
            -int(dec(item.get("priority_score"))),
            str(item.get("trade_date") or ""),
        ),
    )
    out = []
    for record in ranked[:limit]:
        auto = record.get("auto_review") or {}
        out.append(
            {
                "trade_id": record.get("trade_id"),
                "trade_date": norm(record.get("trade_date")),
                "name": record.get("name") or record.get("symbol"),
                "symbol": record.get("symbol"),
                "side": record.get("side"),
                "position_lifecycle_status": record.get("position_lifecycle_status"),
                "current_return_pct": norm(record.get("current_return_pct")),
                "estimated_decision_effect_cny": norm(record.get("estimated_decision_effect_cny")),
                "auto_label": auto.get("label") or auto.get("kind"),
            }
        )
    return out


def _pattern_rows(
    group_name: str,
    rows: list[dict[str, Any]],
    *,
    event_records: list[dict[str, Any]] | None = None,
    limit: int = 3,
) -> list[dict[str, Any]]:
    ranked = sorted(
        rows or [],
        key=lambda item: (
            dec(item.get("estimated_decision_effect_cny")),
            dec(item.get("avg_return_pct")),
            -int(dec(item.get("sample_count"))),
        ),
    )
    out = []
    for row in ranked[:limit]:
        out.append(
            {
                "key": row.get("key"),
                "label": row.get("label"),
                "sample_count": row.get("sample_count"),
                "avg_return_pct": row.get("avg_return_pct"),
                "win_rate": row.get("win_rate"),
                "estimated_decision_effect_cny": row.get("estimated_decision_effect_cny"),
                "gross_exposure_cny": row.get("gross_exposure_cny"),
                "missed_gain_count": row.get("missed_gain_count"),
                "avoided_loss_count": row.get("avoided_loss_count"),
                "quality": row.get("quality"),
                "takeaway": _pattern_takeaway(group_name, row),
                "examples": _pattern_examples(group_name, str(row.get("key") or row.get("label") or ""), event_records or []),
            }
        )
    return out


def _build_trade_review_workbench(
    event_records: list[dict[str, Any]],
    finalized_groups: dict[str, list[dict[str, Any]]],
    *,
    sample_count: int,
    missing_price: int,
) -> dict[str, Any]:
    attention = [record for record in event_records if _event_needs_attention(record)]
    priority_items = sorted(attention, key=lambda item: item.get("priority_score") or 0, reverse=True)[:8]
    risk_count = sum(1 for item in event_records if (item.get("auto_review") or {}).get("level") in {"danger", "bad", "warn"})
    missed_gain_count = sum(1 for item in event_records if "sell_missed_gain" in ((item.get("auto_review") or {}).get("queue_kinds") or []))
    avoided_loss_count = sum(1 for item in event_records if "sell_avoided_loss" in ((item.get("auto_review") or {}).get("queue_kinds") or []))
    decision_effect = sum(
        (item.get("estimated_decision_effect_cny") for item in event_records if item.get("estimated_decision_effect_cny") is not None),
        Decimal("0"),
    )
    worst_item = priority_items[0] if priority_items else None

    conclusions: list[dict[str, Any]] = []
    if worst_item:
        auto = worst_item.get("auto_review") or {}
        conclusions.append(
            {
                "level": auto.get("level") or "warn",
                "title": f"先复盘：{worst_item.get('name') or worst_item.get('symbol') or '--'}",
                "detail": auto.get("summary") or "这笔交易进入高优先级复盘队列。",
                "metric": auto.get("label") or "待复盘",
            }
        )
    if missed_gain_count:
        conclusions.append(
            {
                "level": "bad",
                "title": f"卖出后继续上涨 {missed_gain_count} 笔",
                "detail": "这类交易重点检查止盈规则、卖出触发条件和是否过早落袋。",
                "metric": "卖早风险",
            }
        )
    if avoided_loss_count:
        conclusions.append(
            {
                "level": "good",
                "title": f"卖出后继续下跌 {avoided_loss_count} 笔",
                "detail": "这部分说明退出纪律在一些交易上起到了避亏作用。",
                "metric": "避亏有效",
            }
        )
    if missing_price:
        conclusions.append(
            {
                "level": "warn",
                "title": f"{missing_price} 笔交易缺少价格覆盖",
                "detail": "这些交易不会进入涨跌归因，先修标的映射或价格数据。",
                "metric": "数据缺口",
            }
        )
    if not conclusions:
        conclusions.append(
            {
                "level": "neutral",
                "title": "暂无明显高风险交易",
                "detail": "当前样本没有触发高优先级复盘规则，可继续补充主观标签提高分析质量。",
                "metric": "观察",
            }
        )

    pattern_specs = [
        {
            "key": "buy_reason",
            "title": "买入模式",
            "subtitle": "哪些买入理由在拖累结果",
            "focus": True,
        },
        {
            "key": "sell_reason",
            "title": "卖出模式",
            "subtitle": "哪些卖出理由容易卖早或避亏",
            "focus": True,
        },
        {
            "key": "review_tags",
            "title": "行为标签",
            "subtitle": "哪些复盘标签最值得纠正",
            "focus": True,
        },
    ]

    def _pattern_group(spec: dict[str, Any], *, limit: int) -> dict[str, Any]:
        group_name = str(spec["key"])
        rows = finalized_groups.get(group_name, [])
        return {
            "key": group_name,
            "title": spec["title"],
            "subtitle": spec["subtitle"],
            "rows": _pattern_rows(group_name, rows, event_records=event_records, limit=limit),
        }

    patterns = [
        _pattern_group(spec, limit=3)
        for spec in pattern_specs
        if spec.get("focus")
    ]
    pattern_universe = [
        _pattern_group(spec, limit=len(finalized_groups.get(str(spec["key"]), [])))
        for spec in pattern_specs
    ]

    headline_level = "danger" if any((item.get("auto_review") or {}).get("level") == "danger" for item in priority_items) else (
        "bad" if risk_count else ("warn" if missing_price else "neutral")
    )
    headline_title = (
        f"先处理 {len(priority_items)} 笔高优先级复盘"
        if priority_items
        else ("先补价格数据" if missing_price else "复盘样本整体可读")
    )
    return {
        "headline": {
            "level": headline_level,
            "title": headline_title,
            "subtitle": "按交易后表现、金额影响、标签缺口和身份漂移自动排序；金额为价格变动影响估算，不等同于已实现盈亏。",
        },
        "kpis": [
            {"key": "sample", "label": "可分析交易", "value": sample_count, "hint": "有价格覆盖"},
            {"key": "queue", "label": "待复盘", "value": len(priority_items), "hint": "高优先级"},
            {"key": "risk", "label": "风险信号", "value": risk_count, "hint": "亏损/卖飞/漂移"},
            {"key": "missed_gain", "label": "卖早风险", "value": missed_gain_count, "hint": "卖后继续涨"},
            {"key": "data_gap", "label": "数据缺口", "value": missing_price, "hint": "缺价格"},
        ],
        "decision_effect_cny": norm(decision_effect),
        "conclusions": [{k: norm(v) for k, v in item.items()} for item in conclusions[:4]],
        "priority_items": [{k: norm(v) for k, v in item.items()} for item in priority_items],
        "patterns": patterns,
        "pattern_universe": pattern_universe,
        "data_gaps": [
            {
                "kind": "missing_price",
                "label": "缺价格/标的映射",
                "count": missing_price,
                "action": "补价格或修正 asset 映射后，这些交易才会进入归因。",
            }
        ] if missing_price else [],
        "method_note": "只统计第三层交易模式：买入模式、卖出模式、行为标签。金额为事件级价格变动影响估算；行为标签为多标签，不可加总。",
    }


def build_trade_attribution_summary_payload(
    cur,
    *,
    start: Date | None = None,
    end: Date | None = None,
    limit: int = 300,
    as_of: Date | None = None,
) -> dict[str, Any]:
    as_of = as_of or Date.today()
    rows = _fetch_review_trades(cur, start=start, end=end, limit=limit)
    groups: dict[str, dict[str, dict[str, Any]]] = {
        "buy_reason": defaultdict(lambda: _summary_bucket("未标注")),
        "sell_reason": defaultdict(lambda: _summary_bucket("未标注")),
        "review_tags": defaultdict(lambda: _summary_bucket("未标注")),
    }
    events = []
    event_records = []
    missing_price = 0
    for row in rows:
        payload = build_trade_attribution_payload(
            cur,
            int(row["trade_id"]),
            as_of=as_of,
            enrich_name=False,
            trade_row=row,
        )
        metrics = payload.get("event_metrics") or {}
        event_records.append(_event_review_record(row, payload))
        if metrics.get("coverage") != "ok":
            missing_price += 1
            continue
        side = payload.get("side")
        key = row.get("buy_reason") if side == "buy" else row.get("sell_reason") if side == "sell" else None
        group_name = "buy_reason" if side == "buy" else "sell_reason" if side == "sell" else None
        if group_name:
            bucket_key = str(key or "未标注")
            bucket = groups[group_name][bucket_key]
            bucket["key"] = bucket_key
            bucket["label"] = bucket_key
            _add_metric_to_bucket(bucket, row, payload)
        tags = row.get("review_tags") or []
        if not tags:
            tags = ["未标注"]
        for tag in tags:
            bucket_key = str(tag or "未标注")
            bucket = groups["review_tags"][bucket_key]
            bucket["key"] = bucket_key
            bucket["label"] = bucket_key
            _add_metric_to_bucket(bucket, row, payload)
        effect = _decision_effect_cny(row, payload)
        events.append(
            {
                "trade_id": row.get("trade_id"),
                "trade_date": row.get("trade_date"),
                "name": row.get("name") or row.get("symbol"),
                "symbol": row.get("symbol"),
                "side": side,
                "initial_position_identity": row.get("initial_position_identity"),
                "current_position_identity": row.get("current_position_identity"),
                "current_return_pct": metrics.get("current_return_pct"),
                "estimated_decision_effect_cny": effect,
                "judgement": (payload.get("judgements") or [{}])[0],
            }
        )
    finalized = {
        group: sorted(
            (_finalize_bucket(bucket) for bucket in buckets.values() if bucket["sample_count"] > 0),
            key=lambda item: (item.get("sample_count") or 0, dec(item.get("avg_return_pct"))),
            reverse=True,
        )
        for group, buckets in groups.items()
    }
    workbench = _build_trade_review_workbench(
        event_records,
        finalized,
        sample_count=len(events),
        missing_price=missing_price,
    )
    actual_pnl_by_asset = _load_actual_pnl_by_asset(cur, base_currency="CNY")
    asset_heatmap = _build_trade_asset_heatmap(event_records, actual_pnl_by_asset=actual_pnl_by_asset)
    return {
        "as_of": as_of.isoformat(),
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
        "limit": limit,
        "sample_count": len(events),
        "missing_price_count": missing_price,
        "groups": finalized,
        "events": [{k: norm(v) for k, v in event.items()} for event in events[:20]],
        "asset_heatmap": asset_heatmap,
        "workbench": workbench,
        "data_quality": {
            "note": "第三层模式归因：只统计买入模式、卖出模式、行为标签；金额为事件级近似，样本少于 5 的分组会标记 low_sample；行为标签为多标签，不可加总。",
            "missing_price_count": missing_price,
        },
    }
