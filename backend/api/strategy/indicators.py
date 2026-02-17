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


def _compute_ma20_slope(df: pd.DataFrame, lookback: int = 5) -> float:
    """
    MA20 的 5 日斜率 = (MA20_today - MA20_{lookback}d_ago) / lookback。

    斜率 > 0 表示 MA20 仍在上行，是趋势延续的必要条件。
    """
    if len(df) < lookback + 20:
        return 0.0
    ma20_today = df["ma20"].iloc[-1]
    ma20_prev = df["ma20"].iloc[-1 - lookback]
    if pd.isna(ma20_today) or pd.isna(ma20_prev):
        return 0.0
    return (ma20_today - ma20_prev) / lookback


def _compute_rs_vs_index(
    sym_ohlcv: List[Dict[str, Any]],
    idx_ohlcv: List[Dict[str, Any]],
    lookback: int = 60,
) -> float:
    """
    60 日相对强度 RS = stock_ret_60d - index_ret_60d。

    RS >= 0 表示标的跑赢/持平基准（HS300），用于趋势门过滤。
    无指数数据时返回 -1.0（保守拒绝）。
    """
    if not idx_ohlcv or len(idx_ohlcv) < lookback:
        return -1.0
    if len(sym_ohlcv) < lookback:
        return -1.0

    # 取最近 lookback 根
    sym_close_now = float(sym_ohlcv[-1]["close"])
    sym_close_prev = float(sym_ohlcv[-lookback]["close"])
    sym_ret = (sym_close_now / sym_close_prev - 1) if sym_close_prev > 0 else 0.0

    idx_close_now = float(idx_ohlcv[-1]["close"])
    idx_close_prev = float(idx_ohlcv[-lookback]["close"])
    idx_ret = (idx_close_now / idx_close_prev - 1) if idx_close_prev > 0 else 0.0

    return sym_ret - idx_ret


def compute_indicators(
    ohlcv: List[Dict[str, Any]],
    index_ohlcv: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """
    从 OHLCV 列表计算技术指标。

    Args:
        ohlcv: OHLCV dict 列表，按 trade_date ASC 排序，至少 65 条
        index_ohlcv: 基准指数 OHLCV（v1.2 新增，用于 RS 计算）

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

    # v1.2: MA20 斜率
    ma20_slope = _compute_ma20_slope(df, lookback=5)

    # v1.2: RS 相对强度
    rs = _compute_rs_vs_index(ohlcv, index_ohlcv or [], lookback=60)

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
        "ma20_slope": round(ma20_slope, 6),
        "rs": round(rs, 4),
    }


def check_trend_gate(
    indicators: Dict[str, Any],
    config: Dict[str, Any],
) -> Tuple[bool, List[str]]:
    """
    趋势门检查 (v1.2)。

    Require: MA20 > MA60, CLOSE > MA60, MA20_slope > 0, RS >= 0
    Reject:  ABS(MA20 - MA60) / CLOSE < 0.008

    v1.2 变更: 移除 MAX_RETURN_40D >= 0.12，新增 MA20 斜率和 RS 过滤。

    Returns:
        (passed: bool, reason_codes: list)
    """
    ma20 = indicators["ma20"]
    ma60 = indicators["ma60"]
    close = indicators["close"]
    ma20_slope = indicators.get("ma20_slope", 0.0)
    rs = indicators.get("rs", -1.0)

    ma_tangle_threshold = 0.008  # v1.2: 松弛（v1.1.1=0.015）

    reasons = []

    # 检查 MA 纠缠（拒绝条件）— 先检查，快速排除
    if close > 0 and abs(ma20 - ma60) / close < ma_tangle_threshold:
        reasons.append("RC_T102_MA_TANGLE_REJECT")
        return False, reasons

    # v1.2: MA20 斜率 > 0（MA20 仍在上行）
    if ma20_slope <= 0:
        reasons.append("RC_T104_MA20_SLOPE_NEG")
        return False, reasons

    # v1.2: RS >= 0（跑赢/持平基准指数）
    if rs < 0:
        reasons.append("RC_T103_RS_BELOW_ZERO")
        return False, reasons

    # 基础趋势条件
    passed = True

    if ma20 <= ma60:
        passed = False
    if close <= ma60:
        passed = False

    if passed:
        reasons.append("RC_T001_TREND_GATE_PASS")
    else:
        reasons.append("RC_T101_TREND_GATE_FAIL")

    return passed, reasons


def check_pullback_zone(
    ohlcv: List[Dict[str, Any]],
    indicators: Dict[str, Any],
    config: Dict[str, Any],
) -> Tuple[bool, List[str]]:
    """
    回调区间检查 (v1.2: 时序解耦)。

    过去 touch_window_days (默认5) 个交易日内，任一日 LOW 触及 MA20±ATR 区间
    即满足 "touch" 条件。当日不要求仍在回调区间内。

    Invalidation: 当日 LOW < MA60

    Returns:
        (touched: bool, reason_codes: list)
    """
    tech_config = config.get("technical_engine", {})
    pullback = tech_config.get("pullback_setup", {})

    ma20 = indicators["ma20"]
    ma60 = indicators["ma60"]
    atr = indicators["atr14"]
    low = indicators["low"]

    band_atr = pullback.get("pullback_zone", {}).get("band_atr", 1.0)
    touch_window = pullback.get("touch_window_days", 5)

    reasons = []

    # 无效化检查: 当日 LOW < MA60
    if low < ma60:
        reasons.append("RC_T401_INVALID_LOW_BREAK")
        return False, reasons

    # v1.2: 回看 touch_window 根 bar，检查 LOW 是否触及 MA20±ATR
    # 注意: MA20/ATR 用当日值近似（5日内变化 <1%）
    zone_upper = ma20 + band_atr * atr
    zone_lower = ma20 - band_atr * atr

    recent_bars = ohlcv[-touch_window:] if len(ohlcv) >= touch_window else ohlcv
    touched = False

    for bar in recent_bars:
        bar_low = float(bar["low"])
        if zone_lower <= bar_low <= zone_upper:
            touched = True
            break

    if touched:
        reasons.append("RC_T203_PULLBACK_TOUCH_5D")
        reasons.append("RC_T201_PULLBACK_ZONE_HIT")
    else:
        reasons.append("RC_T204_NO_PULLBACK_TOUCH")

    return touched, reasons


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
    ohlcv: List[Dict[str, Any]],
    indicators: Dict[str, Any],
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """
    计算入场价和止损价 (v1.2)。

    Entry: CLOSE × (1 - 0.002)
    Stop:  max(pullback_low - 0.5×ATR, ENTRY - 2.2×ATR)  → 取更紧止损
    Reject: (entry - stop) / entry > 12%

    v1.2 变更: min→max（更紧），ATR mult 2.0→2.2，pullback_low 替代 low_20d，
               新增 12% hard reject。

    Args:
        ohlcv: OHLCV 列表，用于计算 pullback_low（近5日最低价）

    Returns:
        {entry_price, stop_price, valid_until, rejected, reason_codes}
    """
    tech_config = config.get("technical_engine", {})
    pullback = tech_config.get("pullback_setup", {})
    stop_config = tech_config.get("stop", {})

    close = indicators["close"]
    atr = indicators["atr14"]

    # Entry
    offset_pct = pullback.get("entry", {}).get("price_offset_pct", -0.002)
    entry_price = round(close * (1 + offset_pct), 2)

    # v1.2: pullback_low = 近5日最低价（touch window 内的最低点）
    touch_window = pullback.get("touch_window_days", 5)
    recent_bars = ohlcv[-touch_window:] if len(ohlcv) >= touch_window else ohlcv
    pullback_low = min(float(b["low"]) for b in recent_bars)

    # v1.2: Stop = max(struct, atr) → 取更紧止损
    rule_a_mult = stop_config.get("rule_a", {}).get("mult", 2.2)   # v1.2: 2.2
    rule_b_mult = stop_config.get("rule_b", {}).get("mult", 0.5)

    stop_atr = entry_price - rule_a_mult * atr
    stop_struct = pullback_low - rule_b_mult * atr
    stop_price = round(max(stop_atr, stop_struct), 2)  # v1.2: max → 更紧

    # v1.2: 12% hard reject
    max_stop_distance_pct = stop_config.get("max_stop_distance_pct", 0.12)
    if entry_price > 0 and (entry_price - stop_price) / entry_price > max_stop_distance_pct:
        return {
            "entry_price": entry_price,
            "stop_price": stop_price,
            "valid_until": None,
            "rejected": True,
            "reason_codes": ["RC_T403_STOP_TOO_WIDE"],
        }

    # Valid until
    valid_days = pullback.get("valid_until_days", 2)
    valid_until = (date.today() + timedelta(days=valid_days)).isoformat()

    return {
        "entry_price": entry_price,
        "stop_price": stop_price,
        "valid_until": valid_until,
        "rejected": False,
        "reason_codes": ["RC_T301_ENTRY_LIMIT_SET"],
    }
