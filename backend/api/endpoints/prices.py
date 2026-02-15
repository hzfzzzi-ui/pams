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
from typing import Dict, List, Tuple

import psycopg2
import psycopg2.extras
from fastapi import APIRouter
from fastapi.responses import JSONResponse

logger = logging.getLogger("prices")

router = APIRouter()

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


def _get_holdings_with_symbol(category: str) -> List[dict]:
    """查询指定大类中有symbol的持仓"""
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT MAX(as_of) AS as_of FROM pams_holdings_snapshot")
            row = cur.fetchone()
            as_of = row["as_of"] if row else None
            if as_of is None:
                return []

            cur.execute("""
                SELECT id, symbol, name, quantity, buy_price, current_price, market_value
                FROM pams_holdings_snapshot
                WHERE as_of = %s AND major_category = %s
                  AND symbol IS NOT NULL AND symbol != ''
                  AND quantity IS NOT NULL AND quantity > 0
            """, (as_of, category))
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


# ---------- A股综合 (个股 + 基金) ----------

def _fetch_a_stock_prices(symbols: List[str]) -> Dict[str, float]:
    """获取A股价格，自动区分个股和基金
    - sh/sz开头 → 腾讯财经 (个股实时价格)
    - 6位数字 → 天天基金 (基金累计净值)
    """
    stock_syms = [s for s in symbols if s.startswith("sh") or s.startswith("sz")]
    fund_codes = [s for s in symbols if _is_fund_code(s)]

    prices = {}
    if stock_syms:
        prices.update(_fetch_stock_prices_tencent(stock_syms))
    if fund_codes:
        prices.update(_fetch_fund_navs(fund_codes))

    return prices


# ---------- 美股价格获取 (yfinance) ----------

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

    prices = {}
    try:
        tickers = yf.Tickers(" ".join(symbols))
        for sym in symbols:
            try:
                ticker = tickers.tickers.get(sym)
                if ticker is None:
                    continue
                fast = ticker.fast_info
                price = getattr(fast, "last_price", None)
                if price and price > 0:
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


# ---------- 通用刷新逻辑 ----------

def _refresh_category(category: str, fetch_fn) -> dict:
    """刷新指定类别的价格"""
    holdings = _get_holdings_with_symbol(category)
    if not holdings:
        return {"category": category, "found": 0, "updated": 0, "errors": []}

    symbols = [h["symbol"] for h in holdings]
    prices = fetch_fn(symbols)

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

    updated = _update_prices(updates)

    return {
        "category": category,
        "found": len(holdings),
        "updated": updated,
        "errors": errors,
    }


# ---------- API端点 ----------

@router.post("/prices/refresh/a-stock")
def refresh_a_stock():
    """刷新A股价格"""
    result = _refresh_category("A股", _fetch_a_stock_prices)
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
    """刷新贵金属价格（黄金基金等，使用天天基金累计净值）"""
    result = _refresh_category("贵金属", _fetch_fund_navs)
    return _json(result)


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
    for category, fetch_fn in [
        ("A股", _fetch_a_stock_prices),
        ("美股", _fetch_us_stock_prices),
        ("加密", _fetch_crypto_prices),
        ("贵金属", _fetch_fund_navs),
    ]:
        result = _refresh_category(category, fetch_fn)
        results.append(result)

    return _json({"results": results})
