"""
宏观趋势状态模块。
从 yfinance 获取 SPX 月线数据，计算 10 月 MA，判定 RISK_ON/NEUTRAL/OFF。
写入 pams_strategy_state 供 scanner 和前端读取。
"""
import logging
from datetime import date
from typing import Any, Dict, Optional

import psycopg2.extras

logger = logging.getLogger(__name__)


def fetch_spx_monthly(months: int = 24) -> Optional[Dict[str, Any]]:
    """
    从 yfinance 获取 SPX 月线数据并计算 10 月 SMA。

    Returns:
        {spx_close, spx_ma10, months_data_count} 或 None
    """
    try:
        import yfinance as yf

        spx = yf.download("^GSPC", period=f"{months}mo", interval="1mo", progress=False)

        if spx.empty or len(spx) < 10:
            logger.warning("SPX 月线数据不足: %d 条 (需要 >= 10)", len(spx))
            return None

        closes = spx["Close"].dropna()
        if len(closes) < 10:
            return None

        # 扁平化多级列索引
        if hasattr(closes, "columns"):
            closes = closes.squeeze()

        spx_close = float(closes.iloc[-1])
        spx_ma10 = float(closes.rolling(10).mean().iloc[-1])

        return {
            "spx_close": round(spx_close, 2),
            "spx_ma10": round(spx_ma10, 2),
            "months_data_count": len(closes),
        }
    except ImportError:
        logger.error("yfinance 未安装")
        return None
    except Exception as e:
        logger.error("获取 SPX 月线数据失败: %s", e)
        return None


def compute_trend_state(
    spx_close: float,
    spx_ma10: float,
    prev_state: str = "RISK_NEUTRAL",
    prev_hysteresis: int = 0,
    hysteresis_months: int = 2,
) -> Dict[str, Any]:
    """
    根据 SPX 收盘价 vs 10月MA 判定趋势状态，带滞后机制。

    States: RISK_ON (SPX > MA10), RISK_OFF (SPX < MA10), RISK_NEUTRAL (过渡期)
    Hysteresis: 需要连续 N 个月确认才切换状态。

    Returns:
        {trend_state, hysteresis_months}
    """
    if spx_close > spx_ma10:
        candidate = "RISK_ON"
    else:
        candidate = "RISK_OFF"

    # 与当前状态一致：重置计数
    if candidate == prev_state:
        return {"trend_state": prev_state, "hysteresis_months": 0}

    # 不一致：增加滞后计数
    new_hysteresis = prev_hysteresis + 1

    if new_hysteresis >= hysteresis_months:
        # 滞后期满，切换状态
        return {"trend_state": candidate, "hysteresis_months": 0}

    # 仍在滞后期，维持 RISK_NEUTRAL 过渡
    if prev_state in ("RISK_ON", "RISK_OFF"):
        return {"trend_state": "RISK_NEUTRAL", "hysteresis_months": new_hysteresis}

    return {"trend_state": prev_state, "hysteresis_months": new_hysteresis}


def update_trend_state(conn) -> Dict[str, Any]:
    """
    获取 SPX 数据 → 计算趋势 → UPSERT pams_strategy_state。

    Returns:
        写入的 state dict，或 error dict
    """
    # 获取 SPX 数据
    spx_data = fetch_spx_monthly(months=24)
    if not spx_data:
        return {"error": "无法获取 SPX 月线数据"}

    # 读取上一次状态
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT trend_state, hysteresis_months
            FROM pams_strategy_state
            ORDER BY as_of DESC LIMIT 1
        """)
        prev = cur.fetchone()

    prev_state = prev["trend_state"] if prev else "RISK_NEUTRAL"
    prev_hysteresis = int(prev["hysteresis_months"] or 0) if prev else 0

    # 计算新状态
    result = compute_trend_state(
        spx_close=spx_data["spx_close"],
        spx_ma10=spx_data["spx_ma10"],
        prev_state=prev_state,
        prev_hysteresis=prev_hysteresis,
        hysteresis_months=2,
    )

    today = date.today()

    # UPSERT
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO pams_strategy_state
                (as_of, trend_state, spx_close, spx_ma10, hysteresis_months)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (as_of) DO UPDATE SET
                trend_state = EXCLUDED.trend_state,
                spx_close = EXCLUDED.spx_close,
                spx_ma10 = EXCLUDED.spx_ma10,
                hysteresis_months = EXCLUDED.hysteresis_months
        """, (
            today.isoformat(),
            result["trend_state"],
            spx_data["spx_close"],
            spx_data["spx_ma10"],
            result["hysteresis_months"],
        ))
        conn.commit()

    state = {
        "as_of": today.isoformat(),
        "trend_state": result["trend_state"],
        "spx_close": spx_data["spx_close"],
        "spx_ma10": spx_data["spx_ma10"],
        "hysteresis_months": result["hysteresis_months"],
    }

    logger.info(
        "宏观趋势已更新: SPX=%.2f MA10=%.2f → %s (滞后=%d)",
        spx_data["spx_close"],
        spx_data["spx_ma10"],
        result["trend_state"],
        result["hysteresis_months"],
    )

    return state
