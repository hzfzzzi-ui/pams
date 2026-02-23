from __future__ import annotations

from backtest_v2.config.models import StrategyConfig
from .base import Strategy
from .ma_atr_stop import MAAtrStopStrategy
from .ma_trend_filter import MATrendFilterStrategy
from .sma_cross import SMACrossStrategy
from .sma_cross_regime import SMACrossRegimeStrategy
from .sma_cross_regime_atr import SMACrossRegimeATRStrategy
from .multifactor_signal import MultifactorSignalStrategy
from .volume_breakout import VolumeBreakoutStrategy
from .momentum_roc import MomentumROCStrategy
from .relative_strength import RelativeStrengthStrategy
from .bollinger_breakout import BollingerBreakoutStrategy
from .composite_strategy import CompositeStrategy


def build_strategy(cfg: StrategyConfig) -> Strategy:
    name = cfg.name.lower()
    params = dict(cfg.params or {})
    if name == "sma_cross":
        return SMACrossStrategy(
            fast=int(params.get("fast", cfg.fast)),
            slow=int(params.get("slow", cfg.slow)),
            order_size=int(params.get("order_size", cfg.order_size)),
        )
    if name == "ma_trend_filter":
        return MATrendFilterStrategy(
            fast=int(params.get("fast", cfg.fast)),
            slow=int(params.get("slow", cfg.slow)),
            order_size=int(params.get("order_size", cfg.order_size)),
            trend_ma=int(params.get("trend_ma", cfg.trend_ma)),
            use_benchmark_regime=bool(params.get("use_benchmark_regime", cfg.use_benchmark_regime)),
        )
    if name == "ma_atr_stop":
        return MAAtrStopStrategy(
            fast=int(params.get("fast", cfg.fast)),
            slow=int(params.get("slow", cfg.slow)),
            order_size=int(params.get("order_size", cfg.order_size)),
            atr_n=int(params.get("atr_n", cfg.atr_n)),
            atr_k=float(params.get("atr_k", cfg.atr_k)),
        )
    if name == "sma_cross_regime":
        return SMACrossRegimeStrategy(
            fast=int(params.get("fast", cfg.fast)),
            slow=int(params.get("slow", cfg.slow)),
            order_size=int(params.get("order_size", cfg.order_size)),
            regime_ma=int(params.get("regime_ma", cfg.trend_ma)),
            benchmark_symbol=str(params.get("benchmark_symbol", "000300.SH")),
            entry_buffer=float(params.get("entry_buffer", 0.0)),
            buffer_mode=str(params.get("buffer_mode", "slow_ma")),
            enable_trend_filter=bool(params.get("enable_trend_filter", False)),
            enable_trend_tier=bool(params.get("enable_trend_tier", True)),
            enable_trend_accel=bool(params.get("enable_trend_accel", True)),
            enable_trend_break_exit=bool(params.get("enable_trend_break_exit", True)),
            trend_break_confirm_days=int(params.get("trend_break_confirm_days", 3)),
            trend_break_confirm_gap_pct=float(params.get("trend_break_confirm_gap_pct", 0.0)),
            trend_break_use_benchmark_regime=bool(params.get("trend_break_use_benchmark_regime", False)),
            trend_tier_mode=str(params.get("trend_tier_mode", "MA120_GT_MA200")),
            weak_trend_order_scale=float(params.get("weak_trend_order_scale", 0.5)),
            strong_trend_order_scale=float(params.get("strong_trend_order_scale", 1.0)),
            trend_fast_ma=int(params.get("trend_fast_ma", 120)),
            trend_slow_ma=int(params.get("trend_slow_ma", 200)),
            enable_trend_slope_filter=bool(params.get("enable_trend_slope_filter", False)),
            slope_lookback=int(params.get("slope_lookback", 5)),
            enable_trend_continuity_filter=bool(params.get("enable_trend_continuity_filter", False)),
            continuity_n=int(params.get("continuity_n", 10)),
        )
    if name == "sma_cross_regime_atr":
        return SMACrossRegimeATRStrategy(
            fast=int(params.get("fast", cfg.fast)),
            slow=int(params.get("slow", cfg.slow)),
            order_size=int(params.get("order_size", cfg.order_size)),
            regime_ma=int(params.get("regime_ma", 120)),
            benchmark_symbol=str(params.get("benchmark_symbol", "000300.SH")),
            entry_buffer=float(params.get("entry_buffer", 0.01)),
            buffer_mode=str(params.get("buffer_mode", "slow_ma")),
            enable_trend_filter=bool(params.get("enable_trend_filter", False)),
            enable_trend_tier=bool(params.get("enable_trend_tier", True)),
            enable_trend_accel=bool(params.get("enable_trend_accel", True)),
            enable_trend_break_exit=bool(params.get("enable_trend_break_exit", True)),
            trend_break_confirm_days=int(params.get("trend_break_confirm_days", 3)),
            trend_break_confirm_gap_pct=float(params.get("trend_break_confirm_gap_pct", 0.0)),
            trend_break_use_benchmark_regime=bool(params.get("trend_break_use_benchmark_regime", False)),
            trend_tier_mode=str(params.get("trend_tier_mode", "MA120_GT_MA200")),
            weak_trend_order_scale=float(params.get("weak_trend_order_scale", 0.5)),
            strong_trend_order_scale=float(params.get("strong_trend_order_scale", 1.0)),
            trend_fast_ma=int(params.get("trend_fast_ma", 120)),
            trend_slow_ma=int(params.get("trend_slow_ma", 200)),
            enable_trend_slope_filter=bool(params.get("enable_trend_slope_filter", False)),
            slope_lookback=int(params.get("slope_lookback", 5)),
            enable_trend_continuity_filter=bool(params.get("enable_trend_continuity_filter", False)),
            continuity_n=int(params.get("continuity_n", 10)),
            atr_n=int(params.get("atr_n", 14)),
            atr_stop_k=float(params.get("atr_stop_k", 2.0)),
            atr_trail_k=float(params.get("atr_trail_k", 3.0)),
            enable_atr_stop=bool(params.get("enable_atr_stop", False)),
            enable_atr_trail=bool(params.get("enable_atr_trail", False)),
            debug_atr=bool(params.get("debug_atr", False)),
        )
    if name == "sma_cross_multifactor":
        # sma_cross_multifactor已废弃，请使用composite策略替代
        raise ValueError(
            "Strategy 'sma_cross_multifactor' is deprecated. "
            "Please use 'composite' strategy with independent factor strategies instead. "
            "See MULTIFACTOR_INCREMENTAL_V1 variants for examples."
        )
    if name == "multifactor_signal":
        # 独立的多因子信号策略
        return MultifactorSignalStrategy(
            benchmark_symbol=str(params.get("benchmark_symbol", "000300.SH")),
            buy_threshold=float(params.get("buy_threshold", 50.0)),
            sell_threshold=float(params.get("sell_threshold", 30.0)),
            max_hold_days=int(params.get("max_hold_days", 0)),
            min_data_days=int(params.get("min_data_days", 60)),
            rebalance_on_score_change=bool(params.get("rebalance_on_score_change", True)),
        )
    if name == "volume_breakout":
        # 量能突破策略
        return VolumeBreakoutStrategy(
            vol_ma_period=int(params.get("vol_ma_period", 20)),
            vol_mult=float(params.get("vol_mult", 1.5)),
            min_data_days=int(params.get("min_data_days", 30)),
        )
    if name == "momentum_roc":
        # 20日动量策略
        return MomentumROCStrategy(
            roc_period=int(params.get("roc_period", 20)),
            roc_threshold=float(params.get("roc_threshold", 0.0)),
            enable_roc_exit=bool(params.get("enable_roc_exit", False)),
            min_data_days=int(params.get("min_data_days", 30)),
        )
    if name == "relative_strength":
        # 相对强度策略
        return RelativeStrengthStrategy(
            rs_period=int(params.get("rs_period", 60)),
            rs_threshold=float(params.get("rs_threshold", 0.7)),
            min_data_days=int(params.get("min_data_days", 70)),
        )
    if name == "bollinger_breakout":
        # 布林带突破策略
        return BollingerBreakoutStrategy(
            bb_period=int(params.get("bb_period", 20)),
            bb_std_mult=float(params.get("bb_std_mult", 2.0)),
            enable_bb_mid_exit=bool(params.get("enable_bb_mid_exit", False)),
            min_data_days=int(params.get("min_data_days", 30)),
        )
    if name == "composite":
        # 组合策略
        sub_strategies = []
        strategies_config = params.get("strategies", [])
        for sub_cfg in strategies_config:
            sub_strategy = build_strategy(
                StrategyConfig(
                    name=sub_cfg.get("name", ""),
                    params=sub_cfg.get("params", {}),
                )
            )
            sub_strategies.append(sub_strategy)

        return CompositeStrategy(
            strategies=sub_strategies,
            combine_mode=str(params.get("combine_mode", "AND")),
            vote_threshold=float(params.get("vote_threshold", 0.5)),
            debug=bool(params.get("debug", False)),
            log_interval_days=int(params.get("log_interval_days", 0)),
        )
    raise ValueError(f"unsupported strategy: {cfg.name}")
