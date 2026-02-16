"""
技术指标计算模块。
使用 pandas 计算 MA20/MA60/ATR14 等指标，并实现趋势门/回调区间/触发日检查。
所有函数均为纯函数，不可变输入输出。
"""
import logging
import math
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

# 计算所需最少日线数量
MIN_BARS_REQUIRED = 65


def compute_indicators(ohlcv: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    从 OHLCV 列表计算技术指标。

    Args:
        ohlcv: OHLCV dict 列表，按 trade_date ASC 排序，至少 65 条

    Returns:
        最新一行的指标 dict，数据不足返回 None
    """
    if len(ohlcv) < MIN_BARS_REQUIRED:
        logger.warning("数据不足: %d 条 (需要 %d)", len(ohlcv), MIN_BARS_REQUIRED)
        return None

    df = pd.DataFrame(ohlcv)
    df["close"] = df["close"].astype(float)
    df["high"] = df["high"].astype(float)
    df["low"] = df["low"].astype(float)
    df["open"] = df["open"].astype(float)
    df["volume"] = df["volume"].astype(float)

    # MA
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma60"] = df["close"].rolling(60).mean()

    # ATR14: True Range = max(H-L, |H-prevC|, |L-prevC|)
    prev_close = df["close"].shift(1)
    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - prev_close).abs()
    tr3 = (df["low"] - prev_close).abs()
    df["tr"] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df["atr14"] = df["tr"].rolling(14).mean()

    # 20日最高/最低
    df["high_20d"] = df["high"].rolling(20).max()
    df["low_20d"] = df["low"].rolling(20).min()

    # 40日最大涨幅: (当前收盘 / 40日内最低收盘 - 1)
    df["min_close_40d"] = df["close"].rolling(40).min()
    df["max_return_40d"] = df["close"] / df["min_close_40d"] - 1

    # 20日均量
    df["avg_vol_20d"] = df["volume"].rolling(20).mean()

    last = df.iloc[-1]

    if pd.isna(last["ma60"]) or pd.isna(last["atr14"]):
        logger.warning("指标计算结果含 NaN，数据可能不足")
        return None

    return {
        "symbol": ohlcv[-1].get("symbol", ""),
        "trade_date": ohlcv[-1].get("trade_date", ""),
        "close": float(last["close"]),
        "open": float(last["open"]),
        "high": float(last["high"]),
        "low": float(last["low"]),
        "volume": float(last["volume"]),
        "ma20": round(float(last["ma20"]), 4),
        "ma60": round(float(last["ma60"]), 4),
        "atr14": round(float(last["atr14"]), 4),
        "high_20d": round(float(last["high_20d"]), 4),
        "low_20d": round(float(last["low_20d"]), 4),
        "max_return_40d": round(float(last["max_return_40d"]), 4),
        "avg_vol_20d": round(float(last["avg_vol_20d"]), 0),
    }


def check_trend_gate(
    indicators: Dict[str, Any],
    config: Dict[str, Any],
) -> Tuple[bool, List[str]]:
    """
    趋势门检查。

    Require: MA20 > MA60, CLOSE > MA60, MAX_RETURN_40D >= 0.12
    Reject:  ABS(MA20 - MA60) / CLOSE < 0.015

    Returns:
        (passed: bool, reason_codes: list)
    """
    tech_config = config.get("technical_engine", {})
    gate = tech_config.get("trend_gate", {})

    ma20 = indicators["ma20"]
    ma60 = indicators["ma60"]
    close = indicators["close"]
    max_ret = indicators["max_return_40d"]

    min_return = 0.12
    ma_tangle_threshold = 0.015

    # 从 config 提取参数（如果有）
    requires = gate.get("require", [])
    rejects = gate.get("reject_if", [])

    reasons = []

    # 检查 MA 纠缠（拒绝条件）
    if close > 0 and abs(ma20 - ma60) / close < ma_tangle_threshold:
        reasons.append("RC_T102_MA_TANGLE_REJECT")
        return False, reasons

    # 检查趋势条件
    passed = True

    if ma20 <= ma60:
        passed = False
    if close <= ma60:
        passed = False
    if max_ret < min_return:
        passed = False

    if passed:
        reasons.append("RC_T001_TREND_GATE_PASS")
    else:
        reasons.append("RC_T101_TREND_GATE_FAIL")

    return passed, reasons


def check_pullback_zone(
    indicators: Dict[str, Any],
    config: Dict[str, Any],
) -> Tuple[bool, List[str]]:
    """
    回调区间检查。

    Zone: MA20 ± 1 × ATR14
    Invalidation: LOW < MA60

    Returns:
        (in_zone: bool, reason_codes: list)
    """
    tech_config = config.get("technical_engine", {})
    pullback = tech_config.get("pullback_setup", {})

    ma20 = indicators["ma20"]
    ma60 = indicators["ma60"]
    atr = indicators["atr14"]
    close = indicators["close"]
    low = indicators["low"]

    band_atr = pullback.get("pullback_zone", {}).get("band_atr", 1.0)

    reasons = []

    # 无效化检查: LOW < MA60
    if low < ma60:
        reasons.append("RC_T401_INVALID_LOW_BREAK")
        return False, reasons

    # 回调区间: MA20 - band_atr*ATR <= CLOSE <= MA20 + band_atr*ATR
    zone_upper = ma20 + band_atr * atr
    zone_lower = ma20 - band_atr * atr

    in_zone = zone_lower <= close <= zone_upper

    if in_zone:
        reasons.append("RC_T201_PULLBACK_ZONE_HIT")
    else:
        reasons.append("RC_T101_TREND_GATE_FAIL")

    return in_zone, reasons


def check_trigger_day(indicators: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """
    触发日条件检查。

    Require: CLOSE > MA20 AND CLOSE > OPEN

    Returns:
        (triggered: bool, reason_codes: list)
    """
    close = indicators["close"]
    open_price = indicators["open"]
    ma20 = indicators["ma20"]

    reasons = []

    triggered = close > ma20 and close > open_price

    if triggered:
        reasons.append("RC_T202_PULLBACK_TRIGGER_OK")

    return triggered, reasons


def calc_entry_stop(
    indicators: Dict[str, Any],
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """
    计算入场价和止损价。

    Entry: CLOSE × (1 - 0.002)
    Stop:  min(ENTRY - 2×ATR, LOW_20D - 0.5×ATR)

    Returns:
        {entry_price, stop_price, valid_until, reason_codes}
    """
    tech_config = config.get("technical_engine", {})
    pullback = tech_config.get("pullback_setup", {})
    stop_config = tech_config.get("stop", {})

    close = indicators["close"]
    atr = indicators["atr14"]
    low_20d = indicators["low_20d"]

    # Entry
    offset_pct = pullback.get("entry", {}).get("price_offset_pct", -0.002)
    entry_price = round(close * (1 + offset_pct), 2)

    # Stop: min of two rules
    rule_a_mult = stop_config.get("rule_a", {}).get("mult", 2.0)
    rule_b_mult = stop_config.get("rule_b", {}).get("mult", 0.5)

    stop_a = entry_price - rule_a_mult * atr
    stop_b = low_20d - rule_b_mult * atr
    stop_price = round(min(stop_a, stop_b), 2)

    # Valid until
    valid_days = pullback.get("valid_until_days", 2)
    valid_until = (date.today() + timedelta(days=valid_days)).isoformat()

    return {
        "entry_price": entry_price,
        "stop_price": stop_price,
        "valid_until": valid_until,
        "reason_codes": ["RC_T301_ENTRY_LIMIT_SET"],
    }
