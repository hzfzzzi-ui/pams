"""
AkShare OHLCV 数据获取与存储。
负责从 AkShare 拉取 A 股前复权日线数据，批量 UPSERT 到 pams_ohlcv_daily。
"""
import logging
import time
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import psycopg2.extras

logger = logging.getLogger(__name__)

# AkShare 请求间隔（秒），避免频率限制
AKSHARE_DELAY = 0.3


def _symbol_to_akshare(symbol: str) -> str:
    """将 DB 中的 symbol (如 sh600519 / sz002594) 转为 AkShare 格式 (600519 / 002594)。"""
    s = symbol.strip().lower()
    if s.startswith(("sh", "sz")):
        return s[2:]
    return s


def _symbol_to_db(code: str, name: str = "") -> str:
    """将 AkShare 纯数字代码转为 DB 格式 (sh/sz 前缀)。"""
    code = code.strip()
    if code.startswith(("6", "5")):
        return f"sh{code}"
    return f"sz{code}"


def fetch_ohlcv_akshare(
    symbol: str,
    days: int = 120,
    end_date: Optional[date] = None,
) -> List[Dict[str, Any]]:
    """
    通过 AkShare 获取 A 股前复权日线 OHLCV。

    Args:
        symbol: 股票代码（支持 sh600519 / 600519 格式）
        days: 获取天数
        end_date: 截止日期，默认今天

    Returns:
        标准化 OHLCV dict 列表，按 trade_date ASC 排序
    """
    import akshare as ak

    code = _symbol_to_akshare(symbol)
    db_symbol = _symbol_to_db(code)

    if end_date is None:
        end_date = date.today()
    start_date = end_date - timedelta(days=days + 30)  # 多取 30 天缓冲

    try:
        df = ak.stock_zh_a_hist(
            symbol=code,
            period="daily",
            start_date=start_date.strftime("%Y%m%d"),
            end_date=end_date.strftime("%Y%m%d"),
            adjust="qfq",
        )
    except Exception as e:
        logger.error("AkShare 获取失败 %s: %s", symbol, e)
        return []

    if df is None or df.empty:
        logger.warning("AkShare 无数据: %s", symbol)
        return []

    rows = []
    for _, r in df.iterrows():
        rows.append({
            "symbol": db_symbol,
            "market": "CN_A",
            "trade_date": str(r["日期"])[:10],
            "open": float(r["开盘"]),
            "high": float(r["最高"]),
            "low": float(r["最低"]),
            "close": float(r["收盘"]),
            "volume": int(r["成交量"]),
            "turnover": float(r.get("成交额", 0)),
        })

    return sorted(rows, key=lambda x: x["trade_date"])


def store_ohlcv(conn, rows: List[Dict[str, Any]]) -> int:
    """
    批量 UPSERT OHLCV 数据到 pams_ohlcv_daily。

    Returns:
        存储的行数
    """
    if not rows:
        return 0

    with conn.cursor() as cur:
        values = [
            (
                r["symbol"], r["market"], r["trade_date"],
                r["open"], r["high"], r["low"], r["close"],
                r["volume"], r["turnover"],
            )
            for r in rows
        ]
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO pams_ohlcv_daily
                (symbol, market, trade_date, open, high, low, close, volume, turnover)
            VALUES %s
            ON CONFLICT (symbol, market, trade_date)
            DO UPDATE SET
                open = EXCLUDED.open,
                high = EXCLUDED.high,
                low = EXCLUDED.low,
                close = EXCLUDED.close,
                volume = EXCLUDED.volume,
                turnover = EXCLUDED.turnover
            """,
            values,
            page_size=500,
        )
        conn.commit()
    return len(values)


def fetch_and_store_batch(
    conn,
    symbols: List[str],
    days: int = 120,
) -> Dict[str, int]:
    """
    批量获取并存储 OHLCV 数据。

    Returns:
        {symbol: row_count} 汇总
    """
    result = {}
    for i, symbol in enumerate(symbols):
        try:
            rows = fetch_ohlcv_akshare(symbol, days=days)
            stored = store_ohlcv(conn, rows)
            result[symbol] = stored
            logger.info("[%d/%d] %s: %d 条", i + 1, len(symbols), symbol, stored)
        except Exception as e:
            logger.error("[%d/%d] %s 失败: %s", i + 1, len(symbols), symbol, e)
            result[symbol] = 0

        if i < len(symbols) - 1:
            time.sleep(AKSHARE_DELAY)

    return result


def get_ohlcv_from_db(
    conn,
    symbol: str,
    days: int = 120,
    market: str = "CN_A",
) -> List[Dict[str, Any]]:
    """
    从 DB 读取历史 OHLCV，按 trade_date ASC 排序。

    Returns:
        OHLCV dict 列表
    """
    cutoff = date.today() - timedelta(days=days + 30)

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT symbol, market, trade_date, open, high, low, close, volume, turnover
            FROM pams_ohlcv_daily
            WHERE symbol = %s AND market = %s AND trade_date >= %s
            ORDER BY trade_date ASC
            """,
            (symbol, market, cutoff),
        )
        rows = cur.fetchall()

    return [
        {
            "symbol": r["symbol"],
            "market": r["market"],
            "trade_date": r["trade_date"].isoformat() if hasattr(r["trade_date"], "isoformat") else str(r["trade_date"]),
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
            "volume": int(r["volume"]),
            "turnover": float(r["turnover"] or 0),
        }
        for r in rows
    ]
