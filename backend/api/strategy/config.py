"""
策略配置加载 + Reason Codes 枚举
从 pams_policy 表读取 IPS v1.1 配置，提供不可变的 reason code 追踪。
"""
import json
import logging
from typing import Any, Dict, List, Optional

import psycopg2.extras

logger = logging.getLogger(__name__)

# ==================== Reason Codes 枚举 ====================

REASON_CODES = {
    # Universe
    "RC_U001_ELIGIBLE": "Eligible by universe filters",
    "RC_U101_EXCLUDE_ST": "Excluded: ST/delisting risk",
    "RC_U102_EXCLUDE_SUSP": "Excluded: long suspension/abnormal halt",
    "RC_U103_LIQUIDITY_FAIL": "Excluded: liquidity below threshold",
    "RC_U104_COOLDOWN_ACTIVE": "Excluded: cooldown period active",
    "RC_U201_BLACKLIST_ACTIVE": "Excluded: blacklist active",
    # Fundamental
    "RC_F001_QUALITY_SCORE_HIGH": "Fundamental quality score high",
    "RC_F101_CASHFLOW_DETERIORATE": "Fundamental veto: persistent cashflow deterioration",
    "RC_F102_INDUSTRY_DOWNTREND": "Fundamental veto: industry regime clear downtrend",
    "RC_F201_EVENT_PRELOSS": "Event risk: pre-loss warning/profit warning",
    "RC_F202_EVENT_INVESTIGATION": "Event risk: investigation/major penalty",
    "RC_F203_EVENT_ABNORMAL_HALT": "Event risk: abnormal suspension",
    # Technical
    "RC_T001_TREND_GATE_PASS": "Trend gate pass",
    "RC_T101_TREND_GATE_FAIL": "Trend gate fail",
    "RC_T102_MA_TANGLE_REJECT": "Reject: MA20/MA60 too close (no trend)",
    "RC_T201_PULLBACK_ZONE_HIT": "Pullback zone hit",
    "RC_T202_PULLBACK_TRIGGER_OK": "Trigger day close>MA20 and close>open",
    "RC_T301_ENTRY_LIMIT_SET": "Entry limit placed",
    "RC_T401_INVALID_LOW_BREAK": "Invalidation: break pullback low/MA60",
    "RC_T402_VALIDITY_EXPIRED": "Invalidation: valid_until expired",
    # Flow
    "RC_FL001_PULLBACK_VOL_SHRINK": "Flow: pullback volume shrink",
    "RC_FL002_TRIGGER_VOL_POP": "Flow: trigger volume pop",
    "RC_FL101_HARD_FAIL_BIG_RED": "Flow hard fail: big red day with volume spike",
    "RC_FL201_FLOW_PASS": "Flow confirm PASS",
    "RC_FL202_FLOW_WEAK": "Flow confirm WEAK",
    "RC_FL203_FLOW_FAIL": "Flow confirm FAIL",
    # Sentiment
    "RC_S001_SENT_NEUTRAL": "Sentiment: neutral",
    "RC_S101_SENT_HOT": "Sentiment: hot (reduce size)",
    "RC_S102_SENT_PANIC": "Sentiment: panic (tighten rules)",
    # Hard Veto
    "RC_V001_BREAK_MA60_VOL_SPIKE": "Hard veto: break MA60 with volume spike",
    "RC_V002_CLOSE_BELOW_PULLBACK_BAND": "Hard veto: close below pullback zone lower band",
    "RC_V003_MAJOR_NEG_EVENT_TPLUS1": "Hard veto: major negative event within T+1",
    "RC_V004_LIQUIDITY_CRASH": "Hard veto: limit-down / liquidity crash",
    "RC_V005_PANIC_AND_INDEX_BREAK": "Hard veto: panic + index breakdown",
    "RC_V006_BLACKLIST": "Hard veto: blacklist active",
    # Risk
    "RC_R001_RISK_MODE_DEFENSIVE": "Risk mode: defensive",
    "RC_R002_RISK_MODE_ATTACK": "Risk mode: attack",
    "RC_R101_CONCURRENT_RISK_CAP": "Blocked: concurrent risk cap exceeded",
    "RC_R102_THEME_EXPOSURE_CAP": "Blocked: theme exposure cap exceeded",
    "RC_R103_POSITION_VALUE_CAP": "Blocked: single position value cap exceeded",
    "RC_R201_MONTHLY_CB_PAUSE": "Circuit breaker: pause new entries",
    "RC_R202_MONTHLY_CB_REDUCE_ONLY": "Circuit breaker: reduce only",
    # Execution
    "RC_E001_ORDER_DRAFT_CREATED": "Order draft created",
    "RC_E002_MANUAL_CONFIRM_REQUIRED": "Manual confirmation required",
    "RC_E003_AUTO_EXECUTED": "Auto executed",
    "RC_E101_KILL_SWITCH": "Kill switch triggered",
}


def add_reason(codes: List[str], code: str) -> List[str]:
    """不可变追加 reason code，返回新 list。"""
    return [*codes, code]


def add_reasons(codes: List[str], new_codes: List[str]) -> List[str]:
    """不可变追加多个 reason codes，返回新 list。"""
    return [*codes, *new_codes]


# ==================== 策略配置加载 ====================

def load_strategy_config(conn) -> Optional[Dict[str, Any]]:
    """从 pams_policy 表读取 IPS v1.1 策略配置。"""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT value FROM pams_policy WHERE key = %s",
            ("ips_v1",),
        )
        row = cur.fetchone()

    if not row or not row.get("value"):
        logger.warning("策略配置未找到 (key=ips_v1)")
        return None

    config = row["value"]
    if isinstance(config, str):
        config = json.loads(config)
    return config


def get_satellite_cn_config(config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """提取 satellite_cn 部分配置。"""
    satellite = config.get("satellite_cn")
    if not satellite:
        logger.warning("satellite_cn 配置缺失")
        return None
    if not satellite.get("enabled", False):
        logger.info("satellite_cn 已禁用")
        return None
    return satellite


def get_portfolio_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """提取 portfolio 风控配置。"""
    return config.get("portfolio", {})


def get_b_nav(config: Dict[str, Any]) -> float:
    """获取 B_NAV（可交易净值）。"""
    portfolio = get_portfolio_config(config)
    return float(portfolio.get("b_nav_override_cny", 0))


def get_risk_budget(config: Dict[str, Any], risk_mode: str = "defensive") -> Dict[str, float]:
    """获取指定风险模式下的风险预算参数。"""
    portfolio = get_portfolio_config(config)
    budget = portfolio.get("risk_budget", {})
    return {
        "per_trade_risk_pct": budget.get("per_trade_risk_pct", {}).get(risk_mode, 0.005),
        "concurrent_risk_pct_cap": budget.get("concurrent_risk_pct_cap", {}).get(risk_mode, 0.02),
        "theme_exposure_pct_cap": budget.get("theme_exposure_pct_cap", {}).get(risk_mode, 0.25),
    }


def get_position_limits(config: Dict[str, Any]) -> Dict[str, Any]:
    """获取持仓限制参数。"""
    portfolio = get_portfolio_config(config)
    return portfolio.get("limits", {})


def get_circuit_breakers(config: Dict[str, Any]) -> Dict[str, float]:
    """获取月度熔断参数。"""
    portfolio = get_portfolio_config(config)
    return portfolio.get("monthly_circuit_breakers", {})


def get_trend_filter_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """获取宏观趋势过滤器配置。"""
    core = config.get("core_policy", {})
    return core.get("trend_filter", {})
