"""
风险预算与仓位计算模块。
计算单笔风险、并发风险、熔断状态，以及基于 entry/stop 的仓位大小。
包含并发风险上限检查和主题/行业敞口上限检查。
"""
import logging
import math
from typing import Any, Dict, List, Tuple

import psycopg2.extras

from .config import get_b_nav, get_circuit_breakers, get_position_limits, get_risk_budget

logger = logging.getLogger(__name__)


def get_current_risk_state(
    conn,
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """
    获取当前风控状态。

    从 pams_risk_snapshot 获取最新快照，判断风险模式和熔断状态。

    Returns:
        {risk_mode, concurrent_risk_pct, circuit_breaker, can_open_new,
         open_positions, b_nav, reason_codes}
    """
    reason_codes = []

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT * FROM pams_risk_snapshot
            ORDER BY as_of DESC LIMIT 1
        """)
        snapshot = cur.fetchone()

    b_nav = get_b_nav(config)
    cb_config = get_circuit_breakers(config)
    limits = get_position_limits(config)

    # 读取宏观趋势状态
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT trend_state FROM pams_strategy_state
            ORDER BY as_of DESC LIMIT 1
        """)
        state_row = cur.fetchone()

    trend_state = state_row["trend_state"] if state_row else "RISK_NEUTRAL"

    # 默认值
    risk_mode = "defensive"
    concurrent_risk_pct = 0.0
    monthly_drawdown_pct = 0.0
    circuit_breaker = "normal"
    open_positions = 0

    if snapshot:
        concurrent_risk_pct = float(snapshot.get("concurrent_risk_pct", 0) or 0)
        monthly_drawdown_pct = float(snapshot.get("monthly_drawdown_pct", 0) or 0)
        circuit_breaker = snapshot.get("circuit_breaker", "normal")
        open_positions = int(snapshot.get("open_positions", 0) or 0)
        if snapshot.get("b_nav"):
            b_nav = float(snapshot["b_nav"])

    # RISK_OFF 强制 defensive 模式
    if trend_state == "RISK_OFF":
        risk_mode = "defensive"

    # 熔断检查
    pause_dd = cb_config.get("pause_new_entries_drawdown_pct", 0.04)
    reduce_dd = cb_config.get("reduce_only_drawdown_pct", 0.07)

    can_open_new = True

    if monthly_drawdown_pct >= reduce_dd:
        circuit_breaker = "reduce_only"
        can_open_new = False
        reason_codes.append("RC_R202_MONTHLY_CB_REDUCE_ONLY")
    elif monthly_drawdown_pct >= pause_dd:
        circuit_breaker = "pause"
        can_open_new = False
        reason_codes.append("RC_R201_MONTHLY_CB_PAUSE")

    # 持仓数量限制
    max_positions = limits.get("max_positions_total", 12)
    if open_positions >= max_positions:
        can_open_new = False

    reason_codes.append(
        "RC_R001_RISK_MODE_DEFENSIVE" if risk_mode == "defensive"
        else "RC_R002_RISK_MODE_ATTACK"
    )

    return {
        "risk_mode": risk_mode,
        "b_nav": b_nav,
        "trend_state": trend_state,
        "concurrent_risk_pct": concurrent_risk_pct,
        "monthly_drawdown_pct": monthly_drawdown_pct,
        "circuit_breaker": circuit_breaker,
        "can_open_new": can_open_new,
        "open_positions": open_positions,
        "reason_codes": reason_codes,
    }


def calculate_position_size(
    entry_price: float,
    stop_price: float,
    b_nav: float,
    risk_pct: float,
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """
    基于 entry/stop 计算仓位大小。

    position_size = floor(risk_amount / risk_per_share)
    并检查单笔市值占比限制。

    Returns:
        {position_size, risk_amount_cny, position_value_pct, max_loss_cny, reason_codes}
    """
    reason_codes = []
    limits = get_position_limits(config)

    risk_per_share = entry_price - stop_price
    if risk_per_share <= 0:
        logger.warning("risk_per_share <= 0: entry=%.2f stop=%.2f", entry_price, stop_price)
        return {
            "position_size": 0,
            "risk_amount_cny": 0,
            "position_value_pct": 0,
            "max_loss_cny": 0,
            "reason_codes": ["RC_T401_INVALID_LOW_BREAK"],
        }

    risk_amount = b_nav * risk_pct
    raw_size = risk_amount / risk_per_share
    # A 股最小交易单位 100 股
    position_size = max(int(math.floor(raw_size / 100)) * 100, 0)

    if position_size == 0:
        return {
            "position_size": 0,
            "risk_amount_cny": 0,
            "position_value_pct": 0,
            "max_loss_cny": 0,
            "reason_codes": reason_codes,
        }

    position_value = position_size * entry_price
    position_value_pct = position_value / b_nav if b_nav > 0 else 0
    max_loss_cny = position_size * risk_per_share

    # 单笔市值限制
    max_equity_pct = limits.get("max_single_position_value_pct", {}).get("equity", 0.10)  # v1.2: 10%
    if position_value_pct > max_equity_pct:
        # 缩减到限制内
        max_value = b_nav * max_equity_pct
        position_size = max(int(math.floor(max_value / entry_price / 100)) * 100, 0)
        position_value = position_size * entry_price
        position_value_pct = position_value / b_nav if b_nav > 0 else 0
        max_loss_cny = position_size * risk_per_share
        reason_codes.append("RC_R103_POSITION_VALUE_CAP")

    # v1.2: lot rounding 超限检查
    # 100股取整后实际风险可能 > 预算×1.2，需降1手或拒绝
    if position_size > 0:
        actual_risk = position_size * risk_per_share
        lot_rounding_cap = risk_amount * 1.2
        if actual_risk > lot_rounding_cap:
            position_size -= 100  # 降1手
            if position_size <= 0:
                return {
                    "position_size": 0,
                    "risk_amount_cny": 0,
                    "position_value_pct": 0,
                    "max_loss_cny": 0,
                    "reason_codes": [*reason_codes, "RC_R104_LOT_ROUNDING_EXCESS"],
                }
            # 重新计算降手后的值
            position_value = position_size * entry_price
            position_value_pct = position_value / b_nav if b_nav > 0 else 0
            max_loss_cny = position_size * risk_per_share
            # 再次检查是否仍超限
            if max_loss_cny > lot_rounding_cap:
                return {
                    "position_size": 0,
                    "risk_amount_cny": 0,
                    "position_value_pct": 0,
                    "max_loss_cny": 0,
                    "reason_codes": [*reason_codes, "RC_R104_LOT_ROUNDING_EXCESS"],
                }

    return {
        "position_size": position_size,
        "risk_amount_cny": round(max_loss_cny, 2),
        "position_value_pct": round(position_value_pct, 4),
        "max_loss_cny": round(max_loss_cny, 2),
        "reason_codes": reason_codes,
    }


def check_concurrent_risk(
    conn,
    config: Dict[str, Any],
    new_trade_risk_cny: float,
    risk_mode: str = "defensive",
) -> Tuple[bool, List[str]]:
    """
    检查新交易后并发风险是否超过上限。

    Args:
        new_trade_risk_cny: 新交易的风险金额 (position_size * (entry - stop))

    Returns:
        (ok: bool, reason_codes: list)
    """
    b_nav = get_b_nav(config)
    budget = get_risk_budget(config, risk_mode)
    cap = budget.get("concurrent_risk_pct_cap", 0.02)

    # 当前已有并发风险
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT COALESCE(SUM(position_size * (entry_price - stop_price)), 0) AS total_risk
            FROM pams_order_drafts
            WHERE status IN ('confirmed', 'executed')
        """)
        row = cur.fetchone()

    current_risk_cny = float(row["total_risk"] or 0)
    new_total = current_risk_cny + new_trade_risk_cny
    new_pct = new_total / b_nav if b_nav > 0 else 0

    if new_pct > cap:
        logger.info(
            "并发风险超限: 当前=%.2f%% + 新增=%.0f → %.2f%% > %.2f%%",
            current_risk_cny / b_nav * 100 if b_nav > 0 else 0,
            new_trade_risk_cny,
            new_pct * 100,
            cap * 100,
        )
        return False, ["RC_R101_CONCURRENT_RISK_CAP"]

    return True, []


def check_theme_exposure(
    conn,
    config: Dict[str, Any],
    symbol: str,
    new_position_value: float,
    risk_mode: str = "defensive",
) -> Tuple[bool, List[str]]:
    """
    检查同行业/主题敞口是否超过上限。

    通过 pams_holdings_snapshot 获取标的的行业分类，
    计算同行业已有持仓 + 新增仓位占 B_NAV 比例。

    Returns:
        (ok: bool, reason_codes: list)
    """
    b_nav = get_b_nav(config)
    budget = get_risk_budget(config, risk_mode)
    cap = budget.get("theme_exposure_pct_cap", 0.25)

    # 获取该标的的行业分类
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT minor_category FROM pams_holdings_snapshot
            WHERE symbol = %s AND major_category = 'A股'
            ORDER BY as_of DESC LIMIT 1
        """, (symbol,))
        cat_row = cur.fetchone()

    if not cat_row or not cat_row.get("minor_category"):
        # 无行业分类信息，跳过检查
        return True, []

    minor_category = cat_row["minor_category"]

    # 计算同行业已有持仓市值 (从 holdings_snapshot)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT COALESCE(SUM(market_value), 0) AS theme_value
            FROM pams_holdings_snapshot
            WHERE minor_category = %s
              AND major_category = 'A股'
              AND as_of = (SELECT MAX(as_of) FROM pams_holdings_snapshot)
        """, (minor_category,))
        row = cur.fetchone()

    current_value = float(row["theme_value"] or 0)
    new_total = current_value + new_position_value
    new_pct = new_total / b_nav if b_nav > 0 else 0

    if new_pct > cap:
        logger.info(
            "主题敞口超限 [%s]: 当前=%.0f + 新增=%.0f → %.1f%% > %.1f%%",
            minor_category,
            current_value,
            new_position_value,
            new_pct * 100,
            cap * 100,
        )
        return False, ["RC_R102_THEME_EXPOSURE_CAP"]

    return True, []
