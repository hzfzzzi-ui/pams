"""Daily return state helpers.

This module is the canonical status layer for daily investment return data.
It intentionally keeps the rules small: market-data jobs decide what to fetch,
then this service normalizes quote status, builds asset daily-PnL rows, and
writes audit-friendly facts.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date as Date
from decimal import Decimal
from typing import Any

import psycopg2.extras


RETURN_STATUS_PENDING = "pending"
RETURN_STATUS_ESTIMATED = "estimated"
RETURN_STATUS_CONFIRMED = "confirmed"
RETURN_STATUS_MIXED = "mixed"

RETURN_STATUSES = {
    RETURN_STATUS_PENDING,
    RETURN_STATUS_ESTIMATED,
    RETURN_STATUS_CONFIRMED,
}

STATUS_LABELS = {
    RETURN_STATUS_PENDING: "\u672a\u5237\u65b0",
    RETURN_STATUS_ESTIMATED: "\u9884\u4f30\u6536\u76ca",
    RETURN_STATUS_CONFIRMED: "\u4eca\u65e5\u6536\u76ca\u5df2\u786e\u8ba4",
    RETURN_STATUS_MIXED: "\u90e8\u5206\u9884\u4f30",
}

ESTIMATE_SOURCE_TOKENS = (
    "estimate",
    "estimated",
    "fundgz",
    "databento_estimate",
    "ibkr_tws_estimate",
    "ibkr_client_portal_estimate",
    "yahoo_estimate",
)

CONFIRMED_SOURCE_TOKENS = (
    "close",
    "confirmed",
    "tushare",
    "eastmoney_f10",
    "fund_nav",
    "ibkr_flex",
)

ESTIMATE_MARKETS = {"US_OVERNIGHT", "US_EXTENDED", "YAHOO_EXTENDED"}
ESTIMATE_SESSIONS = {"premarket", "intraday", "afterhours", "overnight"}
VALID_QUOTE_SESSIONS = {"open", "close", "premarket", "intraday", "afterhours", "overnight", "nav", "unknown"}


def dec(value: Any) -> Decimal:
    """Parse Decimal with a stable zero fallback."""
    if value in (None, ""):
        return Decimal("0")
    return Decimal(str(value))


def norm(value: Any) -> Any:
    """Return JSON-serializable values while preserving decimal text precision."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: norm(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [norm(item) for item in value]
    return value


def stable_source_hash(payload: dict[str, Any]) -> str:
    """Hash a payload deterministically for idempotent audit rows."""
    raw = json.dumps(norm(payload), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize_return_status(
    raw_status: Any = None,
    *,
    source: Any = None,
    market: Any = None,
    quote_session: Any = None,
) -> str:
    """Normalize provider and row-level status into PAMS's three states."""
    raw = str(raw_status or "").strip().lower()
    if raw in RETURN_STATUSES:
        return raw
    if raw.startswith(RETURN_STATUS_CONFIRMED):
        return RETURN_STATUS_CONFIRMED
    if raw.startswith(RETURN_STATUS_ESTIMATED):
        return RETURN_STATUS_ESTIMATED

    session = str(quote_session or "").strip().lower()
    if session in ESTIMATE_SESSIONS:
        return RETURN_STATUS_ESTIMATED

    market_value = str(market or "").strip().upper()
    if market_value in ESTIMATE_MARKETS:
        return RETURN_STATUS_ESTIMATED

    source_value = str(source or "").strip().lower()
    if any(token in source_value for token in ESTIMATE_SOURCE_TOKENS):
        return RETURN_STATUS_ESTIMATED
    if any(token in source_value for token in CONFIRMED_SOURCE_TOKENS):
        return RETURN_STATUS_CONFIRMED
    return RETURN_STATUS_PENDING


def return_status_label(status: Any) -> str:
    """Return the user-facing label for a normalized status."""
    normalized = str(status or RETURN_STATUS_PENDING).strip().lower()
    return STATUS_LABELS.get(normalized, STATUS_LABELS[RETURN_STATUS_PENDING])


def combine_return_status(statuses: list[Any]) -> str:
    """Combine asset-level statuses into one portfolio-level state."""
    normalized = [normalize_return_status(status) for status in statuses if status not in (None, "")]
    if not normalized:
        return RETURN_STATUS_PENDING
    unique = set(normalized)
    if unique == {RETURN_STATUS_CONFIRMED}:
        return RETURN_STATUS_CONFIRMED
    if unique == {RETURN_STATUS_ESTIMATED}:
        return RETURN_STATUS_ESTIMATED
    if unique == {RETURN_STATUS_PENDING}:
        return RETURN_STATUS_PENDING
    return RETURN_STATUS_MIXED


def quote_session_from_source(
    *,
    source: Any = None,
    market: Any = None,
    raw_status: Any = None,
    default: str = "close",
) -> str:
    """Infer the quote session bucket without using it as a data source."""
    source_value = str(source or "").strip().lower()
    market_value = str(market or "").strip().upper()
    status_value = str(raw_status or "").strip().lower()
    if "nav" in source_value or "fund" in source_value:
        return "nav"
    if "premarket" in source_value:
        return "premarket"
    if "afterhours" in source_value or market_value in {"US_EXTENDED", "YAHOO_EXTENDED"}:
        return "afterhours"
    if "overnight" in source_value or market_value == "US_OVERNIGHT":
        return "overnight"
    if "intraday" in source_value or status_value.startswith("estimated"):
        return "intraday"
    return default if default in VALID_QUOTE_SESSIONS else "unknown"


def asset_key(row: dict[str, Any]) -> str:
    """Return a stable asset key for daily PnL facts."""
    for key in ("asset_id", "symbol", "name"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return "unknown"


def quote_snapshot_from_market_daily(row: dict[str, Any]) -> dict[str, Any]:
    """Build a canonical quote snapshot from pams_asset_market_daily-like rows."""
    source = row.get("source")
    market = row.get("market")
    status = normalize_return_status(
        row.get("return_status"),
        source=source,
        market=market,
        quote_session=row.get("quote_session"),
    )
    if status == RETURN_STATUS_ESTIMATED and row.get("estimated_unit_nav") not in (None, ""):
        price = row.get("estimated_unit_nav")
    else:
        price = (
            row.get("close_price")
            if row.get("close_price") not in (None, "")
            else row.get("unit_nav")
            if row.get("unit_nav") not in (None, "")
            else row.get("estimated_unit_nav")
        )
    payload = {
        "quote_date": row.get("as_of") or row.get("quote_date"),
        "asset_id": row.get("asset_id"),
        "symbol": row.get("symbol"),
        "name": row.get("name"),
        "market": market,
        "currency": row.get("currency") or "CNY",
        "provider": row.get("provider") or str(source or "unknown").split("_")[0] or "unknown",
        "source": source or "unknown",
        "source_key": row.get("source_key") or row.get("symbol") or row.get("asset_id") or row.get("name") or "unknown",
        "quote_session": quote_session_from_source(
            source=source,
            market=market,
            raw_status=row.get("return_status"),
            default="close" if status == RETURN_STATUS_CONFIRMED else "intraday",
        ),
        "return_status": status,
        "price": price,
        "price_time": row.get("price_time") or row.get("updated_at"),
        "base_currency": row.get("base_currency") or "CNY",
        "fx_rate": row.get("fx_rate"),
        "raw_json": row.get("raw_json") or {},
    }
    payload["source_hash"] = stable_source_hash(payload)
    return payload


def daily_pnl_row_from_holding(row: dict[str, Any], *, trade_date: Any | None = None) -> dict[str, Any]:
    """Build a canonical asset daily-PnL row from holdings API rows."""
    status = normalize_return_status(row.get("daily_pnl_status") or row.get("daily_return_status"))
    daily_pnl = row.get("daily_total_pnl_cny")
    if daily_pnl in (None, ""):
        daily_pnl = row.get("daily_pnl_cny", row.get("daily_pnl", 0))
    market_value = row.get("market_value_cny", row.get("market_value", 0))
    total_pnl = row.get("holding_pnl_cny", row.get("holding_pnl", row.get("total_pnl_cny", 0)))
    payload = {
        "trade_date": trade_date or row.get("as_of") or row.get("snapshot_as_of"),
        "account_code": row.get("account_code") or row.get("account") or "",
        "asset_key": asset_key(row),
        "asset_id": row.get("asset_id"),
        "symbol": row.get("symbol"),
        "name": row.get("name"),
        "major_category": row.get("major_category"),
        "minor_category": row.get("minor_category"),
        "currency": row.get("currency") or "CNY",
        "base_currency": row.get("base_currency") or "CNY",
        "quantity": row.get("quantity"),
        "previous_price": row.get("previous_price"),
        "current_price": row.get("current_price"),
        "market_value_cny": market_value,
        "daily_pnl_cny": daily_pnl,
        "total_pnl_cny": total_pnl,
        "return_rate": row.get("return_rate"),
        "daily_return_rate": row.get("daily_return_rate"),
        "return_status": status,
        "provider": row.get("daily_pnl_provider") or row.get("provider"),
        "raw_json": {
            "daily_pnl_status_label": row.get("daily_pnl_status_label"),
            "daily_pnl_as_of": row.get("daily_pnl_as_of"),
        },
    }
    payload["source_hash"] = stable_source_hash(payload)
    return payload


def upsert_quote_snapshot(cur: Any, quote: dict[str, Any]) -> dict[str, Any]:
    """Upsert one quote snapshot and return the database row."""
    cur.execute(
        """
        INSERT INTO pams_market_quote_snapshot (
            quote_date, asset_id, symbol, name, market, currency,
            provider, source, source_key, quote_session, return_status,
            price, price_time, base_currency, fx_rate, source_hash, raw_json,
            updated_at
        )
        VALUES (
            %(quote_date)s, %(asset_id)s, %(symbol)s, %(name)s, %(market)s, %(currency)s,
            %(provider)s, %(source)s, %(source_key)s, %(quote_session)s, %(return_status)s,
            %(price)s, %(price_time)s, %(base_currency)s, %(fx_rate)s, %(source_hash)s,
            %(raw_json)s, now()
        )
        ON CONFLICT (quote_date, provider, source_key, quote_session)
        DO UPDATE SET
            asset_id = EXCLUDED.asset_id,
            symbol = EXCLUDED.symbol,
            name = EXCLUDED.name,
            market = EXCLUDED.market,
            currency = EXCLUDED.currency,
            source = EXCLUDED.source,
            return_status = EXCLUDED.return_status,
            price = EXCLUDED.price,
            price_time = EXCLUDED.price_time,
            base_currency = EXCLUDED.base_currency,
            fx_rate = EXCLUDED.fx_rate,
            source_hash = EXCLUDED.source_hash,
            raw_json = EXCLUDED.raw_json,
            updated_at = now()
        RETURNING *
        """,
        {**quote, "raw_json": psycopg2.extras.Json(norm(quote.get("raw_json") or {}))},
    )
    return cur.fetchone()


def upsert_asset_daily_pnl(cur: Any, row: dict[str, Any]) -> dict[str, Any]:
    """Upsert one asset daily-PnL fact and return the database row."""
    cur.execute(
        """
        INSERT INTO pams_asset_daily_pnl (
            trade_date, account_code, asset_key, asset_id, symbol, name,
            major_category, minor_category, currency, base_currency,
            quantity, previous_price, current_price, market_value_cny,
            daily_pnl_cny, total_pnl_cny, return_rate, daily_return_rate,
            return_status, provider, source_quote_id, source_hash, raw_json,
            updated_at
        )
        VALUES (
            %(trade_date)s, %(account_code)s, %(asset_key)s, %(asset_id)s, %(symbol)s, %(name)s,
            %(major_category)s, %(minor_category)s, %(currency)s, %(base_currency)s,
            %(quantity)s, %(previous_price)s, %(current_price)s, %(market_value_cny)s,
            %(daily_pnl_cny)s, %(total_pnl_cny)s, %(return_rate)s, %(daily_return_rate)s,
            %(return_status)s, %(provider)s, %(source_quote_id)s, %(source_hash)s, %(raw_json)s,
            now()
        )
        ON CONFLICT (trade_date, account_code, asset_key, base_currency)
        DO UPDATE SET
            asset_id = EXCLUDED.asset_id,
            symbol = EXCLUDED.symbol,
            name = EXCLUDED.name,
            major_category = EXCLUDED.major_category,
            minor_category = EXCLUDED.minor_category,
            currency = EXCLUDED.currency,
            quantity = EXCLUDED.quantity,
            previous_price = EXCLUDED.previous_price,
            current_price = EXCLUDED.current_price,
            market_value_cny = EXCLUDED.market_value_cny,
            daily_pnl_cny = EXCLUDED.daily_pnl_cny,
            total_pnl_cny = EXCLUDED.total_pnl_cny,
            return_rate = EXCLUDED.return_rate,
            daily_return_rate = EXCLUDED.daily_return_rate,
            return_status = EXCLUDED.return_status,
            provider = EXCLUDED.provider,
            source_quote_id = EXCLUDED.source_quote_id,
            source_hash = EXCLUDED.source_hash,
            raw_json = EXCLUDED.raw_json,
            updated_at = now()
        RETURNING *
        """,
        {**row, "raw_json": psycopg2.extras.Json(norm(row.get("raw_json") or {}))},
    )
    return cur.fetchone()
