"""
观察池管理模块。
提供板块扫描入池、退出条件检查、变更历史查询功能。
复用现有 indicators/fundamental/event_monitor/universe_filter 模块。
"""
import json
import logging
import time
from datetime import date
from typing import Any, Dict, List, Set, Tuple

import psycopg2.extras

from .config import load_strategy_config, get_satellite_cn_config
from .data_feed import fetch_and_store_batch, get_ohlcv_from_db
from .event_monitor import check_event_veto
from .fundamental import refresh_fundamental
from .indicators import compute_indicators, check_trend_gate
from .universe_filter import filter_blacklist, filter_liquidity, filter_st_delist

logger = logging.getLogger(__name__)

# 默认扫描板块列表（AkShare 行业板块名称）
DEFAULT_SECTORS = [
    "有色金属", "半导体", "银行", "消费电子",
    "新能源", "医药生物", "国防军工",
]

# 每板块最多取多少只成分股
MAX_PER_SECTOR = 20

# AkShare 请求间隔
AKSHARE_DELAY = 0.3


# ==================== 辅助函数 ====================

def _get_existing_symbols(conn) -> Set[str]:
    """获取观察池中已有的 symbol 集合。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol FROM pams_watchlist WHERE market = 'CN'"
        )
        return {r[0] for r in cur.fetchall()}


def _get_held_symbols(conn) -> Set[str]:
    """获取当前持仓的 symbol 集合。"""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT symbol FROM pams_holdings_snapshot
            WHERE major_category = 'A股'
              AND as_of = (SELECT MAX(as_of) FROM pams_holdings_snapshot)
        """)
        return {r[0] for r in cur.fetchall()}


def _get_sector_symbols(sector: str) -> List[str]:
    """通过 AkShare 获取某个行业板块的成分股（返回 DB 格式 symbol）。"""
    try:
        import akshare as ak
        df = ak.stock_board_industry_cons_em(symbol=sector)
        if df is None or df.empty:
            logger.warning("板块 %s: 无成分股数据", sector)
            return []

        symbols = []
        for _, row in df.head(MAX_PER_SECTOR).iterrows():
            code = str(row.get("代码", "")).strip()
            if not code or len(code) != 6:
                continue
            prefix = "sh" if code.startswith(("6", "5")) else "sz"
            symbols.append(f"{prefix}{code}")

        time.sleep(AKSHARE_DELAY)
        return symbols
    except Exception as e:
        logger.error("板块 %s 获取失败: %s", sector, e)
        return []


def _get_symbol_name(conn, symbol: str) -> str:
    """从持仓快照或 OHLCV 获取标的名称。"""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT name FROM pams_holdings_snapshot
            WHERE symbol = %s AND name IS NOT NULL
            ORDER BY as_of DESC LIMIT 1
        """, (symbol,))
        row = cur.fetchone()
    return row["name"] if row else symbol


def _log_watchlist_event(
    conn, symbol: str, market: str, name: str,
    action: str, old_status: str, new_status: str,
    reason: str, reason_codes: List[str], scan_date: str,
) -> None:
    """写入观察池变更日志。"""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO pams_watchlist_log
                (symbol, market, name, action, old_status, new_status,
                 reason, reason_codes, scan_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            symbol, market, name, action, old_status, new_status,
            reason, json.dumps(reason_codes), scan_date,
        ))


# ==================== 入池扫描 ====================

def run_watchlist_scan(conn) -> Dict[str, Any]:
    """
    板块轮动扫描 → IPS 筛选 → 自动入池。

    流程:
    1. 加载策略配置
    2. 遍历板块，获取成分股
    3. 排除已有/持仓/ST/黑名单
    4. 拉取行情 → 过滤流动性
    5. 逐标的: 趋势门 + 基本面 + 事件否决
    6. 通过者入池 + 写日志

    Returns: 扫描摘要
    """
    today = date.today().isoformat()

    config = load_strategy_config(conn)
    satellite = get_satellite_cn_config(config) if config else {}

    existing = _get_existing_symbols(conn)
    held = _get_held_symbols(conn)
    exclude = existing | held

    # 收集全部候选
    all_symbols: List[str] = []
    sectors_scanned = 0

    for sector in DEFAULT_SECTORS:
        syms = _get_sector_symbols(sector)
        fresh = [s for s in syms if s not in exclude]
        all_symbols.extend(fresh)
        sectors_scanned += 1
        logger.info("板块 %s: %d 只成分股, %d 只新候选", sector, len(syms), len(fresh))

    # 去重
    all_symbols = list(dict.fromkeys(all_symbols))
    total_before_filter = len(all_symbols)

    if not all_symbols:
        return {
            "scan_date": today,
            "sectors_scanned": sectors_scanned,
            "symbols_checked": 0,
            "new_entries": 0,
            "details": [],
        }

    # ST + 黑名单过滤
    all_symbols, _ = filter_st_delist(all_symbols)
    all_symbols, _ = filter_blacklist(conn, all_symbols)

    # 拉取行情
    logger.info("拉取 %d 只候选的 OHLCV 数据...", len(all_symbols))
    fetch_and_store_batch(conn, all_symbols, days=120)

    # 流动性过滤
    min_turnover = (
        satellite.get("universe", {})
        .get("liquidity_floor", {})
        .get("avg_turnover_20d_cny", 200_000_000)
    )
    all_symbols, _ = filter_liquidity(conn, all_symbols, min_turnover)

    # 逐标的检查
    new_entries = 0
    details: List[Dict[str, str]] = []

    for symbol in all_symbols:
        try:
            ohlcv = get_ohlcv_from_db(conn, symbol, days=120)
            ind = compute_indicators(ohlcv)
            if ind is None:
                continue

            # 趋势门
            trend_ok, _ = check_trend_gate(ind, satellite)
            if not trend_ok:
                continue

            # 基本面（容忍失败，默认通过）
            try:
                fund = refresh_fundamental(conn, symbol)
                quality = fund.get("quality_score", 50.0)
                if quality < 50:
                    continue
            except Exception as e:
                logger.warning("%s 基本面获取失败，跳过: %s", symbol, e)
                continue

            # 事件否决
            try:
                veto_hit, _ = check_event_veto(conn, symbol, days_back=7)
                if veto_hit:
                    continue
            except Exception:
                pass  # 容忍事件检查失败

            # 通过 → 入池
            name = _get_symbol_name(conn, symbol)
            reason = f"板块扫描入池 | 趋势通过 | 质量分{quality:.0f}"

            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO pams_watchlist (market, symbol, name, thesis, status)
                    VALUES ('CN', %s, %s, %s, 'watch')
                    ON CONFLICT (market, symbol) DO NOTHING
                    RETURNING id
                """, (symbol, name, reason))
                row = cur.fetchone()

            if row:
                _log_watchlist_event(
                    conn, symbol, "CN", name,
                    "entry", None, "watch",
                    reason, ["RC_WL_ENTRY_SECTOR_SCAN", "RC_WL_ENTRY_TREND_PASS"],
                    today,
                )
                conn.commit()
                new_entries += 1
                details.append({"symbol": symbol, "name": name, "reason": reason})
                logger.info("入池: %s %s", symbol, name)

        except Exception as e:
            logger.error("处理 %s 异常: %s", symbol, e)
            continue

    return {
        "scan_date": today,
        "sectors_scanned": sectors_scanned,
        "symbols_checked": total_before_filter,
        "symbols_after_filter": len(all_symbols),
        "new_entries": new_entries,
        "details": details,
    }


# ==================== 退出检查 ====================

def run_exit_check(conn) -> Dict[str, Any]:
    """
    逐只检查观察池标的是否触发退出条件。

    退出条件（任一触发即退出）:
    1. 趋势反转: MA20 < MA60
    2. 基本面恶化: quality_score < 30 或 cashflow_deterioration
    3. 事件风险: CRITICAL/HIGH 事件否决

    Returns: 检查摘要
    """
    today = date.today().isoformat()

    config = load_strategy_config(conn)
    satellite = get_satellite_cn_config(config) if config else {}

    # 获取需检查的标的
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT id, market, symbol, name, status
            FROM pams_watchlist
            WHERE status IN ('watch', 'candidate') AND market = 'CN'
        """)
        watchlist = cur.fetchall()

    if not watchlist:
        return {"checked": 0, "exits": 0, "exit_details": []}

    checked = 0
    exits = 0
    exit_details: List[Dict[str, Any]] = []

    for item in watchlist:
        symbol = item["symbol"]
        old_status = item["status"]
        name = item["name"] or symbol
        reasons: List[str] = []
        reason_codes: List[str] = []

        try:
            ohlcv = get_ohlcv_from_db(conn, symbol, days=120)
            ind = compute_indicators(ohlcv)

            # 条件1: 趋势反转
            if ind and ind["ma20"] < ind["ma60"]:
                reasons.append("趋势反转(MA20<MA60)")
                reason_codes.append("RC_WL_EXIT_TREND_REVERSAL")

            # 条件2: 基本面恶化（使用缓存优先）
            try:
                fund = _get_cached_fundamental(conn, symbol)
                if fund:
                    quality = fund.get("quality_score", 50)
                    cf_bad = fund.get("cashflow_deterioration", False)
                    if quality < 30:
                        reasons.append(f"基本面恶化(质量分{quality:.0f})")
                        reason_codes.append("RC_WL_EXIT_FUNDAMENTAL")
                    elif cf_bad:
                        reasons.append("现金流持续恶化")
                        reason_codes.append("RC_WL_EXIT_FUNDAMENTAL")
            except Exception:
                pass

            # 条件3: 事件风险
            try:
                veto_hit, veto_codes = check_event_veto(conn, symbol, days_back=7)
                if veto_hit:
                    reasons.append("事件风险触发")
                    reason_codes.append("RC_WL_EXIT_EVENT_RISK")
            except Exception:
                pass

            checked += 1

            if reason_codes:
                # 更新状态
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE pams_watchlist
                        SET status = 'avoid', updated_at = NOW()
                        WHERE id = %s
                    """, (item["id"],))

                _log_watchlist_event(
                    conn, symbol, "CN", name,
                    "exit", old_status, "avoid",
                    " | ".join(reasons), reason_codes, today,
                )
                conn.commit()

                exits += 1
                exit_details.append({
                    "symbol": symbol,
                    "name": name,
                    "reasons": reasons,
                    "reason_codes": reason_codes,
                })
                logger.info("退出: %s %s — %s", symbol, name, ", ".join(reasons))

        except Exception as e:
            logger.error("退出检查 %s 异常: %s", symbol, e)
            checked += 1

    return {
        "scan_date": today,
        "checked": checked,
        "exits": exits,
        "exit_details": exit_details,
    }


def _get_cached_fundamental(conn, symbol: str) -> Dict[str, Any]:
    """从 pams_fundamental_snapshot 取最近 7 天的缓存数据。"""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT quality_score, cashflow_deterioration
            FROM pams_fundamental_snapshot
            WHERE symbol = %s AND market = 'CN_A'
              AND as_of >= (CURRENT_DATE - INTERVAL '7 days')
            ORDER BY as_of DESC LIMIT 1
        """, (symbol,))
        return cur.fetchone() or {}


# ==================== 历史查询 ====================

def get_exit_history(conn, limit: int = 20) -> List[Dict[str, Any]]:
    """获取观察池退出历史记录。"""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT symbol, market, name, old_status, new_status,
                   reason, reason_codes, scan_date, created_at
            FROM pams_watchlist_log
            WHERE action = 'exit'
            ORDER BY created_at DESC
            LIMIT %s
        """, (limit,))
        return cur.fetchall()


def get_scan_log(conn, limit: int = 20) -> List[Dict[str, Any]]:
    """获取观察池入池记录。"""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT symbol, market, name, new_status,
                   reason, reason_codes, scan_date, created_at
            FROM pams_watchlist_log
            WHERE action = 'entry'
            ORDER BY created_at DESC
            LIMIT %s
        """, (limit,))
        return cur.fetchall()
