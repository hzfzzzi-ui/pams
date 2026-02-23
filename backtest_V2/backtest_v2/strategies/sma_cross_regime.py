from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from backtest_v2.core.models import Position, Signal
from backtest_v2.core.types import Side
from .base import Strategy
from .signals_core import buy_raw_ok_scalar, compute_cross_down, compute_cross_up, compute_entry_buffer_ok, compute_trend_ok


@dataclass(slots=True)
class SMACrossRegimeStrategy(Strategy):
    fast: int = 5
    slow: int = 20
    order_size: int = 1000
    regime_ma: int = 200
    benchmark_symbol: str = "000300.SH"
    entry_buffer: float = 0.0
    buffer_mode: str = "slow_ma"
    enable_trend_filter: bool = False
    enable_trend_tier: bool = True
    enable_trend_accel: bool = True
    enable_trend_break_exit: bool = True
    trend_tier_mode: str = "MA120_GT_MA200"
    weak_trend_order_scale: float = 0.5
    strong_trend_order_scale: float = 1.0
    trend_fast_ma: int = 120
    trend_slow_ma: int = 200
    enable_trend_slope_filter: bool = False
    slope_lookback: int = 5
    enable_trend_continuity_filter: bool = False
    continuity_n: int = 10

    def __post_init__(self) -> None:
        if self.entry_buffer < 0.0 or self.entry_buffer > 0.1:
            raise ValueError(f"entry_buffer out of range [0,0.1]: {self.entry_buffer}")
        if self.weak_trend_order_scale < 0 or self.strong_trend_order_scale <= 0:
            raise ValueError("trend tier order scales must be non-negative")
        Strategy.__init__(self)

    def generate_signals(self, day: date, history: pd.DataFrame, positions: dict[str, Position]) -> list[Signal]:
        out: list[Signal] = []
        day_ts = pd.Timestamp(day)
        day_data = history[history["date"] <= day_ts]
        if day_data.empty:
            return out

        bench_code = f"BENCH:{self.benchmark_symbol}".lower()
        bench_df = day_data[day_data["symbol"].astype(str).str.lower() == bench_code].sort_values("date")
        regime_on = True
        benchmark_close = None
        benchmark_ma = None
        if len(bench_df) >= self.regime_ma:
            bclose = bench_df["close"].astype(float)
            benchmark_close = float(bclose.iloc[-1])
            benchmark_ma = float(bclose.rolling(self.regime_ma).mean().iloc[-1])
            if pd.notna(benchmark_ma):
                regime_on = benchmark_close > benchmark_ma

        non_bench = day_data[~day_data["symbol"].astype(str).str.lower().str.startswith("bench:")]
        for symbol, sym_df in non_bench.groupby("symbol", sort=False):
            sym_df = sym_df.sort_values("date")
            if len(sym_df) < self.slow + 1:
                continue
            close = sym_df["close"].astype(float)
            fast_ser = close.rolling(self.fast).mean()
            slow_ser = close.rolling(self.slow).mean()
            trend_fast_ser = close.rolling(self.trend_fast_ma).mean()
            trend_slow_ser = close.rolling(self.trend_slow_ma).mean()
            fast_now = float(fast_ser.iloc[-1])
            slow_now = float(slow_ser.iloc[-1])
            fast_prev = float(fast_ser.iloc[-2])
            slow_prev = float(slow_ser.iloc[-2])
            if any(pd.isna(v) for v in (fast_now, slow_now, fast_prev, slow_prev)):
                continue

            cross_up = compute_cross_up(fast_now, slow_now, fast_prev, slow_prev)
            cross_down = compute_cross_down(fast_now, slow_now, fast_prev, slow_prev)
            has_pos = symbol in positions and positions[symbol].total_qty > 0
            close_last = float(close.iloc[-1])
            trend_fast_value = (
                float(trend_fast_ser.iloc[-1])
                if len(trend_fast_ser) > 0 and pd.notna(trend_fast_ser.iloc[-1])
                else None
            )
            trend_slow_value = (
                float(trend_slow_ser.iloc[-1])
                if len(trend_slow_ser) > 0 and pd.notna(trend_slow_ser.iloc[-1])
                else None
            )
            trend_ok = compute_trend_ok(close_last, trend_fast_value, trend_slow_value, bool(self.enable_trend_filter))
            strong_trend = bool(
                trend_fast_value is not None
                and trend_slow_value is not None
                and trend_fast_value > trend_slow_value
            )
            if bool(self.enable_trend_accel):
                order_value_scale = float(self.strong_trend_order_scale if strong_trend else self.weak_trend_order_scale)
            else:
                order_value_scale = 1.0
            trend_tier = "STRONG" if strong_trend else "WEAK"
            base_order_value = float(self.state.get("base_order_value", 10_000.0))
            slow_ma_threshold = float(slow_now * (1.0 + self.entry_buffer))
            buffer_ok = compute_entry_buffer_ok(close_last, slow_now, float(self.entry_buffer))
            meta = {
                "fast_ma": round(fast_now, 6),
                "slow_ma": round(slow_now, 6),
                "fast_ma_prev": round(fast_prev, 6),
                "slow_ma_prev": round(slow_prev, 6),
                "regime_ma": self.regime_ma,
                "regime_on": bool(regime_on),
                "benchmark_close": benchmark_close,
                "benchmark_ma": benchmark_ma,
                "entry_buffer": float(self.entry_buffer),
                "buffer_mode": str(self.buffer_mode),
                "buffer_ok": bool(buffer_ok),
                "enable_trend_filter": bool(self.enable_trend_filter),
                "enable_trend_tier": bool(self.enable_trend_tier),
                "enable_trend_accel": bool(self.enable_trend_accel),
                "enable_trend_break_exit": bool(self.enable_trend_break_exit),
                "trend_tier_mode": str(self.trend_tier_mode),
                "trend_fast_ma": int(self.trend_fast_ma),
                "trend_slow_ma": int(self.trend_slow_ma),
                "trend_fast_value": round(trend_fast_value, 6) if trend_fast_value is not None else None,
                "trend_slow_value": round(trend_slow_value, 6) if trend_slow_value is not None else None,
                "ma120": round(trend_fast_value, 6) if trend_fast_value is not None else None,
                "ma200": round(trend_slow_value, 6) if trend_slow_value is not None else None,
                "trend_ok": bool(trend_ok),
                "strong_trend": bool(strong_trend),
                "trend_tier": trend_tier,
                "order_value_scale": float(order_value_scale),
                "order_value_raw": float(base_order_value),
                "order_value_final": float(base_order_value * order_value_scale),
                "trend_tier_at_exit": trend_tier,
                "strong_trend_at_exit": bool(strong_trend),
                "close": round(close_last, 6),
                "slow_ma_threshold": round(slow_ma_threshold, 6),
                "cross_up": bool(cross_up),
                "ma_valid": not any(pd.isna(v) for v in (fast_now, slow_now, fast_prev, slow_prev)),
            }

            if (not regime_on) and has_pos:
                qty = int(positions[symbol].sellable_qty)
                if qty > 0:
                    out.append(
                        Signal(
                            signal_date=day,
                            symbol=symbol,
                            side=Side.SELL,
                            qty=qty,
                            reason="RISK_OFF_EXIT",
                            reason_code="RISK_OFF_EXIT",
                            meta={**meta, "triggered_rule": "RISK_OFF_EXIT", "trend_tier_at_exit": trend_tier, "strong_trend_at_exit": bool(strong_trend)},
                        )
                    )
                continue

            if bool(self.enable_trend_break_exit) and has_pos and (not strong_trend):
                qty = int(positions[symbol].sellable_qty)
                if qty > 0:
                    out.append(
                        Signal(
                            signal_date=day,
                            symbol=symbol,
                            side=Side.SELL,
                            qty=qty,
                            reason="TREND_BREAK_EXIT",
                            reason_code="TREND_BREAK_EXIT",
                            meta={**meta, "triggered_rule": "TREND_BREAK_EXIT", "trend_tier_at_exit": trend_tier, "strong_trend_at_exit": bool(strong_trend)},
                        )
                    )
                continue

            if cross_down and has_pos:
                qty = int(positions[symbol].sellable_qty)
                if qty > 0:
                    out.append(
                        Signal(
                            signal_date=day,
                            symbol=symbol,
                            side=Side.SELL,
                            qty=qty,
                            reason="SMA_CROSS_DOWN",
                            reason_code="SMA_CROSS_DOWN",
                            meta={**meta, "triggered_rule": "SMA_CROSS_DOWN", "trend_tier_at_exit": trend_tier, "strong_trend_at_exit": bool(strong_trend)},
                        )
                    )
                continue

            if cross_up and (not has_pos):
                buy_ok = buy_raw_ok_scalar(
                    cross_up=bool(cross_up),
                    close=float(close_last),
                    slow_ma=float(slow_now),
                    trend_fast=trend_fast_value,
                    trend_slow=trend_slow_value,
                    entry_buffer=float(self.entry_buffer),
                    enable_trend_filter=bool(self.enable_trend_filter) and (not bool(self.enable_trend_tier)),
                    regime_ok=bool(regime_on),
                    ma_is_valid=not any(pd.isna(v) for v in (fast_now, slow_now, fast_prev, slow_prev)),
                    tradable=True,
                    apply_tradable_in_raw=False,
                )
                if buy_ok:
                    out.append(
                        Signal(
                            signal_date=day,
                            symbol=symbol,
                            side=Side.BUY,
                            qty=self.order_size,
                            reason="SMA_CROSS_UP",
                            reason_code="SMA_CROSS_UP",
                            meta=meta,
                        )
                    )
                elif not regime_on:
                    out.append(
                        Signal(
                            signal_date=day,
                            symbol=symbol,
                            side=Side.BUY,
                            qty=self.order_size,
                            action="BLOCKED",
                            reason="SMA_CROSS_UP_BLOCKED",
                            reason_code="SMA_CROSS_UP_BLOCKED",
                            meta=meta,
                        )
                    )
                elif (not bool(self.enable_trend_tier)) and bool(self.enable_trend_filter) and (not trend_ok):
                    out.append(
                        Signal(
                            signal_date=day,
                            symbol=symbol,
                            side=Side.BUY,
                            qty=self.order_size,
                            action="BLOCKED",
                            reason="BUY_BLOCKED_TREND_FILTER",
                            reason_code="BUY_BLOCKED_TREND_FILTER",
                            meta=meta,
                        )
                    )
                else:
                    out.append(
                        Signal(
                            signal_date=day,
                            symbol=symbol,
                            side=Side.BUY,
                            qty=self.order_size,
                            action="BLOCKED",
                            reason="ENTRY_BUFFER_BLOCKED",
                            reason_code="ENTRY_BUFFER_BLOCKED",
                            meta=meta,
                        )
                    )
        return out
