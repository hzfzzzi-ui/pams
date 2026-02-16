"""
风控快照计算模块。
从持仓和订单数据自动计算 B_NAV、并发风险、月度回撤、熔断状态，
写入 pams_risk_snapshot 供 scanner 和前端读取。
"""
import logging
from datetime import date
from typing import Any, Dict

import psycopg2.extras

from .config import get_b_nav, get_circuit_breakers

logger = logging.getLogger(__name__)


def compute_risk_snapshot(
    conn,
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """
    从持仓和订单数据计算当前风控快照。

    Returns:
        {as_of, b_nav, open_positions, concurrent_risk_pct,
         monthly_drawdown_pct, circuit_breaker}
    """
    today = date.today()
    b_nav = get_b_nav(config)
    cb_config = get_circuit_breakers(config)

    # 1. 已执行 + 已确认订单的持仓风险
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE status = 'executed') AS executed_count,
                COUNT(*) FILTER (WHERE status IN ('confirmed', 'executed')) AS active_count,
                COALESCE(SUM(
                    CASE WHEN status IN ('confirmed', 'executed')
                    THEN position_size * (entry_price - stop_price)
                    ELSE 0 END
                ), 0) AS total_risk_cny
            FROM pams_order_drafts
            WHERE status IN ('confirmed', 'executed')
        """)
        row = cur.fetchone()

    open_positions = int(row["active_count"] or 0)
    total_risk_cny = float(row["total_risk_cny"] or 0)
    concurrent_risk_pct = round(total_risk_cny / b_nav, 4) if b_nav > 0 else 0

    # 2. 月度回撤 (从 pams_nav_daily)
    month_start = today.replace(day=1).isoformat()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT
                MAX(total_market_value) AS peak_nav,
                (SELECT total_market_value FROM pams_nav_daily
                 ORDER BY as_of DESC LIMIT 1) AS current_nav
            FROM pams_nav_daily
            WHERE as_of >= %s
        """, (month_start,))
        nav_row = cur.fetchone()

    monthly_drawdown_pct = 0.0
    if nav_row and nav_row["peak_nav"] and nav_row["current_nav"]:
        peak = float(nav_row["peak_nav"])
        current = float(nav_row["current_nav"])
        if peak > 0:
            monthly_drawdown_pct = round(max(0, (peak - current) / peak), 4)

    # 3. 熔断状态
    pause_dd = cb_config.get("pause_new_entries_drawdown_pct", 0.04)
    reduce_dd = cb_config.get("reduce_only_drawdown_pct", 0.07)

    if monthly_drawdown_pct >= reduce_dd:
        circuit_breaker = "reduce_only"
    elif monthly_drawdown_pct >= pause_dd:
        circuit_breaker = "pause"
    else:
        circuit_breaker = "normal"

    return {
        "as_of": today.isoformat(),
        "b_nav": b_nav,
        "open_positions": open_positions,
        "concurrent_risk_pct": concurrent_risk_pct,
        "monthly_drawdown_pct": monthly_drawdown_pct,
        "circuit_breaker": circuit_breaker,
    }


def refresh_risk_snapshot(
    conn,
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """
    计算并写入风控快照到 pams_risk_snapshot。

    Returns:
        写入的快照 dict
    """
    snapshot = compute_risk_snapshot(conn, config)

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO pams_risk_snapshot
                (as_of, b_nav, open_positions, concurrent_risk_pct,
                 monthly_drawdown_pct, circuit_breaker)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (as_of) DO UPDATE SET
                b_nav = EXCLUDED.b_nav,
                open_positions = EXCLUDED.open_positions,
                concurrent_risk_pct = EXCLUDED.concurrent_risk_pct,
                monthly_drawdown_pct = EXCLUDED.monthly_drawdown_pct,
                circuit_breaker = EXCLUDED.circuit_breaker
        """, (
            snapshot["as_of"],
            snapshot["b_nav"],
            snapshot["open_positions"],
            snapshot["concurrent_risk_pct"],
            snapshot["monthly_drawdown_pct"],
            snapshot["circuit_breaker"],
        ))
        conn.commit()

    logger.info(
        "Risk snapshot 已刷新: B_NAV=%.0f, 持仓=%d, 并发风险=%.2f%%, "
        "月度回撤=%.2f%%, 熔断=%s",
        snapshot["b_nav"],
        snapshot["open_positions"],
        snapshot["concurrent_risk_pct"] * 100,
        snapshot["monthly_drawdown_pct"] * 100,
        snapshot["circuit_breaker"],
    )

    return snapshot
