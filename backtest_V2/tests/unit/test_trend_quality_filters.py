from __future__ import annotations

import numpy as np

from backtest_v2.engine_vector.signal_matrix import build_signal_matrices


def test_trend_slope_filter_mask() -> None:
    # T=6, N=1
    close = np.array([[10.0], [10.0], [10.0], [10.0], [10.0], [10.0]])
    fast_ma = np.array([[9.0], [9.0], [9.0], [9.0], [9.0], [9.0]])
    slow_ma = np.array([[8.0], [8.0], [8.0], [8.0], [8.0], [8.0]])
    trend_fast = np.array([[1.0], [2.0], [3.0], [2.0], [1.0], [0.0]])
    trend_slow = np.array([[0.0], [0.0], [0.0], [0.0], [0.0], [0.0]])
    tradable = np.ones((6, 1), dtype=bool)
    risk_on = np.ones(6, dtype=bool)

    sig = build_signal_matrices(
        close=close,
        fast_ma=fast_ma,
        slow_ma=slow_ma,
        trend_fast_ma=trend_fast,
        trend_slow_ma=trend_slow,
        tradable=tradable,
        entry_buffer=0.0,
        enable_trend_filter=True,
        enable_trend_tier=False,
        enable_trend_slope_filter=True,
        slope_lookback=2,
        enable_trend_continuity_filter=False,
        continuity_n=10,
        risk_on=risk_on,
    )
    slope_pass = sig["slope_pass"][:, 0].tolist()
    # First two rows insufficient history -> False
    assert slope_pass[0] is False
    assert slope_pass[1] is False
    # t=2 positive slope; t=3 slope==0 fail; t=4,5 negative slope fail
    assert slope_pass[2] is True
    assert slope_pass[3] is False
    assert slope_pass[4] is False
    assert slope_pass[5] is False


def test_trend_continuity_filter_mask() -> None:
    close = np.array([[10.0], [10.0], [10.0], [10.0], [10.0], [10.0]])
    fast_ma = np.array([[9.0], [9.0], [9.0], [9.0], [9.0], [9.0]])
    slow_ma = np.array([[8.0], [8.0], [8.0], [8.0], [8.0], [8.0]])
    # base_trend_on = fast>slow => [T,T,T,F,T,T]
    trend_fast = np.array([[2.0], [2.0], [2.0], [0.0], [2.0], [2.0]])
    trend_slow = np.array([[1.0], [1.0], [1.0], [0.0], [1.0], [1.0]])
    tradable = np.ones((6, 1), dtype=bool)
    risk_on = np.ones(6, dtype=bool)

    sig = build_signal_matrices(
        close=close,
        fast_ma=fast_ma,
        slow_ma=slow_ma,
        trend_fast_ma=trend_fast,
        trend_slow_ma=trend_slow,
        tradable=tradable,
        entry_buffer=0.0,
        enable_trend_filter=True,
        enable_trend_tier=False,
        enable_trend_slope_filter=False,
        slope_lookback=5,
        enable_trend_continuity_filter=True,
        continuity_n=3,
        risk_on=risk_on,
    )
    continuity = sig["continuity_pass"][:, 0].tolist()
    # First two rows insufficient; row2 passes (T,T,T), row3 fails due break, row4/5 also fail.
    assert continuity == [False, False, True, False, False, False]
