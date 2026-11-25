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
from domain.imports.isolation import source_enabled
from domain.investments.holdings.holding_evidence_writer import finish_holding_change, lock_holding_accounts, prepare_holding_change
from domain.investments.returns.daily_return_state import quote_snapshot_from_market_daily, upsert_quote_snapshot
from domain.market.fund_nav_estimates import refresh_fund_nav_estimates as _refresh_fund_nav_estimates
from domain.investments.returns.investment_view_date import daily_pnl_window_date as _daily_pnl_window_date
from domain.brokers.ibkr.ibkr_report_state import expected_statement_date
from domain.market.quote_valuation import GOLD_BENCHMARK_ASSET, GOLD_CLOSE_SOURCE
from domain.market.price_history import (
    valid_holding_history_window,
    build_price_series_payload,
    ensure_price_history,
    normalize_price_instrument,
    parse_symbol_list,
)

logger = logging.getLogger("prices")


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


# ---------- DB helpers ----------

def _get_conn():
    from domain.imports.isolation import guarded_connect

    return guarded_connect(
        host=os.environ.get("PGHOST", "pams-postgres"),
        port=int(os.environ.get("PGPORT", "5432")),
        dbname=os.environ.get("PGDATABASE", "pams"),
        user=os.environ.get("PGUSER", "pams"),
        password=os.environ.get("PGPASSWORD", ""),
    )



# ---------- A股个股价格 (腾讯财经) ----------

def _fetch_stock_prices_tencent(symbols: List[str]) -> Dict[str, float]:
    """从腾讯财经API批量获取当日收盘后的A股个股价格
    symbols: ['sh600519', 'sz000858', ...]
    返回: {'sh600519': 1855.02, ...}
    """
    from domain.market.tencent_stock_quote import fetch_tencent_stock_quotes, completed_session_prices
    try:
        observations = fetch_tencent_stock_quotes(symbols)
    except Exception as e:
        from domain.imports.isolation import IsolationError
        if isinstance(e, IsolationError):
            raise
        logger.error("腾讯财经API请求失败: %s", e)
        return {}
    return {symbol: float(price) for symbol, price in completed_session_prices(
        observations, now=datetime.now(ZoneInfo('Asia/Shanghai'))).items()}


# ---------- 基金累计净值 (天天基金 Eastmoney) ----------

def _fetch_fund_navs(codes: List[str]) -> Dict[str, float]:
    """从天天基金获取基金累计净值
    codes: ['008928', '011613', ...]
    返回: {'008928': 1.2345, ...}

    使用 F10DataApi 获取最新一期的累计净值(ljjz)
    响应格式: var apidata={content:"<table>...<td>日期</td><td>单位净值</td><td>累计净值</td>...",records:N,...}
    """
    from domain.imports.isolation import require_external_access
    require_external_access()
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

def _yfinance_quote_symbol(symbol: str, market: str | None = None) -> str:
    """Return a yfinance-compatible symbol for overseas holdings."""
    raw = (symbol or "").strip()
    suffix = {"se": ".ST", "ch": ".SW", "kr": ".KS"}.get(str(market or "").lower())
    if suffix and "." not in raw:
        return raw + suffix
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
    if raw.endswith((".HK", ".KS", ".KQ", ".ST", ".SW")):
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
    from domain.imports.isolation import require_external_access
    require_external_access()
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
    from domain.imports.isolation import require_external_access
    require_external_access()
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
    from domain.imports.isolation import require_external_access
    require_external_access()
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
    from domain.imports.isolation import require_external_access
    require_external_access()
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
    from domain.imports.isolation import require_external_access
    require_external_access()
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


def _fetch_sge_au9999_history(as_of) -> List[Tuple[date, float | None, float | None, float | None, float]]:
    from domain.imports.isolation import require_external_access
    require_external_access()
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


def _upsert_gold_spot_closes(
    rows: List[Tuple[date, float | None, float | None, float | None, float]],
) -> int:
    if not rows:
        return 0
    payload = []
    for trade_date, open_price, high_price, low_price, close_price in rows:
        if not close_price or close_price <= 0:
            continue
        payload.append((
            trade_date,
            GOLD_CLOSE_SOURCE,
            f"{GOLD_CLOSE_SOURCE}:Au99.99:{trade_date.isoformat()}",
            GOLD_BENCHMARK_ASSET,
            open_price,
            high_price,
            low_price,
            close_price,
            json_mod.dumps({
                "role": "benchmark",
                "is_holding": False,
                "symbol": "Au99.99",
            }),
        ))
    if not payload:
        return 0
    with _get_conn() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO market_observations (
                    observation_type,
                    as_of, source, source_key, asset_id, symbol, name, market, currency,
                    open_price, high_price, low_price, close_price,
                    confidence, raw_json, updated_at
                ) VALUES %s
                ON CONFLICT (as_of, source, source_key) WHERE observation_type = 'daily'
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
                    raw_json = market_observations.raw_json || EXCLUDED.raw_json,
                    updated_at = now()
                WHERE (market_observations.open_price, market_observations.high_price,
                       market_observations.low_price, market_observations.close_price)
                    IS DISTINCT FROM (EXCLUDED.open_price, EXCLUDED.high_price, EXCLUDED.low_price, EXCLUDED.close_price)
                """,
                payload,
                template="""('daily', %s, %s, %s, %s, 'Au99.99.SGE', '上海黄金Au99.99',
                    'SGE', 'CNY', %s, %s, %s, %s, 'high', %s::jsonb, now())""",
                page_size=500,
            )
            conn.commit()
            return len(payload)


def _fetch_precious_metal_prices(symbols: List[str]) -> Dict[str, float]:
    """Refresh unit NAV for fund positions classified under precious metals."""
    fund_codes = [symbol for symbol in symbols if _is_fund_code(symbol)]
    return _fetch_fund_navs(fund_codes) if fund_codes else {}


