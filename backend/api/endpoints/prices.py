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
import urllib.request
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from services.daily_return_state import quote_snapshot_from_market_daily, upsert_quote_snapshot
from services.fund_nav_estimates import refresh_fund_nav_estimates as _refresh_fund_nav_estimates
from services.price_history import (
    build_price_series_payload,
    ensure_price_history,
    normalize_price_instrument,
    parse_symbol_list,
)

logger = logging.getLogger("prices")

router = APIRouter()

_YFINANCE_CLOSE_CACHE: Dict[str, List[Tuple[date, float]]] = {}


class InvestmentPriceRefreshRequest(BaseModel):
    """Manual public-market refresh request for investment holdings."""

    scopes: List[str] = Field(default_factory=lambda: ["a_stock", "us_stock", "a_fund", "precious_metal"])


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
) -> List[dict]:
    """查询指定大类中有symbol的持仓"""
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            excluded_sql = ""
            params: List[object] = [category]
            if exclude_minor_categories:
                placeholders = ", ".join(["%s"] * len(exclude_minor_categories))
                excluded_sql = f"AND COALESCE(minor_category, '') NOT IN ({placeholders})"
                params.extend(exclude_minor_categories)

            cur.execute(f"""
                SELECT MAX(as_of) AS as_of
                FROM pams_holdings_snapshot
                WHERE major_category = %s
                  AND symbol IS NOT NULL AND symbol != ''
                  AND quantity IS NOT NULL AND quantity > 0
                  {excluded_sql}
            """, params)
            row = cur.fetchone()
            as_of = row["as_of"] if row else None
            if as_of is None:
                return []

            params = [as_of, category]
            if exclude_minor_categories:
                placeholders = ", ".join(["%s"] * len(exclude_minor_categories))
                excluded_sql = f"AND COALESCE(minor_category, '') NOT IN ({placeholders})"
                params.extend(exclude_minor_categories)

            cur.execute(f"""
                SELECT
                    id, symbol, name, asset_liability, currency, account_code,
                    major_category, minor_category, market_value, cost, as_of,
                    quantity, buy_price, current_price
                FROM pams_holdings_snapshot
                WHERE as_of = %s AND major_category = %s
                  AND symbol IS NOT NULL AND symbol != ''
                  AND quantity IS NOT NULL AND quantity > 0
                  {excluded_sql}
            """, params)
            return cur.fetchall()


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


def _upsert_daily_return_quote_snapshots(
    rows: List[dict[str, Any]],
    prices: Dict[str, float],
    as_of,
    *,
    source: str,
    market: str,
    currency: str = "CNY",
) -> int:
    """Mirror refreshed public prices into the canonical daily return state."""
    if not rows or not prices:
        return 0

    written = 0
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            for row in rows:
                symbol = str(row.get("symbol") or "")
                if symbol not in prices:
                    continue
                quote_symbol = _a_stock_quote_symbol(symbol) or symbol
                quote = quote_snapshot_from_market_daily(
                    {
                        "as_of": as_of,
                        "asset_id": row.get("asset_id"),
                        "symbol": symbol,
                        "name": row.get("name"),
                        "market": market,
                        "currency": row.get("currency") or currency,
                        "provider": source.split("_")[0] if source else "market",
                        "source": source,
                        "source_key": f"{source}:{quote_symbol}:{as_of.isoformat()}",
                        "close_price": Decimal(str(prices[symbol])),
                    }
                )
                upsert_quote_snapshot(cur, quote)
                written += 1
            conn.commit()
    return written


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

def _fetch_us_stock_prices_yf(symbols: List[str]) -> Dict[str, float]:
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
                    prices[sym] = round(float(close_rows[-1][1]), 2)
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


def _latest_gold_proxy_price(as_of=None) -> float | None:
    """Return the latest stored domestic gold ETF proxy close on or before as_of."""
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            date_filter = ""
            params: tuple[object, ...] = ()
            if as_of is not None:
                date_filter = "AND trade_date <= %s"
                params = (as_of,)
            cur.execute(
                f"""
                SELECT close
                FROM pams_cn_etf_daily
                WHERE symbol = 'sh518880'
                  AND close IS NOT NULL
                  {date_filter}
                ORDER BY trade_date DESC
                LIMIT 1
                """,
                params,
            )
            row = cur.fetchone()
    if not row or row.get("close") is None:
        return None
    price = float(row["close"])
    return price if price > 0 else None


def _upsert_gold_proxy_close(as_of, price: float) -> int:
    if not price or price <= 0:
        return 0
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO pams_cn_etf_daily (
                    symbol, trade_date, ts_code, close, source, build_run_id
                )
                VALUES ('sh518880', %s, '518880.SH', %s, 'tencent_quote', 'manual_precious_metal_refresh')
                ON CONFLICT (symbol, trade_date)
                DO UPDATE SET
                    close = EXCLUDED.close,
                    source = EXCLUDED.source,
                    build_run_id = EXCLUDED.build_run_id,
                    latest_refresh_at = now(),
                    updated_at = now()
                """,
                (as_of, price),
            )
            conn.commit()
            return 1


def _gold_proxy_price_for_refresh(as_of) -> float | None:
    """Fetch same-day sh518880 ETF quote, falling back to stored close."""
    prices = _fetch_stock_prices_tencent(["sh518880"])
    price = prices.get("sh518880")
    if price and price > 0:
        _upsert_gold_proxy_close(as_of, price)
        return price
    return _latest_gold_proxy_price(as_of=as_of)


def _fetch_precious_metal_prices(symbols: List[str]) -> Dict[str, float]:
    """Fetch precious-metal valuation prices for local holdings.

    CCB gold accumulation is valued with the local sh518880 gold ETF proxy.
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
) -> dict:
    """刷新指定类别的价格"""
    holdings = _get_holdings_with_symbol(
        category,
        exclude_minor_categories=exclude_minor_categories,
    )
    if not holdings:
        return {"category": category, "found": 0, "updated": 0, "errors": []}

    symbols = [h["symbol"] for h in holdings]
    prices = fetch_fn(symbols)
    target_as_of = _today_cst()
    close_rows = 0
    if category == "美股":
        close_rows = _upsert_yfinance_closes(holdings)
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
    daily_quote_rows = 0
    if category == "A股":
        close_rows = _upsert_cn_a_closes(holdings, prices, target_as_of)
        daily_quote_rows = _upsert_daily_return_quote_snapshots(
            holdings,
            prices,
            target_as_of,
            source="tencent_a_stock_close",
            market="CN_A",
        )

    return {
        "category": category,
        "source_as_of": str(holdings[0]["as_of"]) if holdings else None,
        "as_of": str(target_as_of),
        "found": len(holdings),
        "updated": updated,
        "close_rows": close_rows,
        "daily_quote_rows": daily_quote_rows,
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
def refresh_us_stock():
    """刷新美股价格"""
    result = _refresh_category("美股", _fetch_us_stock_prices)
    return _json(result)


@router.post("/prices/refresh/crypto")
def refresh_crypto():
    """刷新加密货币价格"""
    result = _refresh_category("加密", _fetch_crypto_prices)
    return _json(result)


@router.post("/prices/refresh/precious-metal")
def refresh_precious_metal():
    """刷新贵金属价格（黄金积存金使用本地黄金 ETF 代理价）"""
    result = _refresh_category("贵金属", _fetch_precious_metal_prices)
    return _json(result)


@router.post("/prices/refresh/fund-nav-estimates")
def refresh_fund_nav_estimates():
    """Fetch A-share fund confirmed NAVs and post-close estimated NAVs."""
    with _get_conn() as conn:
        result = _refresh_fund_nav_estimates(conn)
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
    }
    normalized: List[str] = []
    for raw in scopes or ["a_stock", "us_stock", "a_fund", "precious_metal"]:
        key = str(raw or "").strip().lower().replace("-", "_")
        if key in aliases:
            for item in aliases[key]:
                if item not in normalized:
                    normalized.append(item)
            continue
        if key in {"a_stock", "us_stock", "a_fund", "precious_metal"} and key not in normalized:
            normalized.append(key)
    return normalized or ["a_stock", "us_stock", "a_fund", "precious_metal"]


@router.post("/prices/refresh/investments")
def refresh_investment_prices(body: InvestmentPriceRefreshRequest | None = None):
    """Refresh public market values for investment holdings.

    This endpoint only updates local valuation snapshots from public data
    sources. It does not connect to brokers, place orders, exchange currency, or
    change transactions.
    """
    requested_scopes = _normalize_investment_refresh_scopes((body or InvestmentPriceRefreshRequest()).scopes)
    results: List[dict] = []

    for scope in requested_scopes:
        try:
            if scope == "a_stock":
                detail = _refresh_category("A股", _fetch_a_stock_prices, exclude_minor_categories=("A股基金",))
                label = "A股个股"
            elif scope == "us_stock":
                detail = _refresh_category("美股", _fetch_us_stock_prices)
                label = "美股/海外"
            elif scope == "a_fund":
                with _get_conn() as conn:
                    detail = _refresh_fund_nav_estimates(conn)
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
