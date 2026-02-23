from __future__ import annotations

import numpy as np
import pandas as pd

from backtest_v2.strategies.signals_core import buy_raw_ok_vectorized, compute_cross_down_vec, compute_cross_up_vec


def build_signal_matrices(
    *,
    close: np.ndarray,
    fast_ma: np.ndarray,
    slow_ma: np.ndarray,
    trend_fast_ma: np.ndarray,
    trend_slow_ma: np.ndarray,
    tradable: np.ndarray,
    entry_buffer: float,
    enable_trend_filter: bool,
    enable_trend_tier: bool = False,
    enable_trend_slope_filter: bool = False,
    slope_lookback: int = 5,
    enable_trend_continuity_filter: bool = False,
    continuity_n: int = 10,
    risk_on: np.ndarray,
) -> dict[str, np.ndarray]:
    t, n = close.shape
    cross_up = np.zeros((t, n), dtype=bool)
    cross_down = np.zeros((t, n), dtype=bool)
    trend_ok = np.ones((t, n), dtype=bool)
    buffer_ok = np.zeros((t, n), dtype=bool)

    if t > 1:
        fast_now = fast_ma[1:, :]
        slow_now = slow_ma[1:, :]
        fast_prev = fast_ma[:-1, :]
        slow_prev = slow_ma[:-1, :]
        valid = (
            ~np.isnan(fast_now)
            & ~np.isnan(slow_now)
            & ~np.isnan(fast_prev)
            & ~np.isnan(slow_prev)
            & ~np.isnan(close[1:, :])
        )
        cross_up[1:, :] = compute_cross_up_vec(fast_now, slow_now, fast_prev, slow_prev) & valid
        cross_down[1:, :] = compute_cross_down_vec(fast_now, slow_now, fast_prev, slow_prev) & valid
        buffer_ok[1:, :] = (close[1:, :] > (slow_now * (1.0 + float(entry_buffer)))) & valid

    base_trend_on = (
        ~np.isnan(trend_fast_ma)
        & ~np.isnan(trend_slow_ma)
        & (trend_fast_ma > trend_slow_ma)
    )
    legacy_trend_ok = (
        ~np.isnan(trend_fast_ma)
        & ~np.isnan(trend_slow_ma)
        & ~np.isnan(close)
        & (close > trend_fast_ma)
        & (trend_fast_ma > trend_slow_ma)
    )
    slope_pass = np.ones((t, n), dtype=bool)
    if bool(enable_trend_slope_filter):
        lb = max(1, int(slope_lookback))
        slope_pass[:] = False
        if t > lb:
            slope_fast = np.full((t, n), np.nan, dtype=float)
            slope_slow = np.full((t, n), np.nan, dtype=float)
            slope_fast[lb:, :] = trend_fast_ma[lb:, :] - trend_fast_ma[:-lb, :]
            slope_slow[lb:, :] = trend_slow_ma[lb:, :] - trend_slow_ma[:-lb, :]
            slope_pass = (
                ~np.isnan(slope_fast)
                & ~np.isnan(slope_slow)
                & (slope_fast > 0.0)
                & (slope_slow >= 0.0)
            )
    continuity_pass = np.ones((t, n), dtype=bool)
    if bool(enable_trend_continuity_filter):
        cn = max(1, int(continuity_n))
        continuity_pass[:] = False
        # vectorized via pandas rolling over full matrix (column-wise C-optimized path).
        base_df = pd.DataFrame(base_trend_on.astype(float))
        cont_df = (base_df.rolling(cn, min_periods=cn).sum() == float(cn))
        continuity_pass = cont_df.to_numpy(dtype=bool)
    quality_trend_on = base_trend_on.copy()
    if bool(enable_trend_slope_filter):
        quality_trend_on &= slope_pass
    if bool(enable_trend_continuity_filter):
        quality_trend_on &= continuity_pass
    if enable_trend_filter:
        trend_ok = quality_trend_on if (bool(enable_trend_slope_filter) or bool(enable_trend_continuity_filter)) else legacy_trend_ok

    risk_on_matrix = np.repeat(risk_on.reshape(-1, 1), n, axis=1)
    ma_is_valid = (
        ~np.isnan(fast_ma)
        & ~np.isnan(slow_ma)
        & ~np.isnan(close)
    )
    buy_base = buy_raw_ok_vectorized(
        cross_up=cross_up,
        close=close,
        slow_ma=slow_ma,
        trend_fast=trend_fast_ma,
        trend_slow=trend_slow_ma,
        entry_buffer=float(entry_buffer),
        enable_trend_filter=bool(enable_trend_filter)
        and (
            (not bool(enable_trend_tier))
            or bool(enable_trend_slope_filter)
            or bool(enable_trend_continuity_filter)
        ),
        regime_ok=risk_on_matrix,
        ma_is_valid=ma_is_valid,
        tradable=tradable,
        apply_tradable_in_raw=False,
    )
    return {
        "cross_up": cross_up,
        "cross_down": cross_down,
        "trend_ok": trend_ok,
        "base_trend_on": base_trend_on,
        "slope_pass": slope_pass,
        "continuity_pass": continuity_pass,
        "quality_trend_on": quality_trend_on,
        "buffer_ok": buffer_ok,
        "ma_is_valid": ma_is_valid,
        "buy_base": buy_base,
        "risk_on_matrix": risk_on_matrix,
    }
