from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from backtest_v2.core.models import Position, Signal
from backtest_v2.core.types import Side
from .base import Strategy
from .signals_core import (
    buy_raw_ok_scalar,
    compute_atr_stop_ok,
    compute_atr_trail_ok,
    compute_cross_down,
    compute_cross_up,
    compute_entry_buffer_ok,
    compute_trend_ok,
)


def compute_tr_atr_sma(sym_df: pd.DataFrame, atr_n: int) -> tuple[pd.Series, pd.Series]:
    high = sym_df["high"].astype(float)
    low = sym_df["low"].astype(float)
    close = sym_df["close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(int(atr_n)).mean()
    return tr, atr


@dataclass(slots=True)
class SMACrossRegimeATRStrategy(Strategy):
    fast: int = 5
    slow: int = 20
    order_size: int = 1000
    regime_ma: int = 120
    benchmark_symbol: str = "000300.SH"
    entry_buffer: float = 0.01
    buffer_mode: str = "slow_ma"
    enable_trend_filter: bool = False
    enable_trend_tier: bool = True
    enable_trend_accel: bool = True
    enable_trend_break_exit: bool = True
    trend_break_confirm_days: int = 3
    trend_break_confirm_gap_pct: float = 0.0
    trend_break_use_benchmark_regime: bool = False
    trend_tier_mode: str = "MA120_GT_MA200"
    weak_trend_order_scale: float = 0.5
    strong_trend_order_scale: float = 1.0
    trend_fast_ma: int = 120
    trend_slow_ma: int = 200
    enable_trend_slope_filter: bool = False
    slope_lookback: int = 5
    enable_trend_continuity_filter: bool = False
    continuity_n: int = 10
    atr_n: int = 14
    atr_stop_k: float = 2.0
    atr_trail_k: float = 3.0
    enable_atr_stop: bool = False
    enable_atr_trail: bool = False
    debug_atr: bool = False

    def __post_init__(self) -> None:
        if self.entry_buffer < 0.0 or self.entry_buffer > 0.1:
            raise ValueError(f"entry_buffer out of range [0,0.1]: {self.entry_buffer}")
        if self.atr_n < 2:
            raise ValueError(f"atr_n must be >=2, got {self.atr_n}")
        if self.weak_trend_order_scale < 0 or self.strong_trend_order_scale <= 0:
            raise ValueError("trend tier order scales must be non-negative")
        if int(self.trend_break_confirm_days) < 1:
            raise ValueError("trend_break_confirm_days must be >= 1")
        if float(self.trend_break_confirm_gap_pct) < 0:
            raise ValueError("trend_break_confirm_gap_pct must be >= 0")
        Strategy.__init__(self)
        self.state.setdefault("entry_price", {})
        self.state.setdefault("entry_price_estimated", {})
        self.state.setdefault("peak_close", {})
        self.state.setdefault("entry_atr", {})
        self.state.setdefault("last_update_day", {})
        self.state.setdefault("break_streak", {})
        self.state.setdefault("debug_atr_rows", [])

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

        entry_price_state: dict[str, float] = self.state["entry_price"]
        entry_price_estimated_state: dict[str, bool] = self.state["entry_price_estimated"]
        peak_close_state: dict[str, float] = self.state["peak_close"]
        entry_atr_state: dict[str, float | None] = self.state["entry_atr"]
        last_update_day_state: dict[str, str] = self.state["last_update_day"]
        break_streak_state: dict[str, int] = self.state["break_streak"]

        non_bench = day_data[~day_data["symbol"].astype(str).str.lower().str.startswith("bench:")]
        for symbol, sym_df in non_bench.groupby("symbol", sort=False):
            sym_df = sym_df.sort_values("date")
            if len(sym_df) < self.slow + 1:
                continue
            if not {"high", "low", "close"}.issubset(set(sym_df.columns)):
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

            _, atr_ser = compute_tr_atr_sma(sym_df, self.atr_n)
            atr_last = float(atr_ser.iloc[-1]) if pd.notna(atr_ser.iloc[-1]) else None
            atr_available = atr_last is not None and len(sym_df) >= (self.atr_n + 1)
            prev_close = float(close.iloc[-2]) if len(close) >= 2 else None

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

            if has_pos and symbol not in entry_price_state:
                entry_price_state[symbol] = float(max(positions[symbol].avg_cost, 0.0))
                entry_price_estimated_state[symbol] = False
            if has_pos:
                peak_close_state[symbol] = float(max(peak_close_state.get(symbol, close_last), close_last))
                if symbol not in entry_atr_state:
                    entry_atr_state[symbol] = atr_last if atr_available else None
            else:
                entry_price_state.pop(symbol, None)
                entry_price_estimated_state.pop(symbol, None)
                peak_close_state.pop(symbol, None)
                entry_atr_state.pop(symbol, None)
                break_streak_state.pop(symbol, None)

            entry_price = float(entry_price_state.get(symbol, 0.0)) if has_pos else 0.0
            peak_close = float(peak_close_state.get(symbol, close_last))
            stop_price = (entry_price - self.atr_stop_k * atr_last) if (has_pos and atr_available) else None
            trail_price = (peak_close - self.atr_trail_k * atr_last) if (has_pos and atr_available) else None

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
                "trend_break_confirm_days": int(self.trend_break_confirm_days),
                "trend_break_confirm_gap_pct": float(self.trend_break_confirm_gap_pct),
                "trend_break_use_benchmark_regime": bool(self.trend_break_use_benchmark_regime),
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
                "close": round(close_last, 6),
                "slow_ma_threshold": round(slow_ma_threshold, 6),
                "cross_up": bool(cross_up),
                "ma_valid": not any(pd.isna(v) for v in (fast_now, slow_now, fast_prev, slow_prev)),
                "atr_n": int(self.atr_n),
                "atr_last": round(float(atr_last), 6) if atr_last is not None else None,
                "entry_price": round(float(entry_price), 6) if has_pos else None,
                "entry_price_estimated": bool(entry_price_estimated_state.get(symbol, False)) if has_pos else None,
                "peak_close": round(float(peak_close), 6) if has_pos else None,
                "stop_price": round(float(stop_price), 6) if stop_price is not None else None,
                "trail_price": round(float(trail_price), 6) if trail_price is not None else None,
                "enable_atr_stop": bool(self.enable_atr_stop),
                "enable_atr_trail": bool(self.enable_atr_trail),
                "trend_tier_at_exit": trend_tier,
                "strong_trend_at_exit": bool(strong_trend),
                "break_streak": int(break_streak_state.get(symbol, 0)),
            }

            if has_pos:
                qty = int(positions[symbol].sellable_qty)
                if (not regime_on) and qty > 0:
                    meta["triggered_rule"] = "RISK_OFF_EXIT"
                    out.append(
                        Signal(
                            signal_date=day,
                            symbol=symbol,
                            side=Side.SELL,
                            qty=qty,
                            reason="RISK_OFF_EXIT",
                            reason_code="RISK_OFF_EXIT",
                            meta=meta,
                        )
                    )
                    last_update_day_state[symbol] = day.isoformat()
                    self._maybe_debug_row(
                        day=day,
                        symbol=symbol,
                        position_qty=qty,
                        close_last=close_last,
                        high_last=float(sym_df["high"].astype(float).iloc[-1]),
                        low_last=float(sym_df["low"].astype(float).iloc[-1]),
                        prev_close=prev_close,
                        atr_last=atr_last,
                        entry_price=entry_price,
                        entry_price_estimated=bool(entry_price_estimated_state.get(symbol, False)),
                        peak_close=peak_close,
                        stop_price=stop_price,
                        trail_price=trail_price,
                        risk_on=bool(regime_on),
                        triggered_rule="RISK_OFF_EXIT",
                        reason_code="RISK_OFF_EXIT",
                    )
                    continue

                # Confirmed trend break: weak trend streak with optional gap filter.
                break_t = not strong_trend
                if break_t:
                    break_streak_state[symbol] = int(break_streak_state.get(symbol, 0)) + 1
                else:
                    break_streak_state[symbol] = 0
                break_streak = int(break_streak_state.get(symbol, 0))
                gap_ok = True
                if float(self.trend_break_confirm_gap_pct) > 0:
                    if trend_slow_value is None or trend_slow_value == 0 or trend_fast_value is None:
                        gap_ok = False
                    else:
                        gap_ok = ((trend_slow_value - trend_fast_value) / trend_slow_value) >= float(self.trend_break_confirm_gap_pct)
                regime_gate_ok = True
                if bool(self.trend_break_use_benchmark_regime):
                    regime_gate_ok = bool(regime_on)
                confirmed_break = (
                    bool(self.enable_trend_break_exit)
                    and break_t
                    and break_streak >= int(self.trend_break_confirm_days)
                    and gap_ok
                    and regime_gate_ok
                )
                meta["break_streak"] = int(break_streak)
                meta["trend_break_gap_ok"] = bool(gap_ok)
                meta["trend_break_regime_gate_ok"] = bool(regime_gate_ok)
                if confirmed_break and qty > 0:
                    meta["triggered_rule"] = "TREND_BREAK_EXIT"
                    meta["trend_tier_at_exit"] = trend_tier
                    meta["strong_trend_at_exit"] = bool(strong_trend)
                    meta["triggered_rule"] = "CONFIRMED_TREND_BREAK_EXIT"
                    out.append(
                        Signal(
                            signal_date=day,
                            symbol=symbol,
                            side=Side.SELL,
                            qty=qty,
                            reason="TREND_BREAK_EXIT",
                            reason_code="TREND_BREAK_EXIT",
                            meta=meta,
                        )
                    )
                    last_update_day_state[symbol] = day.isoformat()
                    self._maybe_debug_row(
                        day=day,
                        symbol=symbol,
                        position_qty=qty,
                        close_last=close_last,
                        high_last=float(sym_df["high"].astype(float).iloc[-1]),
                        low_last=float(sym_df["low"].astype(float).iloc[-1]),
                        prev_close=prev_close,
                        atr_last=atr_last,
                        entry_price=entry_price,
                        entry_price_estimated=bool(entry_price_estimated_state.get(symbol, False)),
                        peak_close=peak_close,
                        stop_price=stop_price,
                        trail_price=trail_price,
                        risk_on=bool(regime_on),
                        triggered_rule="CONFIRMED_TREND_BREAK_EXIT",
                        reason_code="TREND_BREAK_EXIT",
                    )
                    continue

                if qty > 0 and stop_price is not None and compute_atr_stop_ok(
                    close_last, entry_price, atr_last if atr_available else None, float(self.atr_stop_k), bool(self.enable_atr_stop)
                ):
                    meta["triggered_rule"] = "ATR_STOP_LOSS"
                    out.append(
                        Signal(
                            signal_date=day,
                            symbol=symbol,
                            side=Side.SELL,
                            qty=qty,
                            reason="ATR_STOP_LOSS",
                            reason_code="ATR_STOP_LOSS",
                            meta=meta,
                        )
                    )
                    last_update_day_state[symbol] = day.isoformat()
                    self._maybe_debug_row(
                        day=day,
                        symbol=symbol,
                        position_qty=qty,
                        close_last=close_last,
                        high_last=float(sym_df["high"].astype(float).iloc[-1]),
                        low_last=float(sym_df["low"].astype(float).iloc[-1]),
                        prev_close=prev_close,
                        atr_last=atr_last,
                        entry_price=entry_price,
                        entry_price_estimated=bool(entry_price_estimated_state.get(symbol, False)),
                        peak_close=peak_close,
                        stop_price=stop_price,
                        trail_price=trail_price,
                        risk_on=bool(regime_on),
                        triggered_rule="ATR_STOP_LOSS",
                        reason_code="ATR_STOP_LOSS",
                    )
                    continue

                if qty > 0 and trail_price is not None and compute_atr_trail_ok(
                    close_last, peak_close, atr_last if atr_available else None, float(self.atr_trail_k), bool(self.enable_atr_trail)
                ):
                    meta["triggered_rule"] = "ATR_TRAIL_EXIT"
                    out.append(
                        Signal(
                            signal_date=day,
                            symbol=symbol,
                            side=Side.SELL,
                            qty=qty,
                            reason="ATR_TRAIL_EXIT",
                            reason_code="ATR_TRAIL_EXIT",
                            meta=meta,
                        )
                    )
                    last_update_day_state[symbol] = day.isoformat()
                    self._maybe_debug_row(
                        day=day,
                        symbol=symbol,
                        position_qty=qty,
                        close_last=close_last,
                        high_last=float(sym_df["high"].astype(float).iloc[-1]),
                        low_last=float(sym_df["low"].astype(float).iloc[-1]),
                        prev_close=prev_close,
                        atr_last=atr_last,
                        entry_price=entry_price,
                        entry_price_estimated=bool(entry_price_estimated_state.get(symbol, False)),
                        peak_close=peak_close,
                        stop_price=stop_price,
                        trail_price=trail_price,
                        risk_on=bool(regime_on),
                        triggered_rule="ATR_TRAIL_EXIT",
                        reason_code="ATR_TRAIL_EXIT",
                    )
                    continue

                if cross_down and qty > 0:
                    meta["triggered_rule"] = "SMA_CROSS_DOWN"
                    out.append(
                        Signal(
                            signal_date=day,
                            symbol=symbol,
                            side=Side.SELL,
                            qty=qty,
                            reason="SMA_CROSS_DOWN",
                            reason_code="SMA_CROSS_DOWN",
                            meta=meta,
                        )
                    )
                    last_update_day_state[symbol] = day.isoformat()
                    self._maybe_debug_row(
                        day=day,
                        symbol=symbol,
                        position_qty=qty,
                        close_last=close_last,
                        high_last=float(sym_df["high"].astype(float).iloc[-1]),
                        low_last=float(sym_df["low"].astype(float).iloc[-1]),
                        prev_close=prev_close,
                        atr_last=atr_last,
                        entry_price=entry_price,
                        entry_price_estimated=bool(entry_price_estimated_state.get(symbol, False)),
                        peak_close=peak_close,
                        stop_price=stop_price,
                        trail_price=trail_price,
                        risk_on=bool(regime_on),
                        triggered_rule="SMA_CROSS_DOWN",
                        reason_code="SMA_CROSS_DOWN",
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
                    meta["triggered_rule"] = "SMA_CROSS_UP"
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
                    meta["triggered_rule"] = "SMA_CROSS_UP_BLOCKED"
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
                    self._maybe_debug_row(
                        day=day,
                        symbol=symbol,
                        position_qty=0,
                        close_last=close_last,
                        high_last=float(sym_df["high"].astype(float).iloc[-1]),
                        low_last=float(sym_df["low"].astype(float).iloc[-1]),
                        prev_close=prev_close,
                        atr_last=atr_last,
                        entry_price=None,
                        entry_price_estimated=None,
                        peak_close=None,
                        stop_price=None,
                        trail_price=None,
                        risk_on=bool(regime_on),
                        triggered_rule="SMA_CROSS_UP_BLOCKED",
                        reason_code="SMA_CROSS_UP_BLOCKED",
                    )
                elif (not bool(self.enable_trend_tier)) and bool(self.enable_trend_filter) and (not trend_ok):
                    meta["triggered_rule"] = "BUY_BLOCKED_TREND_FILTER"
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
                    meta["triggered_rule"] = "ENTRY_BUFFER_BLOCKED"
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
                    self._maybe_debug_row(
                        day=day,
                        symbol=symbol,
                        position_qty=0,
                        close_last=close_last,
                        high_last=float(sym_df["high"].astype(float).iloc[-1]),
                        low_last=float(sym_df["low"].astype(float).iloc[-1]),
                        prev_close=prev_close,
                        atr_last=atr_last,
                        entry_price=None,
                        entry_price_estimated=None,
                        peak_close=None,
                        stop_price=None,
                        trail_price=None,
                        risk_on=bool(regime_on),
                        triggered_rule="ENTRY_BUFFER_BLOCKED",
                        reason_code="ENTRY_BUFFER_BLOCKED",
                    )
            elif has_pos:
                self._maybe_debug_row(
                    day=day,
                    symbol=symbol,
                    position_qty=int(positions[symbol].total_qty),
                    close_last=close_last,
                    high_last=float(sym_df["high"].astype(float).iloc[-1]),
                    low_last=float(sym_df["low"].astype(float).iloc[-1]),
                    prev_close=prev_close,
                    atr_last=atr_last,
                    entry_price=entry_price,
                    entry_price_estimated=bool(entry_price_estimated_state.get(symbol, False)),
                    peak_close=peak_close,
                    stop_price=stop_price,
                    trail_price=trail_price,
                    risk_on=bool(regime_on),
                    triggered_rule="NONE",
                    reason_code="",
                )

        return out

    def _maybe_debug_row(
        self,
        day: date,
        symbol: str,
        position_qty: int,
        close_last: float,
        high_last: float,
        low_last: float,
        prev_close: float | None,
        atr_last: float | None,
        entry_price: float | None,
        entry_price_estimated: bool | None,
        peak_close: float | None,
        stop_price: float | None,
        trail_price: float | None,
        risk_on: bool,
        triggered_rule: str,
        reason_code: str,
    ) -> None:
        if not bool(self.debug_atr):
            return
        if position_qty <= 0 and triggered_rule == "NONE":
            return
        self.state.setdefault("debug_atr_rows", []).append(
            {
                "date": day.isoformat(),
                "symbol": symbol,
                "position_qty": int(position_qty),
                "close": float(close_last),
                "high": float(high_last),
                "low": float(low_last),
                "prev_close": None if prev_close is None else float(prev_close),
                "atr_n": int(self.atr_n),
                "atr_last": None if atr_last is None else float(atr_last),
                "entry_price": None if entry_price is None else float(entry_price),
                "entry_price_estimated": entry_price_estimated,
                "peak_close": None if peak_close is None else float(peak_close),
                "stop_k": float(self.atr_stop_k),
                "stop_price": None if stop_price is None else float(stop_price),
                "trail_k": float(self.atr_trail_k),
                "trail_price": None if trail_price is None else float(trail_price),
                "risk_on": bool(risk_on),
                "triggered_rule": triggered_rule,
                "reason_code": reason_code,
            }
        )
