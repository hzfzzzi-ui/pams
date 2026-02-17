"""
Universe 筛选模块。
从持仓/观察池获取候选 symbol，过滤 ST/黑名单/流动性不足的标的。
"""
import logging
from typing import Any, Dict, List, Tuple

import psycopg2.extras

logger = logging.getLogger(__name__)


def get_universe_symbols(conn) -> Dict[str, str]:
    """
    从持仓和观察池收集 A 股候选 symbol，标注来源。

    来源:
    1. pams_holdings_snapshot: major_category='A股' 的 symbol
    2. pams_watchlist: market='CN' 的 symbol

    Returns:
        {symbol: "holdings"|"watchlist"|"both"}
    """
    holdings_set: set = set()
    watchlist_set: set = set()

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT DISTINCT symbol
            FROM pams_holdings_snapshot
            WHERE major_category = 'A股'
              AND symbol IS NOT NULL AND symbol != ''
              AND as_of = (SELECT MAX(as_of) FROM pams_holdings_snapshot)
        """)
        for row in cur.fetchall():
            holdings_set.add(row["symbol"])

        cur.execute("""
            SELECT DISTINCT symbol
            FROM pams_watchlist
            WHERE market = 'CN' AND symbol IS NOT NULL AND symbol != ''
        """)
        for row in cur.fetchall():
            watchlist_set.add(row["symbol"])

    result: Dict[str, str] = {}
    for s in sorted(holdings_set | watchlist_set):
        in_h = s in holdings_set
        in_w = s in watchlist_set
        result[s] = "both" if (in_h and in_w) else ("holdings" if in_h else "watchlist")

    logger.info("Universe 收集: %d 个 symbol (持仓%d, 观察池%d)",
                len(result), len(holdings_set), len(watchlist_set))
    return result


def filter_st_delist(symbols: List[str]) -> Tuple[List[str], List[Tuple[str, str]]]:
    """
    过滤 ST/退市风险标的。

    通过 AkShare 获取当前 ST 名单，排除匹配的 symbol。

    Returns:
        (通过列表, [(symbol, reason_code)] 排除记录)
    """
    try:
        import akshare as ak
        st_df = ak.stock_zh_a_st_em()
        if st_df is not None and not st_df.empty:
            st_codes = set()
            for _, row in st_df.iterrows():
                code = str(row.get("代码", "")).strip()
                if code.startswith(("6", "5")):
                    st_codes.add(f"sh{code}")
                else:
                    st_codes.add(f"sz{code}")
        else:
            st_codes = set()
    except Exception as e:
        logger.error("获取 ST 名单失败: %s", e)
        st_codes = set()

    passed = []
    excluded = []

    for s in symbols:
        if s in st_codes:
            excluded.append((s, "RC_U101_EXCLUDE_ST"))
        else:
            passed.append(s)

    logger.info("ST 过滤: %d 通过, %d 排除", len(passed), len(excluded))
    return passed, excluded


def filter_blacklist(
    conn,
    symbols: List[str],
) -> Tuple[List[str], List[Tuple[str, str]]]:
    """
    过滤黑名单标的。

    查询 pams_strategy_blacklist 中 active=true 且未过期的记录。

    Returns:
        (通过列表, [(symbol, reason_code)] 排除记录)
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT DISTINCT symbol
            FROM pams_strategy_blacklist
            WHERE active = true
              AND market = 'CN_A'
              AND (expires_at IS NULL OR expires_at >= CURRENT_DATE)
        """)
        blacklisted = {row["symbol"] for row in cur.fetchall()}

    passed = []
    excluded = []

    for s in symbols:
        if s in blacklisted:
            excluded.append((s, "RC_U201_BLACKLIST_ACTIVE"))
        else:
            passed.append(s)

    logger.info("黑名单过滤: %d 通过, %d 排除", len(passed), len(excluded))
    return passed, excluded


def filter_liquidity(
    conn,
    symbols: List[str],
    min_turnover: float = 200_000_000,
) -> Tuple[List[str], List[Tuple[str, str]]]:
    """
    过滤流动性不足的标的。

    检查 pams_ohlcv_daily 近 20 日均成交额是否达到阈值。

    Args:
        min_turnover: 最低 20 日均成交额（元），默认 2 亿

    Returns:
        (通过列表, [(symbol, reason_code)] 排除记录)
    """
    if not symbols:
        return [], []

    passed = []
    excluded = []

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        for s in symbols:
            cur.execute("""
                SELECT AVG(turnover) AS avg_turnover
                FROM (
                    SELECT turnover
                    FROM pams_ohlcv_daily
                    WHERE symbol = %s AND market = 'CN_A'
                    ORDER BY trade_date DESC
                    LIMIT 20
                ) sub
            """, (s,))
            row = cur.fetchone()
            avg = float(row["avg_turnover"] or 0) if row else 0

            if avg >= min_turnover:
                passed.append(s)
            else:
                excluded.append((s, "RC_U103_LIQUIDITY_FAIL"))

    logger.info("流动性过滤: %d 通过, %d 排除 (阈值 %.0f)", len(passed), len(excluded), min_turnover)
    return passed, excluded
