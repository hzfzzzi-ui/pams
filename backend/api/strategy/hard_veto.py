"""
Hard Veto 检查模块。
实现 IPS v1.1.1 定义的 5 条 Hard Veto 规则，任一命中则否决候选。
所有函数为纯函数，不可变输入输出。
"""
import logging
from typing import Any, Dict, List, Tuple

import psycopg2.extras

logger = logging.getLogger(__name__)


def check_hard_veto(
    conn,
    indicators: Dict[str, Any],
    ohlcv: List[Dict[str, Any]],
    config: Dict[str, Any],
) -> Tuple[bool, List[str]]:
    """
    检查 5 条 Hard Veto 规则。

    Rules (from v1.1.1 satellite_cn.hard_veto.rules):
    1. V001: LOW < MA60 AND volume spike
    2. V002: CLOSE < pullback zone lower band
    3. V004: Consecutive limit-down or liquidity crash
    4. V005: RISK_OFF trend state (stub for SENT_PANIC + index breakdown)
    5. V006: Blacklist active

    Args:
        conn: DB connection (for blacklist + trend state lookup)
        indicators: 最新技术指标 dict
        ohlcv: OHLCV bar 列表 (按 trade_date ASC)
        config: satellite_cn 配置

    Returns:
        (veto_hit: bool, reason_codes: list)
    """
    reason_codes: List[str] = []

    # V001: LOW < MA60 AND volume > 1.5 × avg_vol_20d
    hit_v001 = _check_v001_ma60_vol_spike(indicators)
    if hit_v001:
        reason_codes.append("RC_V001_BREAK_MA60_VOL_SPIKE")

    # V002: CLOSE < MA20 - 1×ATR (pullback zone lower band)
    hit_v002 = _check_v002_below_pullback_band(indicators)
    if hit_v002:
        reason_codes.append("RC_V002_CLOSE_BELOW_PULLBACK_BAND")

    # V004: Consecutive limit-down or liquidity crash
    hit_v004 = _check_v004_liquidity_crash(ohlcv)
    if hit_v004:
        reason_codes.append("RC_V004_LIQUIDITY_CRASH")

    # V005: RISK_OFF state (proxy for SENT_PANIC + index breakdown)
    hit_v005 = _check_v005_panic_index_break(conn)
    if hit_v005:
        reason_codes.append("RC_V005_PANIC_AND_INDEX_BREAK")

    # V006: Blacklist active
    symbol = indicators.get("symbol", "")
    hit_v006 = _check_v006_blacklist(conn, symbol)
    if hit_v006:
        reason_codes.append("RC_V006_BLACKLIST")

    veto_hit = any([hit_v001, hit_v002, hit_v004, hit_v005, hit_v006])
    return veto_hit, reason_codes


def _check_v001_ma60_vol_spike(indicators: Dict[str, Any]) -> bool:
    """V001: LOW < MA60 AND 成交量 > 1.5 × 20日均量"""
    low = indicators.get("low", 0)
    ma60 = indicators.get("ma60", 0)
    volume = indicators.get("volume", 0)
    avg_vol_20d = indicators.get("avg_vol_20d", 0)

    if ma60 <= 0 or avg_vol_20d <= 0:
        return False

    return low < ma60 and volume > 1.5 * avg_vol_20d


def _check_v002_below_pullback_band(indicators: Dict[str, Any]) -> bool:
    """V002: CLOSE < MA20 - 1×ATR14 (回调区间下沿)"""
    close = indicators.get("close", 0)
    ma20 = indicators.get("ma20", 0)
    atr14 = indicators.get("atr14", 0)

    if ma20 <= 0 or atr14 <= 0:
        return False

    pullback_lower = ma20 - atr14
    return close < pullback_lower


def _check_v004_liquidity_crash(ohlcv: List[Dict[str, Any]]) -> bool:
    """
    V004: 连续跌停 or 流动性枯竭。
    检查最近 3 日是否存在涨跌停板 (|day_return| >= 9.5% 且 close==low 或 close==high)
    连续 2 天跌停视为流动性枯竭。
    """
    if len(ohlcv) < 3:
        return False

    recent = ohlcv[-3:]
    limit_down_count = 0

    for bar in recent:
        close = float(bar.get("close", 0))
        low = float(bar.get("low", 0))
        open_p = float(bar.get("open", 0))

        if open_p <= 0:
            continue

        day_ret = (close - open_p) / open_p

        # 跌停: 下跌 >= 9.5% 且 close 接近 low (允许 0.1% 误差)
        if day_ret <= -0.095 and close <= low * 1.001:
            limit_down_count += 1

    return limit_down_count >= 2


def _check_v005_panic_index_break(conn) -> bool:
    """
    V005: SENT_PANIC + index breakdown。
    P1 阶段以 RISK_OFF 趋势状态作为代理指标。
    完整实现需要 P3 的情绪分析模块。
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT trend_state FROM pams_strategy_state
            ORDER BY as_of DESC LIMIT 1
        """)
        row = cur.fetchone()

    if not row:
        return False

    return row["trend_state"] == "RISK_OFF"


def _check_v006_blacklist(conn, symbol: str) -> bool:
    """V006: 标的在活跃黑名单中。"""
    if not symbol:
        return False

    with conn.cursor() as cur:
        cur.execute("""
            SELECT 1 FROM pams_strategy_blacklist
            WHERE symbol = %s AND active = true
              AND (expires_at IS NULL OR expires_at >= CURRENT_DATE)
            LIMIT 1
        """, (symbol,))
        return cur.fetchone() is not None
