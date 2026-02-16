"""
量价确认模块。
检查回调段缩量 + 触发日放量 + 大阴线否决，输出 PASS/WEAK/FAIL 评分。
"""
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def compute_flow_confirm(
    ohlcv: List[Dict[str, Any]],
    indicators: Dict[str, Any],
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """
    量价确认计算。

    指标:
    1. 回调段均量 < 冲击段均量 → 缩量回调
    2. 触发日成交量 > 1.2 × 回调段均量 → 放量触发
    3. 回调期间是否存在 ret < -2.5% 且 vol > 冲击段均量 → 大阴线否决

    Returns:
        {flow_result: "PASS"|"WEAK"|"FAIL", flow_score: float, reason_codes: list}
    """
    if len(ohlcv) < 20:
        return {
            "flow_result": "WEAK",
            "flow_score": 60.0,
            "reason_codes": ["RC_FL202_FLOW_WEAK"],
        }

    high_20d = indicators["high_20d"]
    reason_codes = []

    # 找到最近 20 日最高价对应的位置，划分冲击段/回调段
    recent = ohlcv[-40:] if len(ohlcv) >= 40 else ohlcv
    peak_idx = 0
    peak_high = 0
    for i, bar in enumerate(recent):
        if float(bar["high"]) >= peak_high:
            peak_high = float(bar["high"])
            peak_idx = i

    # 冲击段: peak_idx 之前的 bars（至少取 5 根）
    impulse_bars = recent[max(0, peak_idx - 10):peak_idx + 1]
    # 回调段: peak_idx 之后的 bars
    pullback_bars = recent[peak_idx + 1:]

    if not impulse_bars or not pullback_bars:
        return {
            "flow_result": "WEAK",
            "flow_score": 60.0,
            "reason_codes": ["RC_FL202_FLOW_WEAK"],
        }

    avg_vol_impulse = sum(float(b["volume"]) for b in impulse_bars) / len(impulse_bars)
    avg_vol_pullback = sum(float(b["volume"]) for b in pullback_bars) / len(pullback_bars)
    trigger_vol = float(ohlcv[-1]["volume"])

    # 指标 1: 缩量回调
    pullback_shrink = avg_vol_pullback < avg_vol_impulse
    if pullback_shrink:
        reason_codes.append("RC_FL001_PULLBACK_VOL_SHRINK")

    # 指标 2: 放量触发
    vol_pop = trigger_vol > avg_vol_pullback * 1.2 if avg_vol_pullback > 0 else False
    if vol_pop:
        reason_codes.append("RC_FL002_TRIGGER_VOL_POP")

    # 指标 3: 大阴线否决
    hard_fail = False
    for bar in pullback_bars:
        close = float(bar["close"])
        open_p = float(bar["open"])
        vol = float(bar["volume"])
        if open_p > 0:
            day_ret = (close - open_p) / open_p
            if day_ret < -0.025 and vol > avg_vol_impulse:
                hard_fail = True
                reason_codes.append("RC_FL101_HARD_FAIL_BIG_RED")
                break

    # 综合判定
    if hard_fail:
        flow_result = "FAIL"
        flow_score = 0.0
        reason_codes.append("RC_FL203_FLOW_FAIL")
    elif pullback_shrink and vol_pop:
        flow_result = "PASS"
        flow_score = 75.0
        reason_codes.append("RC_FL201_FLOW_PASS")
    elif pullback_shrink or vol_pop:
        flow_result = "WEAK"
        flow_score = 60.0
        reason_codes.append("RC_FL202_FLOW_WEAK")
    else:
        flow_result = "FAIL"
        flow_score = 30.0
        reason_codes.append("RC_FL203_FLOW_FAIL")

    return {
        "flow_result": flow_result,
        "flow_score": flow_score,
        "reason_codes": reason_codes,
    }
