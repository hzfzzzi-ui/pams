"""
价格刷新端点 - 从外部API获取最新价格并更新持仓数据

数据源：
- A股股票: 腾讯财经 qt.gtimg.cn (免费, 无需API key)
- A股/美股基金: 天天基金 fund.eastmoney.com (累计净值, 免费)
- 美股个股: yfinance / Yahoo Finance (免费)
- 加密: CoinGecko API (免费, 无需API key)

Symbol格式约定：
- sh601899 / sz002594 = A股个股 (腾讯财经)
- 008928 (6位纯数字) = 中国基金 (天天基金, 含QDII)
- AAPL / VOO = 美股个股 (yfinance)
- bitcoin / ethereum = 加密货币 (CoinGecko ID)
"""
import os
import re
import json as json_mod
import asyncio
import ssl
import time
import urllib.parse
import urllib.request
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from services.fund_nav_estimates import refresh_fund_nav_estimates as _refresh_fund_nav_estimates
from services.investment_view_date import daily_pnl_window_date as _daily_pnl_window_date
from services.price_history import (
    build_price_series_payload,
    ensure_price_history,
    normalize_price_instrument,
    parse_symbol_list,
)

logger = logging.getLogger("prices")

router = APIRouter()

_YFINANCE_CLOSE_CACHE: Dict[str, List[Tuple[date, float]]] = {}
_US_ESTIMATE_QUOTE_CACHE: Dict[str, dict[str, Any]] = {}
_IBKR_CONID_CACHE: Dict[str, str] = {}
_IBKR_TWS_CONNECTION: Any | None = None
_IBKR_TWS_CONNECTION_KEY: tuple[str, int, int] | None = None

_HIGH_CONFIDENCE_US_ESTIMATE_SOURCES = {
    "databento_estimate",
    "ibkr_tws_estimate",
    "ibkr_client_portal_estimate",
}

_A_STOCK_MAJOR_CATEGORY = "A\u80a1"
_A_STOCK_MINOR_CATEGORY = "A\u80a1"
_A_STOCK_FUND_MINOR_CATEGORY = "A\u80a1\u57fa\u91d1"
_OVERSEAS_MAJOR_CATEGORY = "\u7f8e\u80a1"


class InvestmentPriceRefreshRequest(BaseModel):
    """Manual public-market refresh request for investment holdings."""

    scopes: List[str] = Field(default_factory=lambda: ["a_stock", "us_stock", "a_fund", "precious_metal"])
    fund_use_intraday_estimate: Optional[bool] = None
    overseas_price_basis: Optional[str] = None


class PriceHistoryBackfillRequest(BaseModel):
    """Persist instrument price history for analysis curves and trade review."""

    symbols: List[str] = Field(default_factory=list)
    benchmarks: List[str] = Field(default_factory=list)
    start: date
    end: Optional[date] = None
    market: Optional[str] = None

# ---------- DB helpers ----------

def _get_conn():
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "pams-postgres"),
        port=int(os.environ.get("PGPORT", "5432")),
        dbname=os.environ.get("PGDATABASE", "pams"),
        user=os.environ.get("PGUSER", "pams"),
        password=os.environ.get("PGPASSWORD", ""),
    )


def _json(data, status_code=200):
    return JSONResponse(content=data, status_code=status_code)


def _today_cst():
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def _price_refresh_as_of(category: str, price_basis: str = "close") -> date:
    """Return the snapshot date for a price refresh.

    Overseas daily PnL rolls at 07:00 Beijing time because US market refreshes
    happen after local midnight. Writing US estimates/closes to the calendar
    date before that cutoff makes the holdings page compare the wrong bucket.
    """
    if category == _OVERSEAS_MAJOR_CATEGORY:
        return _daily_pnl_window_date()
    return _today_cst()


def _latest_investment_snapshot_as_of():
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT MAX(as_of) AS as_of
                FROM pams_holdings_snapshot
                WHERE major_category = ANY(%s::text[])
                """,
                (["A股", "美股", "贵金属", "ESOP"],),
            )
            row = cur.fetchone()
            return row["as_of"] if row else None


def _get_holdings_with_symbol(
    category: str,
    exclude_minor_categories: Tuple[str, ...] = (),
    market_buckets: Tuple[str, ...] = (),
) -> List[dict]:
    """查询指定大类中有symbol的持仓"""
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            excluded_sql = ""
            excluded_sql_h = ""
            params: List[object] = [category]
            if exclude_minor_categories:
                placeholders = ", ".join(["%s"] * len(exclude_minor_categories))
                excluded_sql = f"AND COALESCE(minor_category, '') NOT IN ({placeholders})"
                excluded_sql_h = f"AND COALESCE(h.minor_category, '') NOT IN ({placeholders})"
                params.extend(exclude_minor_categories)

            cte_params = list(params)
            select_params = [category]
            if exclude_minor_categories:
                select_params.extend(exclude_minor_categories)
            if _use_latest_account_snapshot_bucket(category, exclude_minor_categories):
                if category == _OVERSEAS_MAJOR_CATEGORY:
                    bucket_expr = _overseas_market_bucket_case()
                    h_bucket_expr = _overseas_market_bucket_case("h")
                    cur.execute(f"""
                        WITH latest_bucket AS (
                            SELECT
                                account_code,
                                asset_liability,
                                major_category,
                                minor_category,
                                {bucket_expr} AS market_bucket,
                                MAX(as_of) AS as_of
                            FROM pams_holdings_snapshot
                            WHERE major_category = %s
                              {excluded_sql}
                            GROUP BY account_code, asset_liability, major_category, minor_category, {bucket_expr}
                        )
                        SELECT
                            h.id, h.asset_id, h.symbol, h.name, h.asset_liability, h.currency, h.account_code,
                            h.major_category, h.minor_category, h.market_value, h.cost, h.as_of,
                            h.quantity, h.buy_price, h.current_price
                        FROM pams_holdings_snapshot h
                        JOIN latest_bucket lb
                          ON lb.account_code = h.account_code
                         AND lb.asset_liability = h.asset_liability
                         AND lb.major_category = h.major_category
                         AND lb.minor_category = h.minor_category
                         AND lb.market_bucket = {h_bucket_expr}
                         AND lb.as_of = h.as_of
                        WHERE h.major_category = %s
                          AND h.symbol IS NOT NULL AND h.symbol != ''
                          AND h.quantity IS NOT NULL AND h.quantity > 0
                          {excluded_sql_h}
                    """, [*cte_params, *select_params])
                else:
                    cur.execute(f"""
                        WITH latest_bucket AS (
                            SELECT
                                account_code,
                                asset_liability,
                                major_category,
                                minor_category,
                                MAX(as_of) AS as_of
                            FROM pams_holdings_snapshot
                            WHERE major_category = %s
                              {excluded_sql}
                            GROUP BY account_code, asset_liability, major_category, minor_category
                        )
                        SELECT
                            h.id, h.asset_id, h.symbol, h.name, h.asset_liability, h.currency, h.account_code,
                            h.major_category, h.minor_category, h.market_value, h.cost, h.as_of,
                            h.quantity, h.buy_price, h.current_price
                        FROM pams_holdings_snapshot h
                        JOIN latest_bucket lb
                          ON lb.account_code = h.account_code
                         AND lb.asset_liability = h.asset_liability
                         AND lb.major_category = h.major_category
                         AND lb.minor_category = h.minor_category
                         AND lb.as_of = h.as_of
                        WHERE h.major_category = %s
                          AND h.symbol IS NOT NULL AND h.symbol != ''
                          AND h.quantity IS NOT NULL AND h.quantity > 0
                          {excluded_sql_h}
                    """, [*cte_params, *select_params])
            else:
                cur.execute(f"""
                    WITH latest_position AS (
                        SELECT
                            account_code,
                            COALESCE(NULLIF(symbol, ''), name) AS position_key,
                            MAX(as_of) AS as_of
                        FROM pams_holdings_snapshot
                        WHERE major_category = %s
                          AND symbol IS NOT NULL AND symbol != ''
                          AND quantity IS NOT NULL AND quantity > 0
                          {excluded_sql}
                        GROUP BY account_code, COALESCE(NULLIF(symbol, ''), name)
                    )
                    SELECT
                        h.id, h.asset_id, h.symbol, h.name, h.asset_liability, h.currency, h.account_code,
                        h.major_category, h.minor_category, h.market_value, h.cost, h.as_of,
                        h.quantity, h.buy_price, h.current_price
                    FROM pams_holdings_snapshot h
                    JOIN latest_position lp
                      ON lp.account_code = h.account_code
                     AND lp.position_key = COALESCE(NULLIF(h.symbol, ''), h.name)
                     AND lp.as_of = h.as_of
                    WHERE h.major_category = %s
                      AND h.symbol IS NOT NULL AND h.symbol != ''
                      AND h.quantity IS NOT NULL AND h.quantity > 0
                      {excluded_sql}
                """, [*cte_params, *select_params])
            rows = [dict(row) for row in cur.fetchall()]
            if market_buckets:
                wanted = {str(item).strip().lower() for item in market_buckets if str(item).strip()}
                rows = [row for row in rows if _overseas_market_bucket(row) in wanted]
            return rows


def _use_latest_account_snapshot_bucket(category: str, exclude_minor_categories: Tuple[str, ...]) -> bool:
    """Broker snapshots are complete account buckets, not per-symbol feeds."""
    return (
        category == _A_STOCK_MAJOR_CATEGORY
        and _A_STOCK_FUND_MINOR_CATEGORY in set(exclude_minor_categories)
    ) or category == _OVERSEAS_MAJOR_CATEGORY


def _overseas_market_bucket(row: dict) -> str:
    """Classify overseas holdings for targeted refresh scheduling."""
    symbol = str(row.get("symbol") or "").strip().upper()
    currency = str(row.get("currency") or "").strip().upper()
    if currency == "KRW" or symbol.endswith(".KS") or symbol.endswith(".KQ"):
        return "kr"
    if currency == "HKD" or symbol.endswith(".HK") or (symbol.isdigit() and 1 <= len(symbol) <= 5):
        return "hk"
    if currency == "USD":
        return "us"
    return "other"


def _overseas_market_bucket_case(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    symbol = f"UPPER(COALESCE({prefix}symbol, ''))"
    currency = f"UPPER(COALESCE({prefix}currency, ''))"
    return f"""
        CASE
            WHEN {currency} = 'KRW' OR {symbol} LIKE '%%.KS' OR {symbol} LIKE '%%.KQ' THEN 'kr'
            WHEN {currency} = 'HKD' OR {symbol} LIKE '%%.HK'
              OR ({symbol} ~ '^[0-9]{{1,5}}$') THEN 'hk'
            WHEN {currency} = 'USD' THEN 'us'
            ELSE 'other'
        END
    """


def _normalize_overseas_market_filter(market: Optional[str]) -> Tuple[str, ...]:
    raw = str(market or "").strip().lower().replace("-", "_")
    if raw in {"", "all", "overseas"}:
        return ()
    aliases = {
        "kr": "kr",
        "korea": "kr",
        "korean": "kr",
        "kr_stock": "kr",
        "hk": "hk",
        "hong_kong": "hk",
        "hongkong": "hk",
        "hk_stock": "hk",
        "us": "us",
        "usa": "us",
        "us_stock": "us",
        "us_only": "us",
    }
    bucket = aliases.get(raw)
    return (bucket,) if bucket else ()


def _update_prices(updates: List[Tuple[float, float, int]]) -> int:
    """批量更新 current_price 和 market_value
    updates: [(current_price, market_value, id), ...]
    """
    if not updates:
        return 0

    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.executemany("""
                UPDATE pams_holdings_snapshot
                SET current_price = %s, market_value = %s
                WHERE id = %s
            """, updates)
            conn.commit()
            return len(updates)


def _dec(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _same_day_trade_symbol(symbol: Any) -> str:
    raw = str(symbol or "").strip()
    if raw.upper().startswith(("SSE:", "SZSE:")):
        return raw.split(":", 1)[1]
    if raw.lower().startswith(("sh", "sz", "bj")) and len(raw) >= 8:
        return raw[2:]
    return raw


def _is_a_stock_buy_trade(trade: dict) -> bool:
    trade_type = str(trade.get("trade_type") or "").lower()
    side = str(trade.get("side") or "").lower()
    return trade_type in {"buy", "fund_subscription"} or side == "buy"


def _is_a_stock_sell_trade(trade: dict) -> bool:
    trade_type = str(trade.get("trade_type") or "").lower()
    side = str(trade.get("side") or "").lower()
    return trade_type in {"sell", "fund_redemption"} or side == "sell"


def _trade_gross_amount(trade: dict, quantity: Decimal) -> Decimal:
    gross = _dec(trade.get("gross_amount"))
    if gross != 0:
        return abs(gross)
    price = _dec(trade.get("price"))
    return abs(quantity * price)


def _roll_forward_a_stock_position(base_row: dict, trades: list[dict]) -> dict[str, Decimal]:
    """Replay same-day A-share trades over one position snapshot."""
    quantity = _dec(base_row.get("quantity"))
    cost = _dec(base_row.get("cost"))

    for trade in trades:
        trade_qty = abs(_dec(trade.get("quantity")))
        if trade_qty <= 0:
            continue
        if _is_a_stock_buy_trade(trade):
            fees = abs(_dec(trade.get("fee_amount"))) + abs(_dec(trade.get("tax_amount")))
            quantity += trade_qty
            cost += _trade_gross_amount(trade, trade_qty) + fees
            continue
        if _is_a_stock_sell_trade(trade):
            if quantity <= 0:
                continue
            sell_qty = min(trade_qty, quantity)
            avg_cost = cost / quantity if quantity else Decimal("0")
            cost -= avg_cost * sell_qty
            quantity -= sell_qty
            if quantity <= Decimal("0.0000001"):
                quantity = Decimal("0")
                cost = Decimal("0")

    return {
        "quantity": quantity,
        "cost": cost,
        "buy_price": cost / quantity if quantity > 0 else Decimal("0"),
    }


def _row_matches_rollforward(row: dict, expected: dict[str, Decimal]) -> bool:
    qty_delta = abs(_dec(row.get("quantity")) - expected["quantity"])
    cost_delta = abs(_dec(row.get("cost")) - expected["cost"])
    return qty_delta <= Decimal("0.0001") and cost_delta <= Decimal("0.01")


def _apply_rollforward_to_row(row: dict, expected: dict[str, Decimal]) -> dict:
    adjusted = dict(row)
    adjusted["quantity"] = expected["quantity"]
    adjusted["cost"] = expected["cost"]
    adjusted["buy_price"] = expected["buy_price"]
    current_price = _dec(adjusted.get("current_price"))
    if current_price > 0:
        adjusted["market_value"] = expected["quantity"] * current_price
    adjusted["same_day_trade_adjusted"] = True
    return adjusted


def _previous_a_stock_snapshot_rows(cur, keys: set[tuple[str, str]], as_of) -> dict[tuple[str, str], dict]:
    if not keys:
        return {}
    accounts = sorted({account for account, _ in keys})
    symbols = sorted({symbol for _, symbol in keys})
    cur.execute(
        """
        SELECT DISTINCT ON (account_code, symbol)
            account_code, symbol, quantity, cost, buy_price, current_price, market_value, as_of
        FROM pams_holdings_snapshot
        WHERE major_category = %s
          AND minor_category = %s
          AND account_code = ANY(%s::text[])
          AND symbol = ANY(%s::text[])
          AND as_of < %s
          AND quantity IS NOT NULL
          AND quantity > 0
        ORDER BY account_code, symbol, as_of DESC, updated_at DESC, id DESC
        """,
        (_A_STOCK_MAJOR_CATEGORY, _A_STOCK_MINOR_CATEGORY, accounts, symbols, as_of),
    )
    return {
        (str(row["account_code"]), _same_day_trade_symbol(row["symbol"])): dict(row)
        for row in cur.fetchall()
    }


def _a_stock_trade_net_quantities_before(cur, keys: set[tuple[str, str]], as_of) -> dict[tuple[str, str], Decimal]:
    """Return pre-day net share quantities from confirmed trade facts."""
    if not keys:
        return {}
    cur.execute("SELECT to_regclass('public.pams_investment_trade_fact') IS NOT NULL AS exists")
    exists_row = cur.fetchone()
    exists = bool(exists_row.get("exists") if isinstance(exists_row, dict) else exists_row and exists_row[0])
    if not exists:
        return {}

    accounts = sorted({account for account, _ in keys})
    symbols = sorted({symbol for _, symbol in keys})
    cur.execute(
        """
        SELECT
            account_code,
            symbol,
            SUM(
                CASE
                    WHEN trade_type IN ('buy', 'fund_subscription')
                      OR LOWER(COALESCE(side, '')) = 'buy'
                        THEN ABS(COALESCE(quantity, 0))
                    WHEN trade_type IN ('sell', 'fund_redemption')
                      OR LOWER(COALESCE(side, '')) = 'sell'
                        THEN -ABS(COALESCE(quantity, 0))
                    ELSE 0
                END
            ) AS net_quantity
        FROM pams_investment_trade_fact
        WHERE trade_date < %s
          AND account_code = ANY(%s::text[])
          AND symbol = ANY(%s::text[])
          AND COALESCE(quantity, 0) > 0
          AND (
              trade_type IN ('buy', 'sell', 'fund_subscription', 'fund_redemption')
              OR LOWER(COALESCE(side, '')) IN ('buy', 'sell')
          )
        GROUP BY account_code, symbol
        """,
        (as_of, accounts, symbols),
    )
    return {
        (str(row["account_code"]), _same_day_trade_symbol(row["symbol"])): _dec(row["net_quantity"])
        for row in cur.fetchall()
    }


def _same_day_a_stock_trades(cur, keys: set[tuple[str, str]], as_of) -> dict[tuple[str, str], list[dict]]:
    if not keys:
        return {}
    cur.execute("SELECT to_regclass('public.pams_investment_trade_fact') IS NOT NULL AS exists")
    exists_row = cur.fetchone()
    exists = bool(exists_row.get("exists") if isinstance(exists_row, dict) else exists_row and exists_row[0])
    if not exists:
        return {}

    accounts = sorted({account for account, _ in keys})
    symbols = sorted({symbol for _, symbol in keys})
    cur.execute(
        """
        SELECT
            trade_id,
            account_code,
            symbol,
            trade_type,
            side,
            quantity,
            price,
            gross_amount,
            fee_amount,
            tax_amount
        FROM pams_investment_trade_fact
        WHERE trade_date = %s
          AND account_code = ANY(%s::text[])
          AND symbol = ANY(%s::text[])
          AND COALESCE(quantity, 0) > 0
          AND (
              trade_type IN ('buy', 'sell', 'fund_subscription', 'fund_redemption')
              OR LOWER(COALESCE(side, '')) IN ('buy', 'sell')
          )
        ORDER BY COALESCE(trade_time, trade_date::timestamp), trade_id
        """,
        (as_of, accounts, symbols),
    )
    grouped: dict[tuple[str, str], list[dict]] = {}
    for row in cur.fetchall():
        key = (str(row["account_code"]), _same_day_trade_symbol(row["symbol"]))
        grouped.setdefault(key, []).append(dict(row))
    return grouped


def _apply_same_day_a_stock_trades(rows: list[dict], as_of) -> list[dict]:
    """Roll A-share snapshots forward with same-day confirmed trades."""
    keys: set[tuple[str, str]] = set()
    for row in rows:
        if row.get("major_category") != _A_STOCK_MAJOR_CATEGORY:
            continue
        if row.get("minor_category") != _A_STOCK_MINOR_CATEGORY:
            continue
        symbol = _same_day_trade_symbol(row.get("symbol"))
        account = str(row.get("account_code") or "")
        if account and symbol:
            keys.add((account, symbol))
    if not keys:
        return rows

    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            previous_rows = _previous_a_stock_snapshot_rows(cur, keys, as_of)
            pre_day_quantities = _a_stock_trade_net_quantities_before(cur, keys, as_of)
            trades_by_key = _same_day_a_stock_trades(cur, keys, as_of)

    if not trades_by_key:
        return rows

    adjusted_rows: list[dict] = []
    for row in rows:
        key = (str(row.get("account_code") or ""), _same_day_trade_symbol(row.get("symbol")))
        trades = trades_by_key.get(key)
        if not trades:
            adjusted_rows.append(row)
            continue

        pre_day_quantity = pre_day_quantities.get(key)
        if pre_day_quantity is not None and pre_day_quantity <= 0:
            base_row = {"quantity": Decimal("0"), "cost": Decimal("0")}
        else:
            base_row = previous_rows.get(key)
        if base_row is None and str(row.get("as_of") or "")[:10] != str(as_of):
            base_row = row
        if base_row is None:
            adjusted_rows.append(row)
            continue

        expected = _roll_forward_a_stock_position(base_row, trades)
        if _row_matches_rollforward(row, expected):
            adjusted_rows.append(row)
        else:
            adjusted_rows.append(_apply_rollforward_to_row(row, expected))
    return adjusted_rows


def _upsert_price_snapshots(rows: List[dict], prices: Dict[str, float], as_of) -> int:
    """Copy the latest category snapshot to `as_of` with refreshed prices."""
    payload = []
    for row in rows:
        symbol = row["symbol"]
        if symbol not in prices:
            continue
        quantity = float(row["quantity"])
        current_price = float(prices[symbol])
        market_value = round(quantity * current_price, 2)
        payload.append((
            row.get("symbol"),
            row.get("name"),
            row.get("asset_liability") or "资产",
            row.get("currency") or "CNY",
            row.get("account_code"),
            row.get("major_category"),
            row.get("minor_category"),
            market_value,
            row.get("cost"),
            as_of,
            row.get("quantity"),
            row.get("buy_price"),
            current_price,
        ))

    if not payload:
        return 0

    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO pams_holdings_snapshot (
                    symbol, name, asset_liability, currency, account_code,
                    major_category, minor_category, market_value, cost, as_of,
                    quantity, buy_price, current_price
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (account_code, name, as_of)
                DO UPDATE SET
                    symbol = EXCLUDED.symbol,
                    asset_liability = EXCLUDED.asset_liability,
                    currency = EXCLUDED.currency,
                    major_category = EXCLUDED.major_category,
                    minor_category = EXCLUDED.minor_category,
                    market_value = EXCLUDED.market_value,
                    cost = EXCLUDED.cost,
                    quantity = EXCLUDED.quantity,
                    buy_price = EXCLUDED.buy_price,
                    current_price = EXCLUDED.current_price,
                    updated_at = now()
            """, payload)
            conn.commit()
            return len(payload)


def _upsert_cn_a_closes(rows: List[dict], prices: Dict[str, float], as_of) -> int:
    """Store refreshed A-share prices as same-day local close rows."""
    payload = []
    for row in rows:
        symbol = row.get("symbol")
        if symbol not in prices:
            continue
        quote_symbol = _a_stock_quote_symbol(str(symbol))
        if not quote_symbol:
            continue
        payload.append((quote_symbol, "CN_A", as_of, prices[symbol]))

    if not payload:
        return 0

    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO pams_ohlcv_daily (symbol, market, trade_date, close)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (symbol, market, trade_date)
                DO UPDATE SET close = EXCLUDED.close
            """, payload)
            conn.commit()
            return len(payload)


def _upsert_yfinance_closes(rows: List[dict]) -> int:
    """Store recent Yahoo Finance close rows for overseas daily PnL."""
    payload = []
    seen = set()
    for row in rows:
        symbol = row.get("symbol")
        if not symbol:
            continue
        for trade_date, close in _YFINANCE_CLOSE_CACHE.get(str(symbol), []):
            key = (str(symbol), "YF", trade_date)
            if key in seen:
                continue
            seen.add(key)
            payload.append((str(symbol), "YF", trade_date, close))

    if not payload:
        return 0

    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO pams_ohlcv_daily (symbol, market, trade_date, close)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (symbol, market, trade_date)
                DO UPDATE SET close = EXCLUDED.close
            """, payload)
            conn.commit()
            return len(payload)


def _estimate_quote_market(meta: dict[str, Any], symbol: str) -> str:
    source = str(meta.get("source") or "")
    if source == "databento_estimate":
        return "US_EXTENDED"
    if source == "ibkr_client_portal_estimate":
        return "US_EXTENDED"
    if source == "ibkr_tws_estimate":
        return "US_OVERNIGHT"
    raw_symbol = str(symbol or "").upper()
    if raw_symbol.endswith(".KS") or raw_symbol.endswith(".KQ"):
        return "KR_ESTIMATE"
    if raw_symbol.endswith(".HK") or raw_symbol.isdigit():
        return "HK_ESTIMATE"
    return "YAHOO_EXTENDED"


def _upsert_us_estimate_market_daily(rows: List[dict], prices: Dict[str, float], as_of) -> int:
    """Persist selected estimate quote marks for source-level auditability."""
    payload = []
    for row in rows:
        symbol = str(row.get("symbol") or "").strip()
        if not symbol:
            continue
        quoted_price = prices.get(symbol)
        if quoted_price is None:
            quoted_price = prices.get(symbol.upper())
        if quoted_price is None:
            continue
        cache_key = symbol.upper()
        meta = dict(_US_ESTIMATE_QUOTE_CACHE.get(cache_key) or {})
        source = str(meta.get("source") or "external_estimate")
        raw_json = {
            "price_basis": "estimate",
            "provider": meta.get("provider") or source,
            "session": meta.get("session"),
            "quote_type": meta.get("quote_type"),
            "as_of_utc": meta.get("as_of_utc"),
            "conid": meta.get("conid"),
            "raw": meta.get("raw"),
        }
        payload.append(
            (
                as_of,
                source,
                f"{source}:{symbol}:{as_of}",
                row.get("asset_id"),
                symbol,
                row.get("name"),
                _estimate_quote_market(meta, symbol),
                row.get("currency") or "USD",
                quoted_price,
                str(meta.get("confidence") or "medium"),
                psycopg2.extras.Json(raw_json),
            )
        )

    if not payload:
        return 0

    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO pams_asset_market_daily (
                    as_of, source, source_key, asset_id, symbol, name, market,
                    currency, close_price, confidence, raw_json
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (as_of, source, source_key)
                DO UPDATE SET
                    asset_id = EXCLUDED.asset_id,
                    symbol = EXCLUDED.symbol,
                    name = EXCLUDED.name,
                    market = EXCLUDED.market,
                    currency = EXCLUDED.currency,
                    close_price = EXCLUDED.close_price,
                    confidence = EXCLUDED.confidence,
                    raw_json = pams_asset_market_daily.raw_json || EXCLUDED.raw_json,
                    updated_at = now()
                """,
                payload,
            )
            conn.commit()
            return len(payload)


def _estimate_quote_source_summary(symbols: List[str]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for symbol in symbols:
        meta = _US_ESTIMATE_QUOTE_CACHE.get(str(symbol or "").strip().upper()) or {}
        source = str(meta.get("source") or "untracked")
        summary[source] = summary.get(source, 0) + 1
    return summary


def _price_key_for_symbol(prices: Dict[str, float], symbol: Any) -> str | None:
    raw = str(symbol or "").strip()
    if not raw:
        return None
    if raw in prices:
        return raw
    upper = raw.upper()
    if upper in prices:
        return upper
    return None


def _is_ibkr_account(account_code: Any) -> bool:
    raw = str(account_code or "").strip().lower()
    return raw in {"ibkr", "interactive_brokers", "interactivebrokers"}


def _is_high_confidence_us_estimate_source(symbol: Any) -> bool:
    meta = _US_ESTIMATE_QUOTE_CACHE.get(str(symbol or "").strip().upper()) or {}
    source = str(meta.get("source") or "")
    return source in _HIGH_CONFIDENCE_US_ESTIMATE_SOURCES


def _filter_us_estimate_prices_for_holdings(
    rows: List[dict],
    prices: Dict[str, float],
) -> tuple[Dict[str, float], list[dict[str, Any]]]:
    """IBKR holdings need high-confidence all-hours marks, not fallback quotes."""
    filtered = dict(prices)
    skipped: list[dict[str, Any]] = []
    for row in rows:
        symbol = str(row.get("symbol") or "").strip()
        price_key = _price_key_for_symbol(filtered, symbol)
        if price_key is None:
            continue
        if not _is_ibkr_account(row.get("account_code")):
            continue
        if _is_high_confidence_us_estimate_source(symbol):
            corrected = _correct_ibkr_estimate_scale(
                symbol=symbol,
                price=filtered[price_key],
                reference_price=row.get("current_price"),
            )
            if corrected is not None and corrected != filtered[price_key]:
                filtered[price_key] = corrected
                if symbol in filtered:
                    filtered[symbol] = corrected
                if symbol.upper() in filtered:
                    filtered[symbol.upper()] = corrected
            continue
        meta = _US_ESTIMATE_QUOTE_CACHE.get(symbol.upper()) or {}
        filtered.pop(price_key, None)
        filtered.pop(symbol, None)
        filtered.pop(symbol.upper(), None)
        skipped.append(
            {
                "symbol": symbol,
                "name": row.get("name"),
                "account_code": row.get("account_code"),
                "source": meta.get("source") or "untracked",
                "reason": "ibkr_estimate_requires_high_confidence_quote",
            }
        )
    return filtered, skipped


def _correct_ibkr_estimate_scale(
    *,
    symbol: str,
    price: Any,
    reference_price: Any,
) -> float | None:
    """Correct obvious IBKR fixed-point quote scale drift.

    If a quote arrives one or two decimal places away from the latest holding
    price, use the holding price as a sanity anchor and only correct when a
    decimal shift brings the quote back into a plausible range.
    """
    try:
        value = float(price)
        reference = float(reference_price)
    except (TypeError, ValueError):
        return None
    if value <= 0 or reference <= 0:
        return None

    def plausible(candidate: float) -> bool:
        ratio = candidate / reference
        return 0.35 <= ratio <= 2.75

    if plausible(value):
        return value

    candidates = [value / 10, value / 100, value * 10, value * 100]
    for candidate in candidates:
        if candidate > 0 and plausible(candidate):
            fixed = round(candidate, 4)
            meta = _US_ESTIMATE_QUOTE_CACHE.setdefault(str(symbol or "").strip().upper(), {})
            meta["price"] = fixed
            meta["scale_correction"] = {
                "original_price": value,
                "corrected_price": fixed,
                "reference_price": reference,
                "method": "latest_holding_price_sanity",
            }
            return fixed
    return value


def _latest_stored_yfinance_prices(
    symbols: List[str],
    *,
    as_of,
    fallback: Dict[str, float] | None = None,
) -> Dict[str, float]:
    """Return the latest stored Yahoo close for each symbol on or before as_of.

    Yahoo's quote endpoints can expose moving or stale quote prices around
    overseas market sessions. PAMS displays overseas holdings on a close-price
    basis, so valuation snapshots should prefer persisted OHLCV closes.
    """
    prices: Dict[str, float] = dict(fallback or {})
    clean_symbols = [str(s) for s in symbols if s]
    if not clean_symbols:
        return prices

    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (symbol) symbol, close
                FROM pams_ohlcv_daily
                WHERE market = 'YF'
                  AND symbol = ANY(%s::text[])
                  AND trade_date <= %s
                  AND close IS NOT NULL
                ORDER BY symbol, trade_date DESC
                """,
                (clean_symbols, as_of),
            )
            for row in cur.fetchall():
                close = row.get("close")
                if close is None:
                    continue
                try:
                    value = float(close)
                except (TypeError, ValueError):
                    continue
                if value > 0:
                    prices[str(row["symbol"])] = round(value, 6)
    return prices


# ---------- A股个股价格 (腾讯财经) ----------

def _fetch_stock_prices_tencent(symbols: List[str]) -> Dict[str, float]:
    """从腾讯财经API批量获取A股个股价格
    symbols: ['sh600519', 'sz000858', ...]
    返回: {'sh600519': 1855.02, ...}
    """
    if not symbols:
        return {}

    url = f"https://qt.gtimg.cn/q={','.join(symbols)}"
    req = urllib.request.Request(url, headers={"User-Agent": "PAMS/2.0"})

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode("gbk", errors="replace")
    except Exception as e:
        logger.error("腾讯财经API请求失败: %s", e)
        return {}

    prices = {}
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line or "=" not in line:
            continue
        # v_sh600519="1~贵州茅台~600519~1855.02~..."
        try:
            var_name, value = line.split("=", 1)
            symbol = var_name.replace("v_", "")
            fields = value.strip('"').strip(";").split("~")
            if len(fields) > 3 and fields[3]:
                price = float(fields[3])
                if price > 0:
                    prices[symbol] = price
        except (ValueError, IndexError) as e:
            logger.warning("解析A股行情失败 [%s]: %s", line[:50], e)

    return prices


# ---------- 基金累计净值 (天天基金 Eastmoney) ----------

def _fetch_fund_navs(codes: List[str]) -> Dict[str, float]:
    """从天天基金获取基金累计净值
    codes: ['008928', '011613', ...]
    返回: {'008928': 1.2345, ...}

    使用 F10DataApi 获取最新一期的累计净值(ljjz)
    响应格式: var apidata={content:"<table>...<td>日期</td><td>单位净值</td><td>累计净值</td>...",records:N,...}
    """
    if not codes:
        return {}

    prices = {}
    for code in codes:
        url = (
            f"https://fund.eastmoney.com/f10/F10DataApi.aspx"
            f"?type=lsjz&code={code}&page=1&per=1"
        )
        req = urllib.request.Request(url, headers={
            "User-Agent": "PAMS/2.0",
            "Referer": "https://fund.eastmoney.com/",
        })

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                text = resp.read().decode("utf-8", errors="replace")

            # 从HTML表格中提取累计净值
            # tbody中第一个tr的td顺序: 日期, 单位净值, 累计净值, 日增长率, ...
            tds = re.findall(r"<td[^>]*>(.*?)</td>", text)
            if len(tds) >= 3:
                ljjz = float(tds[2])  # 第3个td = 累计净值
                if ljjz > 0:
                    prices[code] = ljjz
                    logger.info("基金 %s 累计净值: %s", code, ljjz)
        except Exception as e:
            logger.warning("获取基金 %s 累计净值失败: %s", code, e)

    return prices


def _is_fund_code(symbol: str) -> bool:
    """判断是否为基金代码 (6位纯数字)"""
    return bool(symbol and len(symbol) == 6 and symbol.isdigit())


def _a_stock_quote_symbol(symbol: str) -> str | None:
    """Return Tencent quote symbol for an A-share stock code."""
    if not symbol:
        return None
    symbol = symbol.strip()
    if symbol.startswith(("sh", "sz", "bj")):
        return symbol
    if len(symbol) == 6 and symbol.isdigit():
        if symbol.startswith(("5", "6", "9")):
            return f"sh{symbol}"
        if symbol.startswith(("0", "1", "2", "3")):
            return f"sz{symbol}"
    return None


# ---------- A股综合 (个股 + 基金) ----------

def _fetch_a_stock_prices(symbols: List[str]) -> Dict[str, float]:
    """获取A股价格，自动区分个股和基金
    - sh/sz开头 → 腾讯财经 (个股实时价格)
    - 6位数字 → 天天基金 (基金累计净值)
    """
    stock_symbol_map = {
        original: quote_symbol
        for original in symbols
        if (quote_symbol := _a_stock_quote_symbol(original))
    }
    fund_codes = [
        s for s in symbols
        if _is_fund_code(s) and s not in stock_symbol_map
    ]

    prices = {}
    if stock_symbol_map:
        quote_prices = _fetch_stock_prices_tencent(list(stock_symbol_map.values()))
        prices.update({
            original: quote_prices[quote_symbol]
            for original, quote_symbol in stock_symbol_map.items()
            if quote_symbol in quote_prices
        })
    if fund_codes:
        prices.update(_fetch_fund_navs(fund_codes))

    return prices


# ---------- 美股价格获取 (yfinance) ----------

def _yfinance_quote_symbol(symbol: str) -> str:
    """Return a yfinance-compatible symbol for overseas holdings."""
    raw = (symbol or "").strip()
    if raw.isdigit() and 1 <= len(raw) <= 5:
        return f"{raw.zfill(4)}.HK"
    return raw


def _positive_quote_price(value) -> float | None:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def _looks_like_us_stock_symbol(symbol: str) -> bool:
    raw = str(symbol or "").strip().upper()
    if not raw or raw.startswith("CASH_"):
        return False
    if raw.endswith((".HK", ".KS", ".KQ")):
        return False
    if raw.isdigit():
        return False
    return bool(re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", raw))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _databento_api_key() -> str | None:
    raw = os.environ.get("PAMS_DATABENTO_API_KEY") or os.environ.get("DATABENTO_API_KEY") or ""
    key = raw.strip()
    return key or None


def _databento_enabled() -> bool:
    configured = _databento_api_key() is not None
    return _env_bool("PAMS_DATABENTO_ENABLED", configured)


def _databento_dataset() -> str:
    return (os.environ.get("PAMS_DATABENTO_DATASET") or "DBEQ.BASIC").strip() or "DBEQ.BASIC"


def _databento_schema() -> str:
    return (os.environ.get("PAMS_DATABENTO_SCHEMA") or "trades").strip() or "trades"


def _databento_lookback_minutes() -> int:
    raw = os.environ.get("PAMS_DATABENTO_LOOKBACK_MINUTES", "1440")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 1440
    return min(max(value, 15), 4320)


def _databento_symbol(symbol: str) -> str:
    """Return Databento raw_symbol input for a PAMS US ticker."""
    return str(symbol or "").strip().upper().replace("-", ".")


def _databento_json_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    return str(value)


def _databento_prices_from_frame(frame: Any, requested_symbols: list[str]) -> dict[str, tuple[float, dict[str, Any]]]:
    if frame is None or getattr(frame, "empty", False):
        return {}

    try:
        table = frame.reset_index()
    except Exception:
        table = frame

    columns = [str(col) for col in getattr(table, "columns", [])]
    symbol_col = next((col for col in ("symbol", "raw_symbol", "stype_in") if col in columns), None)
    price_col = next(
        (col for col in ("price", "close", "last_price", "last") if col in columns),
        None,
    )
    if price_col is None:
        logger.info("Databento quote skipped: no supported price column in %s", columns)
        return {}
    if symbol_col is None and len(requested_symbols) != 1:
        logger.info("Databento quote skipped: multi-symbol response missing symbol column")
        return {}

    time_col = next(
        (col for col in ("ts_event", "ts_recv", "time", "timestamp", "index") if col in columns),
        None,
    )
    if time_col is not None:
        try:
            table = table.sort_values(time_col)
        except Exception:
            pass

    requested_by_raw = {_databento_symbol(symbol): str(symbol).strip().upper() for symbol in requested_symbols}
    latest: dict[str, tuple[float, dict[str, Any]]] = {}
    for _, row in table.iterrows():
        if symbol_col is None:
            raw_symbol = _databento_symbol(requested_symbols[0])
        else:
            raw_symbol = _databento_symbol(row.get(symbol_col))
        symbol = requested_by_raw.get(raw_symbol)
        if not symbol:
            continue
        price = _positive_quote_price(row.get(price_col))
        if price is None:
            continue
        raw_row = {
            str(key): _databento_json_safe(value)
            for key, value in row.to_dict().items()
        }
        latest[symbol] = (
            round(price, 4),
            {
                "price_column": price_col,
                "time_column": time_col,
                "event_time": _databento_json_safe(row.get(time_col)) if time_col else None,
                "raw_row": raw_row,
            },
        )
    return latest


def _fetch_databento_us_estimate_prices(symbols: List[str]) -> Dict[str, float]:
    """Fetch recent US all-hours marks from Databento for estimate PnL."""
    wanted = [str(sym).strip().upper() for sym in symbols if _looks_like_us_stock_symbol(str(sym))]
    if not wanted or not _databento_enabled():
        return {}
    api_key = _databento_api_key()
    if not api_key:
        logger.info("Databento quote skipped: DATABENTO_API_KEY is not configured")
        return {}

    try:
        import databento as db
    except Exception as exc:
        logger.info("Databento quote skipped: databento package unavailable: %s", exc)
        return {}

    dataset = _databento_dataset()
    schema = _databento_schema()
    end_utc = datetime.now(ZoneInfo("UTC"))
    start_utc = end_utc - timedelta(minutes=_databento_lookback_minutes())
    raw_symbols = [_databento_symbol(symbol) for symbol in wanted]

    try:
        client = db.Historical(key=api_key)
        response = client.timeseries.get_range(
            dataset=dataset,
            schema=schema,
            symbols=raw_symbols,
            stype_in="raw_symbol",
            start=start_utc.isoformat(),
            end=end_utc.isoformat(),
        )
        frame = response.to_df(price_type="float")
    except Exception as exc:
        logger.info("Databento quote skipped: %s", exc)
        return {}

    extracted = _databento_prices_from_frame(frame, wanted)
    now_utc = end_utc.isoformat()
    prices: Dict[str, float] = {}
    for symbol, (price, meta) in extracted.items():
        prices[symbol] = price
        _US_ESTIMATE_QUOTE_CACHE[symbol] = {
            "source": "databento_estimate",
            "provider": "databento",
            "session": "all_hours",
            "quote_type": f"{dataset}:{schema}:{meta.get('price_column')}",
            "confidence": "high",
            "price": price,
            "as_of_utc": now_utc,
            "raw": {
                "dataset": dataset,
                "schema": schema,
                "symbols": raw_symbols,
                "start": start_utc.isoformat(),
                "end": end_utc.isoformat(),
                "event_time": meta.get("event_time"),
                "row": meta.get("raw_row"),
            },
        }
    return prices


def _ibkr_client_portal_base_url() -> str | None:
    raw = (
        os.environ.get("PAMS_IBKR_CP_API_BASE_URL")
        or os.environ.get("IBKR_CP_API_BASE_URL")
        or ""
    ).strip()
    if raw.lower() in {"", "0", "false", "none", "disabled", "off"}:
        return None
    return raw.rstrip("/")


def _ibkr_client_portal_timeout() -> float:
    raw = os.environ.get("PAMS_IBKR_CP_TIMEOUT_SECONDS") or os.environ.get("IBKR_CP_TIMEOUT_SECONDS") or "2"
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        return 2.0
    return min(max(timeout, 0.5), 15.0)


def _ibkr_client_portal_verify_ssl() -> bool:
    raw = (
        os.environ.get("PAMS_IBKR_CP_VERIFY_SSL")
        or os.environ.get("IBKR_CP_VERIFY_SSL")
        or "false"
    )
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _ibkr_client_portal_request_json(
    path: str,
    *,
    method: str = "GET",
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> Any:
    base_url = _ibkr_client_portal_base_url()
    if not base_url:
        raise RuntimeError("IBKR Client Portal base URL is not configured")
    url = f"{base_url}/{path.lstrip('/')}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
    data = None
    headers = {
        "Accept": "application/json",
        "User-Agent": "PAMS/2.0",
    }
    if payload is not None:
        data = json_mod.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    context = None
    if url.lower().startswith("https://") and not _ibkr_client_portal_verify_ssl():
        context = ssl._create_unverified_context()
    with urllib.request.urlopen(req, timeout=_ibkr_client_portal_timeout(), context=context) as resp:
        text = resp.read().decode("utf-8", errors="replace")
    if not text.strip():
        return None
    return json_mod.loads(text)


def _ibkr_numeric(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        return _positive_quote_price(value)
    text = str(value).strip()
    if not text or text in {"-", "--", "N/A", "NaN"}:
        return None
    match = re.search(r"-?\d[\d,]*(?:\.\d+)?", text)
    if not match:
        return None
    return _positive_quote_price(match.group(0).replace(",", ""))


def _ibkr_conid_from_env(symbol: str) -> str | None:
    key = str(symbol or "").strip().upper()
    if not key:
        return None
    raw_json = os.environ.get("PAMS_IBKR_CONIDS_JSON") or os.environ.get("IBKR_CONIDS_JSON")
    if raw_json:
        try:
            mapping = json_mod.loads(raw_json)
        except Exception:
            mapping = {}
        if isinstance(mapping, dict):
            value = mapping.get(key) or mapping.get(key.lower())
            if value:
                return str(value).strip()
    env_key = f"PAMS_IBKR_CONID_{re.sub(r'[^A-Z0-9]', '_', key)}"
    value = os.environ.get(env_key)
    return str(value).strip() if value else None


def _ibkr_select_contract(symbol: str, response: Any) -> str | None:
    if isinstance(response, dict):
        candidates = response.get("contracts") or response.get("data") or response.get("results") or []
    elif isinstance(response, list):
        candidates = response
    else:
        candidates = []
    clean_symbol = str(symbol or "").strip().upper()

    def candidate_score(item: dict[str, Any]) -> tuple[int, str]:
        item_symbol = str(item.get("symbol") or item.get("ticker") or "").strip().upper()
        asset_class = str(item.get("assetClass") or item.get("secType") or item.get("sectype") or "").strip().upper()
        currency = str(item.get("currency") or "").strip().upper()
        description = str(item.get("description") or item.get("companyName") or item.get("companyHeader") or "")
        score = 0
        if item_symbol == clean_symbol:
            score -= 20
        if asset_class in {"STK", "STOCK"}:
            score -= 10
        if currency == "USD":
            score -= 5
        if "OPT" in asset_class or "OPTION" in description.upper():
            score += 25
        return score, str(item.get("conid") or "")

    usable = [item for item in candidates if isinstance(item, dict) and item.get("conid")]
    if not usable:
        return None
    usable.sort(key=candidate_score)
    return str(usable[0].get("conid")).strip()


def _ibkr_conid(symbol: str) -> str | None:
    clean_symbol = str(symbol or "").strip().upper()
    if not clean_symbol:
        return None
    cached = _IBKR_CONID_CACHE.get(clean_symbol)
    if cached:
        return cached
    configured = _ibkr_conid_from_env(clean_symbol)
    if configured:
        _IBKR_CONID_CACHE[clean_symbol] = configured
        return configured
    try:
        response = _ibkr_client_portal_request_json(
            "iserver/secdef/search",
            method="POST",
            params={"symbol": clean_symbol, "secType": "STK"},
        )
    except Exception as exc:
        logger.info("IBKR secdef search skipped for %s: %s", clean_symbol, exc)
        return None
    conid = _ibkr_select_contract(clean_symbol, response)
    if conid:
        _IBKR_CONID_CACHE[clean_symbol] = conid
    return conid


def _ibkr_quote_price(row: dict[str, Any]) -> tuple[float | None, str | None]:
    last = _ibkr_numeric(row.get("31") or row.get("last") or row.get("lastPrice") or row.get("Last Price"))
    if last is not None:
        return round(last, 4), "last"
    bid = _ibkr_numeric(row.get("84") or row.get("bid") or row.get("bidPrice"))
    ask = _ibkr_numeric(row.get("86") or row.get("ask") or row.get("askPrice"))
    if bid is not None and ask is not None:
        return round((bid + ask) / 2, 4), "bid_ask_mid"
    if bid is not None:
        return round(bid, 4), "bid"
    if ask is not None:
        return round(ask, 4), "ask"
    close = _ibkr_numeric(row.get("7296") or row.get("close") or row.get("Close"))
    if close is not None:
        return round(close, 4), "close"
    return None, None


def _ibkr_snapshot_rows(response: Any) -> list[dict[str, Any]]:
    if isinstance(response, list):
        return [item for item in response if isinstance(item, dict)]
    if isinstance(response, dict):
        for key in ("data", "quotes", "results"):
            value = response.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return [response]
    return []


def _fetch_ibkr_client_portal_estimate_prices(symbols: List[str]) -> Dict[str, float]:
    """Fetch live/overnight marks from the local IBKR Client Portal Gateway."""
    if not _ibkr_client_portal_base_url():
        return {}
    wanted = [str(sym).strip().upper() for sym in symbols if _looks_like_us_stock_symbol(str(sym))]
    if not wanted:
        return {}

    try:
        _ibkr_client_portal_request_json("iserver/accounts")
    except Exception as exc:
        logger.info("IBKR Client Portal quote skipped: %s", exc)
        return {}

    conid_by_symbol: dict[str, str] = {}
    for symbol in wanted:
        conid = _ibkr_conid(symbol)
        if conid:
            conid_by_symbol[symbol] = conid
    if not conid_by_symbol:
        return {}

    symbol_by_conid = {conid: symbol for symbol, conid in conid_by_symbol.items()}
    fields = os.environ.get("PAMS_IBKR_CP_SNAPSHOT_FIELDS", "31,84,86,6509,7059,7296")
    params = {
        "conids": ",".join(conid_by_symbol.values()),
        "fields": fields,
        "outsideRth": "true",
    }

    rows: list[dict[str, Any]] = []
    for attempt in range(2):
        try:
            response = _ibkr_client_portal_request_json("iserver/marketdata/snapshot", params=params)
            rows = _ibkr_snapshot_rows(response)
        except Exception as exc:
            logger.info("IBKR marketdata snapshot skipped: %s", exc)
            return {}
        if rows or attempt == 1:
            break
        time.sleep(0.25)

    prices: Dict[str, float] = {}
    now_utc = datetime.now(ZoneInfo("UTC")).isoformat()
    for row in rows:
        conid = str(row.get("conid") or row.get("conidEx") or "").split("@", 1)[0]
        symbol = symbol_by_conid.get(conid)
        if not symbol:
            raw_symbol = str(row.get("symbol") or "").strip().upper()
            symbol = raw_symbol if raw_symbol in conid_by_symbol else None
        if not symbol:
            continue
        price, quote_type = _ibkr_quote_price(row)
        if price is None:
            continue
        prices[symbol] = price
        _US_ESTIMATE_QUOTE_CACHE[symbol] = {
            "source": "ibkr_client_portal_estimate",
            "provider": "ibkr_client_portal",
            "session": "extended_or_overnight",
            "quote_type": quote_type,
            "confidence": "high",
            "conid": conid_by_symbol.get(symbol),
            "price": price,
            "as_of_utc": now_utc,
            "raw": row,
        }
    return prices


def _ibkr_tws_enabled() -> bool:
    raw = os.environ.get("PAMS_IBKR_TWS_ENABLED", "true")
    return str(raw).strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _ibkr_tws_persistent_enabled() -> bool:
    raw = os.environ.get("PAMS_IBKR_TWS_PERSISTENT", "true")
    return str(raw).strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _ibkr_tws_ports() -> list[int]:
    raw = os.environ.get("PAMS_IBKR_TWS_PORTS") or os.environ.get("IBKR_TWS_PORTS") or "4000,4001,4002,7497,7496"
    ports: list[int] = []
    for token in re.split(r"[,;\s]+", raw):
        if not token:
            continue
        try:
            port = int(token)
        except ValueError:
            continue
        if port > 0 and port not in ports:
            ports.append(port)
    return ports or [4000, 4001, 4002, 7497, 7496]


def _ibkr_tws_float_env(name: str, default: float, *, min_value: float, max_value: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return min(max(value, min_value), max_value)


def _ibkr_tws_client_id() -> int:
    try:
        return int(os.environ.get("PAMS_IBKR_TWS_CLIENT_ID", "62017"))
    except (TypeError, ValueError):
        return 62017


def _ensure_ibkr_tws_event_loop() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


def _ibkr_tws_is_connected(ib: Any) -> bool:
    try:
        checker = getattr(ib, "isConnected", None)
        if callable(checker):
            return bool(checker())
        return bool(getattr(ib, "is_connected", False))
    except Exception:
        return False


def _disconnect_ibkr_tws(ib: Any) -> None:
    try:
        ib.disconnect()
    except Exception:
        pass


def _get_ibkr_tws_connection(
    ib_factory,
    *,
    host: str,
    ports: list[int],
    client_id: int,
    timeout: float,
):
    global _IBKR_TWS_CONNECTION, _IBKR_TWS_CONNECTION_KEY

    if _ibkr_tws_persistent_enabled() and _IBKR_TWS_CONNECTION is not None:
        key = _IBKR_TWS_CONNECTION_KEY
        if key and key[0] == host and key[2] == client_id and key[1] in ports:
            if _ibkr_tws_is_connected(_IBKR_TWS_CONNECTION):
                return _IBKR_TWS_CONNECTION, key[1], True
        _disconnect_ibkr_tws(_IBKR_TWS_CONNECTION)
        _IBKR_TWS_CONNECTION = None
        _IBKR_TWS_CONNECTION_KEY = None

    ib = ib_factory()
    connected_port: int | None = None
    for port in ports:
        try:
            ib.connect(host, port, clientId=client_id, timeout=timeout, readonly=True)
            connected_port = port
            break
        except Exception as exc:
            logger.info("IBKR TWS connect skipped on %s:%s: %s", host, port, exc)
    if connected_port is None:
        _disconnect_ibkr_tws(ib)
        return None, None, False

    if _ibkr_tws_persistent_enabled():
        _IBKR_TWS_CONNECTION = ib
        _IBKR_TWS_CONNECTION_KEY = (host, connected_port, client_id)
        return ib, connected_port, True

    return ib, connected_port, False


def _ibkr_tws_primary_exchange(symbol: str, qualified_contract: Any | None = None) -> str:
    raw_json = os.environ.get("PAMS_IBKR_TWS_PRIMARY_EXCHANGE_JSON") or ""
    mapping: dict[str, Any] = {}
    if raw_json:
        try:
            parsed = json_mod.loads(raw_json)
            if isinstance(parsed, dict):
                mapping = parsed
        except Exception:
            mapping = {}
    clean_symbol = str(symbol or "").strip().upper()
    configured = mapping.get(clean_symbol) or mapping.get(clean_symbol.lower())
    if configured:
        return str(configured).strip().upper()
    primary = str(getattr(qualified_contract, "primaryExchange", "") or "").strip().upper()
    if primary:
        return primary
    if clean_symbol in {"PLTR"}:
        return "NASDAQ"
    return "NASDAQ"


def _ibkr_tws_use_overnight() -> bool:
    raw = os.environ.get("PAMS_IBKR_TWS_USE_OVERNIGHT", "true")
    return str(raw).strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _ibkr_estimate_fallback_enabled() -> bool:
    return _env_bool("PAMS_IBKR_ESTIMATE_FALLBACK_ENABLED", False)


def _ibkr_tws_ticker_price(ticker: Any) -> tuple[float | None, str | None]:
    for attr, label in (
        ("last", "last"),
        ("marketPrice", "market_price"),
        ("close", "close"),
        ("bid", "bid"),
        ("ask", "ask"),
    ):
        try:
            value = getattr(ticker, attr)
            if callable(value):
                value = value()
        except Exception:
            value = None
        price = _positive_quote_price(value)
        if price is not None:
            price = _normalize_ibkr_tws_price(price)
            return round(price, 4), label
    try:
        bid = _positive_quote_price(getattr(ticker, "bid", None))
        ask = _positive_quote_price(getattr(ticker, "ask", None))
    except Exception:
        bid, ask = None, None
    if bid is not None and ask is not None:
        return round(_normalize_ibkr_tws_price((bid + ask) / 2), 4), "bid_ask_mid"
    return None, None


def _normalize_ibkr_tws_price(price: float) -> float:
    """Normalize IBKR overnight ticks that arrive as fixed-point integers."""
    value = float(price)
    if value >= 100000:
        value = value / 10000
    return value


def _ibkr_tws_raw_value(value: Any) -> float | str | None:
    price = _positive_quote_price(value)
    if price is not None:
        return price
    if value is None:
        return None
    text = str(value)
    if text.lower() in {"nan", "inf", "-inf", "none"}:
        return None
    return text


def _fetch_ibkr_tws_estimate_prices(symbols: List[str]) -> Dict[str, float]:
    """Fetch IBKR Desktop/TWS live or overnight marks through the socket API."""
    if not _ibkr_tws_enabled():
        return {}
    wanted = [str(sym).strip().upper() for sym in symbols if _looks_like_us_stock_symbol(str(sym))]
    if not wanted:
        return {}
    _ensure_ibkr_tws_event_loop()
    try:
        from ib_insync import IB, Stock
    except Exception as exc:
        logger.info("IBKR TWS quote skipped: ib_insync unavailable: %s", exc)
        return {}

    host = os.environ.get("PAMS_IBKR_TWS_HOST") or os.environ.get("IBKR_TWS_HOST") or "host.docker.internal"
    timeout = _ibkr_tws_float_env("PAMS_IBKR_TWS_TIMEOUT_SECONDS", 2.0, min_value=0.5, max_value=15.0)
    snapshot_wait = _ibkr_tws_float_env("PAMS_IBKR_TWS_SNAPSHOT_WAIT_SECONDS", 1.5, min_value=0.2, max_value=10.0)
    market_data_type = int(os.environ.get("PAMS_IBKR_TWS_MARKET_DATA_TYPE", "1") or "1")

    ib, connected_port, persistent_connection = _get_ibkr_tws_connection(
        IB,
        host=host,
        ports=_ibkr_tws_ports(),
        client_id=_ibkr_tws_client_id(),
        timeout=timeout,
    )
    if connected_port is None:
        return {}

    prices: Dict[str, float] = {}
    now_utc = datetime.now(ZoneInfo("UTC")).isoformat()
    tickers: list[tuple[str, Any, str, Any]] = []
    try:
        try:
            ib.reqMarketDataType(market_data_type)
        except Exception as exc:
            logger.info("IBKR TWS market data type skipped: %s", exc)

        for symbol in wanted:
            try:
                smart_contract = Stock(symbol, "SMART", "USD")
                qualified = ib.qualifyContracts(smart_contract)
                base_contract = qualified[0] if qualified else smart_contract
                if _ibkr_tws_use_overnight():
                    primary = _ibkr_tws_primary_exchange(symbol, base_contract)
                    contract = Stock(symbol, "OVERNIGHT", "USD", primaryExchange=primary)
                    qualified_overnight = ib.qualifyContracts(contract)
                    contract = qualified_overnight[0] if qualified_overnight else contract
                    session = "overnight"
                else:
                    contract = base_contract
                    session = "live_or_extended"
                ticker = ib.reqMktData(contract, "", True, False)
                tickers.append((symbol, contract, session, ticker))
            except Exception as exc:
                logger.info("IBKR TWS quote request skipped for %s: %s", symbol, exc)

        if tickers:
            ib.sleep(snapshot_wait)

        for symbol, contract, session, ticker in tickers:
            if ticker is None:
                try:
                    ticker = ib.reqTickers(contract)[0]
                except Exception as exc:
                    logger.info("IBKR TWS ticker read skipped for %s: %s", symbol, exc)
                    continue
            price, quote_type = _ibkr_tws_ticker_price(ticker)
            if price is None:
                continue
            prices[symbol] = price
            _US_ESTIMATE_QUOTE_CACHE[symbol] = {
                "source": "ibkr_tws_estimate",
                "provider": "ibkr_tws",
                "session": session,
                "quote_type": quote_type,
                "confidence": "high",
                "price": price,
                "as_of_utc": now_utc,
                "raw": {
                    "host": host,
                    "port": connected_port,
                    "contract": {
                        "symbol": getattr(contract, "symbol", symbol),
                        "exchange": getattr(contract, "exchange", None),
                        "primaryExchange": getattr(contract, "primaryExchange", None),
                        "conId": getattr(contract, "conId", None),
                    },
                    "bid": _ibkr_tws_raw_value(getattr(ticker, "bid", None)),
                    "ask": _ibkr_tws_raw_value(getattr(ticker, "ask", None)),
                    "last": _ibkr_tws_raw_value(getattr(ticker, "last", None)),
                    "close": _ibkr_tws_raw_value(getattr(ticker, "close", None)),
                },
            }
    finally:
        for _symbol, contract, _session, _ticker in tickers:
            try:
                ib.cancelMktData(contract)
            except Exception:
                pass
        if not persistent_connection:
            _disconnect_ibkr_tws(ib)
    return prices


def _preferred_yfinance_live_price(ticker) -> float | None:
    """Return Yahoo live/extended-hours quote price when available.

    Yahoo's 1m pre/post chart can stop at the regular after-hours session and
    miss overnight/premarket prints. The quote payload's pre/post fields are
    closer to what brokers show during the extended session.
    """
    try:
        info = ticker.info or {}
    except Exception:
        info = {}
    market_state = str(info.get("marketState") or "").upper()
    priority: list[str]
    if market_state.startswith("PRE"):
        priority = ["preMarketPrice", "postMarketPrice", "regularMarketPrice"]
    elif market_state.startswith("POST") or market_state in {"CLOSED", "POSTPOST"}:
        priority = ["postMarketPrice", "preMarketPrice", "regularMarketPrice"]
    else:
        priority = ["regularMarketPrice", "preMarketPrice", "postMarketPrice"]
    for key in priority:
        price = _positive_quote_price(info.get(key))
        if price is not None:
            return round(price, 2)
    return None


def _fetch_us_stock_prices_yf(symbols: List[str], *, prefer_live_quote: bool = False) -> Dict[str, float]:
    """从Yahoo Finance获取美股个股价格
    symbols: ['AAPL', 'VOO', ...]
    返回: {'AAPL': 189.50, ...}
    """
    if not symbols:
        return {}

    try:
        import yfinance as yf
    except ImportError:
        logger.error("yfinance未安装，无法获取美股价格")
        return {}

    quote_symbol_map = {sym: _yfinance_quote_symbol(sym) for sym in symbols}
    prices = {}
    _YFINANCE_CLOSE_CACHE.clear()
    try:
        for sym, quote_symbol in quote_symbol_map.items():
            try:
                ticker = yf.Ticker(quote_symbol)
                live_price = None
                if prefer_live_quote:
                    live_price = _preferred_yfinance_live_price(ticker)
                    if live_price is None:
                        intraday = ticker.history(period="1d", interval="1m", prepost=True, auto_adjust=False)
                        if intraday is not None and not intraday.empty and "Close" in intraday:
                            intraday_close = intraday["Close"].dropna()
                            if not intraday_close.empty:
                                price = _positive_quote_price(intraday_close.iloc[-1])
                                if price is not None:
                                    live_price = round(price, 2)
                    if live_price is None:
                        fast = ticker.fast_info
                        price = fast.get("last_price") if hasattr(fast, "get") else getattr(fast, "last_price", None)
                        if price and float(price) > 0:
                            live_price = round(float(price), 2)
                history = ticker.history(period="7d", auto_adjust=False)
                close_rows: List[Tuple[date, float]] = []
                if history is not None and not history.empty and "Close" in history:
                    for idx, close_value in history["Close"].dropna().items():
                        try:
                            close = float(close_value)
                        except (TypeError, ValueError):
                            continue
                        if close <= 0:
                            continue
                        trade_date = idx.date() if hasattr(idx, "date") else datetime.strptime(str(idx)[:10], "%Y-%m-%d").date()
                        close_rows.append((trade_date, close))
                if close_rows:
                    _YFINANCE_CLOSE_CACHE[sym] = close_rows
                    prices[sym] = live_price if live_price is not None else round(float(close_rows[-1][1]), 2)
                    continue

                fast = ticker.fast_info
                price = fast.get("last_price") if hasattr(fast, "get") else getattr(fast, "last_price", None)
                if price and float(price) > 0:
                    prices[sym] = round(float(price), 2)
            except Exception as e:
                logger.warning("获取 %s 价格失败: %s", sym, e)
    except Exception as e:
        logger.error("yfinance批量查询失败: %s", e)

    return prices


def _fetch_us_stock_prices(symbols: List[str]) -> Dict[str, float]:
    """获取美股价格，自动区分QDII基金和美股个股
    - 6位数字 → 天天基金 (QDII基金累计净值)
    - 其他 → yfinance (美股个股)
    """
    fund_codes = [s for s in symbols if _is_fund_code(s)]
    stock_syms = [s for s in symbols if not _is_fund_code(s)]

    _YFINANCE_CLOSE_CACHE.clear()
    prices = {}
    if fund_codes:
        prices.update(_fetch_fund_navs(fund_codes))
    if stock_syms:
        prices.update(_fetch_us_stock_prices_yf(stock_syms))

    return prices


def _record_yahoo_estimate_quote_sources(symbols: List[str], prices: Dict[str, float]) -> None:
    now_utc = datetime.now(ZoneInfo("UTC")).isoformat()
    for symbol in symbols:
        key = str(symbol or "").strip().upper()
        if not key or symbol not in prices:
            continue
        _US_ESTIMATE_QUOTE_CACHE[key] = {
            "source": "yahoo_estimate",
            "provider": "yahoo_finance",
            "session": "live_or_extended_fallback",
            "quote_type": "yahoo_live_or_close",
            "confidence": "medium",
            "price": prices[symbol],
            "as_of_utc": now_utc,
        }


def _fetch_us_stock_estimate_prices(symbols: List[str]) -> Dict[str, float]:
    """Fetch overseas holdings with Databento all-hours marks preferred."""
    fund_codes = [s for s in symbols if _is_fund_code(s)]
    stock_syms = [s for s in symbols if not _is_fund_code(s)]

    _US_ESTIMATE_QUOTE_CACHE.clear()
    _YFINANCE_CLOSE_CACHE.clear()
    prices = {}
    if fund_codes:
        prices.update(_fetch_fund_navs(fund_codes))
    if stock_syms:
        yahoo_prices = _fetch_us_stock_prices_yf(stock_syms, prefer_live_quote=True)
        prices.update(yahoo_prices)
        _record_yahoo_estimate_quote_sources(stock_syms, yahoo_prices)

        if _ibkr_estimate_fallback_enabled():
            tws_prices = _fetch_ibkr_tws_estimate_prices(stock_syms)
            prices.update(tws_prices)

            ibkr_prices = _fetch_ibkr_client_portal_estimate_prices(stock_syms)
            prices.update(ibkr_prices)

        databento_prices = _fetch_databento_us_estimate_prices(stock_syms)
        prices.update(databento_prices)

    return prices


# ---------- 加密价格获取 (CoinGecko) ----------

def _fetch_crypto_prices(symbols: List[str]) -> Dict[str, float]:
    """从CoinGecko获取加密货币价格 (USD)
    symbols: ['bitcoin', 'ethereum', ...]
    返回: {'bitcoin': 97000.0, ...}
    """
    if not symbols:
        return {}

    ids = ",".join(symbols)
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd"
    req = urllib.request.Request(url, headers={"User-Agent": "PAMS/2.0"})

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json_mod.loads(resp.read().decode())
    except Exception as e:
        logger.error("CoinGecko API请求失败: %s", e)
        return {}

    prices = {}
    for coin_id in symbols:
        coin_data = data.get(coin_id, {})
        usd_price = coin_data.get("usd")
        if usd_price is not None and usd_price > 0:
            prices[coin_id] = float(usd_price)

    return prices


def _positive_float(value) -> float | None:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def _parse_sge_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        pass
    match = re.search(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})", text)
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    return date(year, month, day)


def _latest_gold_proxy_price(as_of=None, *, exact: bool = False) -> float | None:
    """Return the latest stored SGE Au99.99 spot gold mark."""
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            date_filter = ""
            params: tuple[object, ...] = ()
            if as_of is not None:
                date_filter = "AND as_of = %s" if exact else "AND as_of <= %s"
                params = (as_of,)
            cur.execute(
                f"""
                SELECT COALESCE(close_price, unit_nav, estimated_unit_nav) AS close_price
                FROM pams_asset_market_daily
                WHERE (
                    asset_id = 'BENCH_GOLD_SPOT_AU9999'
                    OR symbol IN ('Au99.99.SGE', 'Au99.99')
                    OR source_key ILIKE '%%Au99.99%%'
                  )
                  AND COALESCE(close_price, unit_nav, estimated_unit_nav) IS NOT NULL
                  {date_filter}
                ORDER BY
                    as_of DESC,
                    CASE
                        WHEN source = 'akshare_sge_daily' THEN 0
                        WHEN source = 'akshare_sge_realtime' THEN 1
                        ELSE 2
                    END,
                    updated_at DESC
                LIMIT 1
                """,
                params,
            )
            row = cur.fetchone()
    if not row or row.get("close_price") is None:
        return None
    price = float(row["close_price"])
    return price if price > 0 else None


def _fetch_sge_au9999_history(as_of) -> List[Tuple[date, float | None, float | None, float | None, float]]:
    try:
        import akshare as ak
    except ImportError:
        logger.warning("akshare is not installed; SGE gold history is unavailable")
        return []

    try:
        frame = ak.spot_hist_sge(symbol="Au99.99")
    except Exception as exc:
        logger.warning("Failed to fetch SGE Au99.99 history: %s", exc)
        return []

    rows: List[Tuple[date, float | None, float | None, float | None, float]] = []
    for _, record in frame.iterrows():
        trade_date = _parse_sge_date(record.get("date"))
        close_price = _positive_float(record.get("close"))
        if trade_date is None or close_price is None or trade_date > as_of:
            continue
        rows.append((
            trade_date,
            _positive_float(record.get("open")),
            _positive_float(record.get("high")),
            _positive_float(record.get("low")),
            close_price,
        ))
    rows.sort(key=lambda row: row[0])
    return rows


def _fetch_sge_au9999_realtime(as_of) -> Tuple[date, float] | None:
    try:
        import akshare as ak
    except ImportError:
        logger.warning("akshare is not installed; SGE gold realtime quote is unavailable")
        return None

    try:
        frame = ak.spot_quotations_sge(symbol="Au99.99")
    except Exception as exc:
        logger.warning("Failed to fetch SGE Au99.99 realtime quote: %s", exc)
        return None

    latest: Tuple[date, float] | None = None
    for _, record in frame.iterrows():
        quote_date = _parse_sge_date(record.get("更新时间")) or as_of
        price = _positive_float(record.get("现价"))
        if quote_date is None or price is None or quote_date > as_of:
            continue
        latest = (quote_date, price)
    return latest


def _upsert_gold_spot_closes(
    rows: List[Tuple[date, float | None, float | None, float | None, float]],
    *,
    source: str = "akshare_sge_daily",
) -> int:
    if not rows:
        return 0
    payload = []
    for trade_date, open_price, high_price, low_price, close_price in rows:
        if not close_price or close_price <= 0:
            continue
        payload.append((
            trade_date,
            source,
            f"{source}:Au99.99:{trade_date.isoformat()}",
            open_price,
            high_price,
            low_price,
            close_price,
            json_mod.dumps({
                "role": "benchmark",
                "proxy_for": ["CCB_GOLD_ACCUM", "ASSET_CCB_GOLD_ACCUM"],
                "is_holding": False,
                "symbol": "Au99.99",
            }),
        ))
    if not payload:
        return 0
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO pams_asset_market_daily (
                    as_of, source, source_key, asset_id, symbol, name, market, currency,
                    open_price, high_price, low_price, close_price,
                    confidence, raw_json, updated_at
                )
                VALUES (
                    %s, %s, %s,
                    'BENCH_GOLD_SPOT_AU9999', 'Au99.99.SGE', '上海黄金Au99.99',
                    'SGE', 'CNY', %s, %s, %s, %s,
                    'high', %s::jsonb, now()
                )
                ON CONFLICT (as_of, source, source_key)
                DO UPDATE SET
                    asset_id = EXCLUDED.asset_id,
                    symbol = EXCLUDED.symbol,
                    name = EXCLUDED.name,
                    market = EXCLUDED.market,
                    currency = EXCLUDED.currency,
                    open_price = EXCLUDED.open_price,
                    high_price = EXCLUDED.high_price,
                    low_price = EXCLUDED.low_price,
                    close_price = EXCLUDED.close_price,
                    confidence = EXCLUDED.confidence,
                    raw_json = pams_asset_market_daily.raw_json || EXCLUDED.raw_json,
                    updated_at = now()
                """,
                payload,
            )
            conn.commit()
            return len(payload)


def _sync_gold_accum_snapshots_from_spot(start: date, end: date) -> int:
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH spot AS (
                    SELECT DISTINCT ON (as_of) as_of, close_price
                    FROM pams_asset_market_daily
                    WHERE as_of BETWEEN %s AND %s
                      AND asset_id = 'BENCH_GOLD_SPOT_AU9999'
                      AND close_price IS NOT NULL
                    ORDER BY
                        as_of,
                        CASE
                            WHEN source = 'akshare_sge_daily' THEN 0
                            WHEN source = 'akshare_sge_realtime' THEN 1
                            ELSE 2
                        END,
                        updated_at DESC
                )
                UPDATE pams_holdings_snapshot h
                SET current_price = spot.close_price,
                    market_value = ROUND((h.quantity * spot.close_price)::numeric, 2),
                    updated_at = now()
                FROM spot
                WHERE h.as_of = spot.as_of
                  AND h.major_category = '贵金属'
                  AND h.quantity IS NOT NULL
                  AND (
                      UPPER(COALESCE(h.symbol, '')) IN ('CCB_GOLD_ACCUM', 'GOLD', 'XAU')
                      OR UPPER(COALESCE(h.symbol, '')) LIKE '%%GOLD%%'
                  )
                """,
                (start, end),
            )
            updated = cur.rowcount
            conn.commit()
            return updated


def _gold_proxy_price_for_refresh(as_of) -> float | None:
    """Return a fresh SGE Au99.99 mark for local gold accumulation holdings."""
    history = _fetch_sge_au9999_history(as_of)
    if history:
        recent_history = history[-15:]
        _upsert_gold_spot_closes(recent_history)
        _sync_gold_accum_snapshots_from_spot(recent_history[0][0], recent_history[-1][0])

    realtime = _fetch_sge_au9999_realtime(as_of)
    if realtime:
        quote_date, price = realtime
        _upsert_gold_spot_closes(
            [(quote_date, None, None, None, price)],
            source="akshare_sge_realtime",
        )
        return price

    stored = _latest_gold_proxy_price(as_of=as_of, exact=True)
    if stored and stored > 0:
        return stored
    if history:
        return history[-1][4]
    return _latest_gold_proxy_price(as_of=as_of)


def _fetch_precious_metal_prices(symbols: List[str]) -> Dict[str, float]:
    """Fetch precious-metal valuation prices for local holdings.

    CCB gold accumulation is valued with local SGE Au99.99 spot gold.
    Six-digit fund-like symbols still use Eastmoney NAV.
    """
    prices: Dict[str, float] = {}
    fund_codes = [s for s in symbols if _is_fund_code(s)]
    if fund_codes:
        prices.update(_fetch_fund_navs(fund_codes))

    gold_proxy = _gold_proxy_price_for_refresh(_today_cst())
    if gold_proxy:
        for symbol in symbols:
            raw = str(symbol or "").upper()
            if raw in {"CCB_GOLD_ACCUM", "GOLD", "XAU"} or "GOLD" in raw:
                prices[symbol] = gold_proxy
    return prices


# ---------- 通用刷新逻辑 ----------

def _refresh_category(
    category: str,
    fetch_fn,
    exclude_minor_categories: Tuple[str, ...] = (),
    market_buckets: Tuple[str, ...] = (),
    price_basis: str = "close",
) -> dict:
    """刷新指定类别的价格"""
    holdings = _get_holdings_with_symbol(
        category,
        exclude_minor_categories=exclude_minor_categories,
        market_buckets=market_buckets,
    )
    if not holdings:
        return {
            "category": category,
            "found": 0,
            "updated": 0,
            "market_buckets": list(market_buckets),
            "price_basis": price_basis,
            "errors": [],
        }

    target_as_of = _price_refresh_as_of(category, price_basis)
    if category == _A_STOCK_MAJOR_CATEGORY and _A_STOCK_FUND_MINOR_CATEGORY in set(exclude_minor_categories):
        holdings = _apply_same_day_a_stock_trades(holdings, target_as_of)
    same_day_trade_adjusted = sum(1 for h in holdings if h.get("same_day_trade_adjusted"))
    symbols = [h["symbol"] for h in holdings]
    prices = fetch_fn(symbols)
    close_rows = 0
    estimate_rows = 0
    quote_sources: dict[str, int] = {}
    skipped_estimates: list[dict[str, Any]] = []
    if category == "美股":
        close_rows = _upsert_yfinance_closes(holdings)
        if price_basis == "estimate":
            prices, skipped_estimates = _filter_us_estimate_prices_for_holdings(holdings, prices)
            priced_symbols = [str(sym) for sym in symbols if sym in prices or str(sym).upper() in prices]
            estimate_rows = _upsert_us_estimate_market_daily(holdings, prices, target_as_of)
            quote_sources = _estimate_quote_source_summary(priced_symbols)
        if price_basis == "close":
            prices = _latest_stored_yfinance_prices(symbols, as_of=target_as_of, fallback=prices)

    updates = []
    errors = []
    for h in holdings:
        sym = h["symbol"]
        if sym not in prices:
            errors.append(f"{sym} ({h['name']}): 未获取到价格")
            continue
        new_price = prices[sym]
        qty = float(h["quantity"])
        new_mv = round(qty * new_price, 2)
        updates.append((new_price, new_mv, h["id"]))

    updated = _upsert_price_snapshots(holdings, prices, target_as_of)
    if category == "A股":
        close_rows = _upsert_cn_a_closes(holdings, prices, target_as_of)

    return {
        "category": category,
        "source_as_of": str(holdings[0]["as_of"]) if holdings else None,
        "as_of": str(target_as_of),
        "found": len(holdings),
        "updated": updated,
        "same_day_trade_adjusted": same_day_trade_adjusted,
        "close_rows": close_rows,
        "estimate_rows": estimate_rows,
        "quote_sources": quote_sources,
        "skipped_estimates": skipped_estimates,
        "market_buckets": list(market_buckets),
        "price_basis": price_basis,
        "errors": errors,
    }


# ---------- API端点 ----------

def _default_price_start(end_date: date) -> date:
    return date(end_date.year, 1, 1)


def _price_request_instruments(
    *,
    symbols: str | List[str] | None = None,
    benchmarks: str | List[str] | None = None,
    market: Optional[str] = None,
):
    instruments = [
        normalize_price_instrument(symbol, market=market)
        for symbol in parse_symbol_list(symbols)
    ]
    instruments.extend(
        normalize_price_instrument(symbol, benchmark=True)
        for symbol in parse_symbol_list(benchmarks)
    )
    return instruments


@router.get("/prices/series")
def get_price_series(
    symbols: str = "",
    benchmarks: str = "",
    start: Optional[date] = None,
    end: Optional[date] = None,
    normalize: bool = True,
    market: Optional[str] = None,
):
    """Read locally stored price curves for instruments and benchmarks."""
    end_date = end or _today_cst()
    start_date = start or _default_price_start(end_date)
    try:
        instruments = _price_request_instruments(symbols=symbols, benchmarks=benchmarks, market=market)
    except ValueError as exc:
        return _json({"status": "error", "error": str(exc)}, status_code=400)
    if not instruments:
        return _json(
            {
                "start": start_date.isoformat(),
                "end": end_date.isoformat(),
                "normalize": normalize,
                "series": [],
            }
        )
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            payload = build_price_series_payload(
                cur,
                instruments=instruments,
                start=start_date,
                end=end_date,
                normalize=normalize,
            )
    return _json(payload)


@router.post("/prices/history/backfill")
def backfill_price_history(body: PriceHistoryBackfillRequest):
    """Persist supported external price history into local OHLCV storage."""
    end_date = body.end or _today_cst()
    try:
        instruments = _price_request_instruments(
            symbols=body.symbols,
            benchmarks=body.benchmarks,
            market=body.market,
        )
    except ValueError as exc:
        return _json({"status": "error", "error": str(exc)}, status_code=400)
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            backfill = ensure_price_history(cur, instruments=instruments, start=body.start, end=end_date)
            payload = build_price_series_payload(
                cur,
                instruments=instruments,
                start=body.start,
                end=end_date,
                normalize=True,
            )
        conn.commit()
    return _json({"status": "ok", "backfill": backfill, "series": payload})


@router.get("/prices/trade-context")
def get_trade_price_context(
    symbol: str,
    tradeDate: date,
    market: Optional[str] = None,
    benchmarks: str = "",
    lookbackDays: int = 20,
    lookaheadDays: int = 20,
    normalize: bool = True,
):
    """Read a trade-centered price window for future transaction review."""
    lookback = max(0, min(int(lookbackDays), 365))
    lookahead = max(0, min(int(lookaheadDays), 365))
    start_date = tradeDate - timedelta(days=lookback)
    end_date = min(_today_cst(), tradeDate + timedelta(days=lookahead))
    try:
        instruments = _price_request_instruments(symbols=[symbol], benchmarks=benchmarks, market=market)
    except ValueError as exc:
        return _json({"status": "error", "error": str(exc)}, status_code=400)
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            payload = build_price_series_payload(
                cur,
                instruments=instruments,
                start=start_date,
                end=end_date,
                normalize=normalize,
            )
    payload["trade_date"] = tradeDate.isoformat()
    payload["lookback_days"] = lookback
    payload["lookahead_days"] = lookahead
    return _json(payload)


@router.post("/prices/refresh/a-stock")
def refresh_a_stock():
    """刷新A股价格"""
    result = _refresh_category("A股", _fetch_a_stock_prices, exclude_minor_categories=("A股基金",))
    return _json(result)


@router.post("/prices/refresh/us-stock")
def refresh_us_stock(
    market: Optional[str] = Query(None),
    price_basis: str = Query("close", alias="priceBasis"),
):
    """刷新美股价格"""
    if not isinstance(market, str):
        market = None
    if not isinstance(price_basis, str):
        price_basis = "close"
    if price_basis not in {"close", "estimate"}:
        price_basis = "close"
    market_buckets = _normalize_overseas_market_filter(market)
    fetch_fn = _fetch_us_stock_estimate_prices if price_basis == "estimate" else _fetch_us_stock_prices
    result = _refresh_category(
        "美股",
        fetch_fn,
        market_buckets=market_buckets,
        price_basis=price_basis,
    )
    return _json(result)


@router.post("/prices/refresh/crypto")
def refresh_crypto():
    """刷新加密货币价格"""
    result = _refresh_category("加密", _fetch_crypto_prices)
    return _json(result)


@router.post("/prices/refresh/precious-metal")
def refresh_precious_metal():
    """刷新贵金属价格（黄金积存金优先使用本地 SGE 现货金价）"""
    result = _refresh_category("贵金属", _fetch_precious_metal_prices)
    return _json(result)


@router.post("/prices/refresh/fund-nav-estimates")
def refresh_fund_nav_estimates(
    use_intraday_estimate: Optional[bool] = Query(None, alias="useIntradayEstimate"),
):
    """Fetch A-share fund confirmed NAVs and post-close estimated NAVs."""
    if not isinstance(use_intraday_estimate, bool):
        use_intraday_estimate = None
    with _get_conn() as conn:
        result = _refresh_fund_nav_estimates(conn, use_intraday_estimates=use_intraday_estimate)
    return _json(result)


def _normalize_investment_refresh_scopes(scopes: List[str]) -> List[str]:
    aliases = {
        "all": ["a_stock", "us_stock", "a_fund", "precious_metal"],
        "a-share": ["a_stock"],
        "ashare": ["a_stock"],
        "fund": ["a_fund"],
        "funds": ["a_fund"],
        "gold": ["precious_metal"],
        "metal": ["precious_metal"],
        "metals": ["precious_metal"],
        "precious": ["precious_metal"],
        "us": ["us_stock"],
        "us-only": ["us_only"],
        "usa": ["us_only"],
        "hk": ["hk_stock"],
        "hong-kong": ["hk_stock"],
        "hongkong": ["hk_stock"],
        "kr": ["kr_stock"],
        "korea": ["kr_stock"],
    }
    normalized: List[str] = []
    for raw in scopes or ["a_stock", "us_stock", "a_fund", "precious_metal"]:
        key = str(raw or "").strip().lower().replace("-", "_")
        if key in aliases:
            for item in aliases[key]:
                if item not in normalized:
                    normalized.append(item)
            continue
        if key in {"a_stock", "us_stock", "us_only", "hk_stock", "kr_stock", "a_fund", "precious_metal"} and key not in normalized:
            normalized.append(key)
    return normalized or ["a_stock", "us_stock", "a_fund", "precious_metal"]


@router.post("/prices/refresh/investments")
def refresh_investment_prices(body: InvestmentPriceRefreshRequest | None = None):
    """Refresh public market values for investment holdings.

    This endpoint only updates local valuation snapshots from public data
    sources. It does not connect to brokers, place orders, exchange currency, or
    change transactions.
    """
    req = body or InvestmentPriceRefreshRequest()
    requested_scopes = _normalize_investment_refresh_scopes(req.scopes)
    results: List[dict] = []

    for scope in requested_scopes:
        try:
            if scope == "a_stock":
                detail = _refresh_category("A股", _fetch_a_stock_prices, exclude_minor_categories=("A股基金",))
                label = "A股个股"
            elif scope == "us_stock":
                price_basis = req.overseas_price_basis if req.overseas_price_basis in {"close", "estimate"} else "close"
                fetch_fn = _fetch_us_stock_estimate_prices if price_basis == "estimate" else _fetch_us_stock_prices
                detail = _refresh_category("美股", fetch_fn, price_basis=price_basis)
                label = "美股/海外"
            elif scope in {"us_only", "hk_stock", "kr_stock"}:
                price_basis = req.overseas_price_basis if req.overseas_price_basis in {"close", "estimate"} else "close"
                fetch_fn = _fetch_us_stock_estimate_prices if price_basis == "estimate" else _fetch_us_stock_prices
                bucket = {"us_only": "us", "hk_stock": "hk", "kr_stock": "kr"}[scope]
                detail = _refresh_category(
                    "美股",
                    fetch_fn,
                    market_buckets=(bucket,),
                    price_basis=price_basis,
                )
                label = {"us_only": "美股", "hk_stock": "港股", "kr_stock": "韩股"}[scope]
            elif scope == "a_fund":
                with _get_conn() as conn:
                    detail = _refresh_fund_nav_estimates(
                        conn,
                        use_intraday_estimates=req.fund_use_intraday_estimate,
                    )
                label = "A股基金"
            elif scope == "precious_metal":
                detail = _refresh_category("贵金属", _fetch_precious_metal_prices)
                label = "贵金属"
            else:
                continue
            errors = detail.get("errors") or []
            results.append(
                {
                    "scope": scope,
                    "label": label,
                    "success": not bool(errors),
                    "detail": detail,
                    "errors": errors,
                }
            )
        except Exception as exc:  # pragma: no cover - network/runtime guard
            results.append(
                {
                    "scope": scope,
                    "label": {
                        "a_stock": "A股个股",
                        "us_stock": "美股/海外",
                        "us_only": "美股",
                        "hk_stock": "港股",
                        "kr_stock": "韩股",
                        "a_fund": "A股基金",
                        "precious_metal": "贵金属",
                    }.get(scope, scope),
                    "success": False,
                    "detail": {},
                    "errors": [str(exc)],
                }
            )

    latest_as_of = _latest_investment_snapshot_as_of()
    total_found = sum(int((r.get("detail") or {}).get("found") or (r.get("detail") or {}).get("processed") or 0) for r in results)
    total_updated = 0
    for item in results:
        detail = item.get("detail") or {}
        total_updated += int(detail.get("updated") or 0)
        total_updated += int(detail.get("inserted_estimate_snapshots") or 0)
        total_updated += int(detail.get("corrected_confirmed_snapshots") or 0)
    return _json(
        {
            "status": "ok" if all(r.get("success") for r in results) else "partial",
            "as_of": latest_as_of.isoformat() if hasattr(latest_as_of, "isoformat") else latest_as_of,
            "requested_scopes": requested_scopes,
            "total_found": total_found,
            "total_updated": total_updated,
            "results": results,
            "note": "仅刷新公开行情/净值和本地估值快照，不执行交易。",
        }
    )


@router.post("/prices/refresh")
def refresh_all():
    """刷新全部价格（FX + A股 + 美股 + 加密）"""
    results = []

    # FX rates (调用已有端点逻辑)
    try:
        fx_url = "http://localhost:8000/api/fx-rates/refresh"
        req = urllib.request.Request(fx_url, method="POST",
                                     headers={"User-Agent": "PAMS/2.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            fx_result = json_mod.loads(resp.read().decode())
        results.append({"category": "FX", "success": True, "detail": fx_result})
    except Exception as e:
        results.append({"category": "FX", "success": False, "error": str(e)})

    # 各资产类别
    for category, fetch_fn, exclude_minor_categories in [
        ("A股", _fetch_a_stock_prices, ("A股基金",)),
        ("美股", _fetch_us_stock_prices, ()),
        ("加密", _fetch_crypto_prices, ()),
        ("贵金属", _fetch_precious_metal_prices, ()),
    ]:
        result = _refresh_category(category, fetch_fn, exclude_minor_categories=exclude_minor_categories)
        results.append(result)

    return _json({"results": results})
