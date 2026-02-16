"""
冷却期管理模块。
止损后 5 日冷却，双止损 20 日自动黑名单。
"""
import logging
from datetime import date, timedelta
from typing import Any, Dict, List, Tuple

import psycopg2.extras

logger = logging.getLogger(__name__)


def record_stop_loss(
    conn,
    symbol: str,
    stop_date: str,
    market: str = "CN_A",
    after_stop_days: int = 5,
    two_stops_blacklist_days: int = 20,
) -> Dict[str, Any]:
    """
    记录止损事件，设置冷却期或双止损黑名单。

    逻辑:
    1. 查询该 symbol 最近 20 天内是否已有止损记录
    2. 若已有 1 次 → 双止损: 加入黑名单 20 天
    3. 否则 → 单止损: 冷却 5 天

    Returns:
        {symbol, stop_count, cooldown_until, blacklist_until}
    """
    if not stop_date:
        stop_date = date.today().isoformat()
    stop_dt = date.fromisoformat(stop_date)
    lookback = (stop_dt - timedelta(days=two_stops_blacklist_days)).isoformat()

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # 查询最近 N 天内的止损记录
        cur.execute("""
            SELECT COUNT(*) AS cnt
            FROM pams_position_cooldown
            WHERE symbol = %s AND market = %s
              AND stop_date >= %s
        """, (symbol, market, lookback))
        row = cur.fetchone()
        prev_count = int(row["cnt"]) if row else 0

    stop_count = prev_count + 1
    cooldown_until = (stop_dt + timedelta(days=after_stop_days)).isoformat()
    blacklist_until = None

    if prev_count >= 1:
        # 双止损 → 黑名单 20 天
        blacklist_until = (stop_dt + timedelta(days=two_stops_blacklist_days)).isoformat()

        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO pams_strategy_blacklist
                    (symbol, market, reason, expires_at, active)
                VALUES (%s, %s, %s, %s, true)
            """, (
                symbol, market,
                f"双止损自动黑名单 (第{stop_count}次)",
                blacklist_until,
            ))

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO pams_position_cooldown
                (symbol, market, stop_date, stop_count, cooldown_until, blacklist_until, is_active)
            VALUES (%s, %s, %s, %s, %s, %s, true)
        """, (
            symbol, market, stop_date,
            stop_count, cooldown_until, blacklist_until,
        ))
        conn.commit()

    logger.info(
        "%s: 止损记录 — 第%d次, 冷却至%s, 黑名单至%s",
        symbol, stop_count, cooldown_until, blacklist_until or "无",
    )

    return {
        "symbol": symbol,
        "stop_count": stop_count,
        "cooldown_until": cooldown_until,
        "blacklist_until": blacklist_until,
    }


def check_cooldown(
    conn,
    symbol: str,
    market: str = "CN_A",
) -> Tuple[bool, List[str]]:
    """
    检查标的是否处于冷却期。

    Returns:
        (blocked, reason_codes)
    """
    today = date.today().isoformat()

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT cooldown_until, blacklist_until
            FROM pams_position_cooldown
            WHERE symbol = %s AND market = %s AND is_active = true
              AND cooldown_until >= %s
            ORDER BY cooldown_until DESC
            LIMIT 1
        """, (symbol, market, today))
        row = cur.fetchone()

    if not row:
        return False, []

    reason_codes = []
    if row.get("blacklist_until") and str(row["blacklist_until"]) >= today:
        reason_codes.append("RC_U201_BLACKLIST_ACTIVE")
    else:
        reason_codes.append("RC_U104_COOLDOWN_ACTIVE")

    return True, reason_codes


def expire_cooldowns(conn) -> int:
    """
    将过期的冷却记录标记为 inactive。

    Returns:
        过期数量
    """
    today = date.today().isoformat()

    with conn.cursor() as cur:
        cur.execute("""
            UPDATE pams_position_cooldown
            SET is_active = false
            WHERE is_active = true AND cooldown_until < %s
        """, (today,))
        count = cur.rowcount
        conn.commit()

    if count > 0:
        logger.info("冷却期过期: %d 条", count)

    return count
