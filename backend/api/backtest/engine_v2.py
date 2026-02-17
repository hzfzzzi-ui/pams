"""
PAMS HV Alpha V2 — Risk Protocol 回测引擎
─────────────────────────────────────────
在 V1 HV Alpha 基础上叠加 Risk Protocol 状态机：
  NORMAL (R=2.5%) → DEGRADED (R=1.5%) → CONSERVATIVE (R=1%)

V1 engine.py 完全不修改，本模块通过组合复用。
"""

import logging
import math
import time
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from .engine import (
    BacktestParams,
    _Engine,
    _Position,
    _TradeRecord,
    _add_atr_column,
    _breakout_signal,
    _build_benchmark_curve,
    _build_broad_universe,
    _build_weekly_snapshots,
    _calc_entry_stop,
    _calc_shares,
    _check_pyramid_add,
    _compute_indicators,
    _download_all,
    _download_ohlcv,
    _generate_report,
    _get_regime,
    _resolve_custom_symbols,
    _weekly_screen,
    build_watchlist_from_db,
    BENCHMARK_INDEX,
    ENTRY_PREMIUM_PCT,
    INDEX_MA_PERIOD,
    MIN_BARS,
)
from .risk_protocol import RiskMode, RiskProtocol, RiskProtocolConfig

logger = logging.getLogger(__name__)


# ==================== 辅助函数 ====================


def _get_index_values(
    index_df: Optional[pd.DataFrame], trade_date: date,
) -> Optional[Dict[str, float]]:
    """获取指数当日 close 和 MA200。"""
    if index_df is None:
        return None
    rows = index_df[index_df["trade_date"] <= trade_date]
    if len(rows) < INDEX_MA_PERIOD:
        return None
    return {
        "close": float(rows.iloc[-1]["close"]),
        "ma200": float(rows["close"].astype(float).tail(INDEX_MA_PERIOD).mean()),
    }


# ==================== V2 信号扫描 ====================


def _scan_signals_v2(
    all_data: Dict[str, pd.DataFrame],
    names: Dict[str, str],
    trade_date: date,
    engine: _Engine,
    params: BacktestParams,
    index_df: Optional[pd.DataFrame],
    rp: RiskProtocol,
) -> List[Dict]:
    """V2 信号扫描: V1 逻辑 + volume_ratio/breakout_mag + RP 风险调整 + 信号过滤。"""
    held_syms = {p.symbol for p in engine.positions}
    signals: List[Dict] = []

    effective_risk = rp.get_effective_risk()

    for sym, df in all_data.items():
        if sym in held_syms:
            continue

        mask = df["trade_date"] == trade_date
        if not mask.any():
            continue
        idx = int(df.index[mask][0])

        ind = _compute_indicators(df, idx, index_df)
        if ind is None:
            continue

        # 突破信号 (同 V1)
        ok, _reason, detail = _breakout_signal(
            ind, df, idx, params.breakout_lookback, params.volume_mult,
        )
        if not ok:
            continue

        # Regime (仍用于仓位缩减)
        regime = _get_regime(ind)

        entry, stop = _calc_entry_stop(ind, params)
        if entry <= stop:
            continue

        # RP 风险替代 regime-based risk
        risk_pct = effective_risk

        shares = _calc_shares(
            entry, stop, engine.equity, engine.cash,
            risk_pct, params.max_position_value_pct,
        )
        if shares <= 0:
            continue

        # NEUTRAL → 降 30% 仓位 (保持 V1 行为)
        if regime == "NEUTRAL":
            shares = int(shares * 0.7 / 100) * 100
            if shares <= 0:
                continue

        pos_value = entry * shares
        stop_dist_pct = (entry - stop) / entry * 100 if entry > 0 else 0

        # 计算 V2 额外字段
        volume_ratio = detail.get("volume_ratio", 0)
        prev_high = detail.get("prev_high", entry)
        breakout_mag = (
            (ind["close"] - prev_high) / prev_high
            if prev_high > 0
            else 0
        )

        reasoning = {
            "breakout": (
                f"突破 {params.breakout_lookback}日高 "
                f"¥{prev_high:.2f}, "
                f"放量 {volume_ratio:.1f}x, "
                f"RS={ind.get('rs', 0):.3f}"
            ),
            "regime": (
                f"{regime}: 指数"
                f" {ind.get('index_close', '?')}"
                f" vs MA200 {ind.get('index_ma200', '?')}"
            ),
            "entry_stop": (
                f"入场¥{entry:.2f}, 止损¥{stop:.2f} "
                f"(距离{stop_dist_pct:.1f}%, ATR={ind['atr14']:.2f})"
            ),
            "position": (
                f"{shares}股 × ¥{entry:.2f} = ¥{pos_value:,.0f} "
                f"(占NAV {pos_value / engine.equity * 100:.1f}%, "
                f"RP风险{effective_risk * 100:.2f}%)"
            ),
            "risk_mode": rp.mode.value,
        }

        signals.append({
            "symbol": sym,
            "name": names.get(sym, sym),
            "entry": entry,
            "stop": stop,
            "shares": shares,
            "regime": regime,
            "rs": ind.get("rs", 0),
            "volume_ratio": volume_ratio,
            "breakout_mag": round(breakout_mag, 4),
            "reasoning": reasoning,
        })

    # 按 RS 降序
    signals.sort(key=lambda x: x.get("rs", 0), reverse=True)

    # RP 信号过滤 (CONSERVATIVE 模式)
    signals = rp.filter_signals(signals)

    return signals


# ==================== V2 报告 ====================


def _generate_report_v2(
    summary: Dict[str, Any],
    trades_out: List[Dict],
    benchmark_curve: List[Dict],
    params: BacktestParams,
    rp: RiskProtocol,
) -> Dict[str, Any]:
    """V1 报告 + Risk Protocol 专属 section。"""
    report = _generate_report(summary, trades_out, benchmark_curve, params)

    rp_summary = rp.get_summary()
    report["risk_protocol"] = rp_summary

    # 插入 RP section (在"改进建议"前)
    rp_content = (
        f"模式分布: NORMAL {rp_summary['normal_pct']:.0f}% / "
        f"DEGRADED {rp_summary['degraded_pct']:.0f}% / "
        f"CONSERVATIVE {rp_summary['conservative_pct']:.0f}%。"
        f" 有效 R 均值 {rp_summary['avg_r_effective']:.2f}%。"
    )
    if rp_summary["blocked_entries"] > 0 or rp_summary["blocked_pyramids"] > 0:
        rp_content += (
            f" 拦截 {rp_summary['blocked_entries']} 次入场 + "
            f"{rp_summary['blocked_pyramids']} 次加仓。"
        )
    if rp_summary["capital_lock_triggered"]:
        rp_content += f" 资金锁已触发 (R 永久降 {rp_summary['r_reduction']:.1f}%)。"

    rp_section = {"title": "Risk Protocol", "content": rp_content}

    # 插入在最后一个 section ("改进建议") 前
    if report["sections"]:
        report["sections"].insert(-1, rp_section)
    else:
        report["sections"].append(rp_section)

    return report


# ==================== V2 主入口 ====================


def run_backtest_v2(
    params: BacktestParams, rp_config: RiskProtocolConfig,
) -> Dict[str, Any]:
    """运行 HV Alpha V2 Risk Protocol 回测。"""
    start_d = date.fromisoformat(params.start_date)
    end_d = date.fromisoformat(params.end_date)
    data_start = start_d - timedelta(days=350)

    # ── 1. 构建观察池 (同 V1) ──
    if params.pool_source == "custom" and params.symbols:
        pool = _resolve_custom_symbols(params.symbols)
    elif params.pool_source == "dynamic":
        pool = _build_broad_universe()
    else:
        full_pool = build_watchlist_from_db()
        if params.symbols:
            sym_set = set(params.symbols)
            pool = [(s, n) for s, n in full_pool if s in sym_set]
        else:
            pool = full_pool

    names = {s: n for s, n in pool}

    # ── 2. 下载数据 + ATR (同 V1) ──
    all_data = _download_all(pool, data_start, end_d)
    all_data = {sym: _add_atr_column(df) for sym, df in all_data.items()}
    if not all_data:
        return {
            "error": "无有效数据", "summary": {}, "trades": [],
            "equity_curve": [], "weekly_snapshots": [], "params": {},
        }

    index_df = _download_ohlcv(params.index_code, data_start, end_d)
    if index_df is not None:
        index_df = _add_atr_column(index_df)
        logger.info("指数 %s 下载 %d 条", params.index_code, len(index_df))
    else:
        logger.warning("指数 %s 下载失败", params.index_code)

    benchmark_df = None
    if params.index_code == BENCHMARK_INDEX:
        benchmark_df = index_df
    else:
        benchmark_df = _download_ohlcv(BENCHMARK_INDEX, data_start, end_d)
        if benchmark_df is not None:
            logger.info("基准 %s 下载 %d 条", BENCHMARK_INDEX, len(benchmark_df))

    # ── 3. 交易日序列 (同 V1) ──
    sample_df = next(iter(all_data.values()))
    all_dates = sorted(sample_df["trade_date"].unique())
    bt_dates = [d for d in all_dates if start_d <= d <= end_d]

    if not bt_dates:
        first = next((d for d in all_dates if d >= start_d), None)
        if first:
            bt_dates = [d for d in all_dates if first <= d <= end_d]

    if not bt_dates:
        return {
            "error": "回测区间无交易日", "summary": {}, "trades": [],
            "equity_curve": [], "weekly_snapshots": [], "params": {},
        }

    # ── 4. 初始化引擎 + RP ──
    engine = _Engine(
        cash=params.initial_capital,
        initial_capital=params.initial_capital,
        max_positions=params.max_positions,
    )
    rp = RiskProtocol(rp_config, params.initial_capital)

    # ── 5. 逐日执行 ──
    is_dynamic = params.pool_source == "dynamic"
    active_pool: set = set(all_data.keys()) if not is_dynamic else set()
    pool_changes: List[Dict[str, Any]] = []

    prev_month: Optional[str] = None
    month_start_eq: float = params.initial_capital

    for day_idx, trade_date in enumerate(bt_dates):

        # ── 月度跟踪 ──
        cur_month = trade_date.strftime("%Y-%m")
        if prev_month is not None and cur_month != prev_month:
            month_eq = engine.equity
            month_ret = (month_eq / month_start_eq - 1) * 100 if month_start_eq > 0 else 0
            rp.on_month_end(prev_month, month_ret)
            month_start_eq = month_eq

            # Trigger C 检查
            ind_vals = _get_index_values(index_df, trade_date)
            if ind_vals:
                rp.check_structural_failure(
                    trade_date.isoformat(),
                    ind_vals["close"],
                    ind_vals["ma200"],
                )

        if cur_month != prev_month:
            month_start_eq = engine.equity
        prev_month = cur_month

        # ── 5a. 动态筛选 (同 V1) ──
        if is_dynamic and (
            day_idx == 0 or day_idx % params.screen_interval_days == 0
        ):
            screened = set(_weekly_screen(
                all_data, trade_date, index_df, params.screen_min_turnover,
            ))
            held_syms = {p.symbol for p in engine.positions}
            prev_pool = active_pool - held_syms
            added = sorted(screened - active_pool)
            removed = sorted(prev_pool - screened)
            active_pool = screened | held_syms

            pool_changes.append({
                "date": trade_date.isoformat(),
                "day_idx": day_idx,
                "active_count": len(active_pool),
                "added": [{"symbol": s, "name": names.get(s, s)} for s in added],
                "removed": [{"symbol": s, "name": names.get(s, s)} for s in removed],
            })

        # ── 5b. 构建 price_map (同 V1) ──
        price_map: Dict[str, Dict] = {}
        for sym, df in all_data.items():
            rows = df[df["trade_date"] == trade_date]
            if not rows.empty:
                r = rows.iloc[0]
                atr_val = float(r["atr14"]) if pd.notna(r.get("atr14")) else None
                price_map[sym] = {
                    "open": float(r["open"]), "high": float(r["high"]),
                    "low": float(r["low"]), "close": float(r["close"]),
                    "volume": float(r["volume"]), "atr14": atr_val,
                }

        # ── 5c. 止损管理 + RP 通知 ──
        trades_before = len(engine.trades)
        engine.update_and_check_stops(price_map, trade_date, params)
        trades_after = len(engine.trades)

        # 通知 RP 新的平仓
        for t in engine.trades[trades_before:trades_after]:
            if t.action == "SELL":
                dd_pct = (
                    (engine.equity / engine.peak_equity - 1) * 100
                    if engine.peak_equity > 0
                    else 0.0
                )
                rp.on_trade_close(
                    t.pnl, trade_date.isoformat(), engine.equity, dd_pct,
                )

        # ── 5d. 金字塔加仓 (受 RP 控制) ──
        for pos in list(engine.positions):
            if not rp.should_allow_pyramid():
                break

            bar = price_map.get(pos.symbol)
            if not bar:
                continue
            sym_df = all_data.get(pos.symbol)
            if sym_df is None:
                continue
            td_mask = sym_df["trade_date"] == trade_date
            if not td_mask.any():
                continue
            as_of = int(sym_df.index[td_mask][0])

            if _check_pyramid_add(pos, bar, sym_df, as_of, params):
                add_risk = rp.get_effective_risk() * params.pyramid_risk_frac
                add_shares = _calc_shares(
                    bar["close"], pos.stop_price, engine.equity, engine.cash,
                    add_risk, params.max_position_value_pct,
                )

                # 单票上限检查
                pos_value_after = (pos.shares + add_shares) * bar["close"]
                if pos_value_after > engine.equity * params.max_position_value_pct:
                    max_shares = int(
                        (engine.equity * params.max_position_value_pct / bar["close"] - pos.shares) / 100
                    ) * 100
                    add_shares = max(max_shares, 0)

                if add_shares > 0:
                    reason = f"金字塔加仓#{pos.pyramid_count + 1}"
                    engine.add_to_position(
                        pos, add_shares, bar["close"], trade_date, reason,
                    )

        # ── 5e. 新突破扫描 ──
        if engine.can_open() and engine.cash > 0:
            scan_data = (
                {s: df for s, df in all_data.items() if s in active_pool}
                if is_dynamic
                else all_data
            )
            signals = _scan_signals_v2(
                scan_data, names, trade_date, engine, params, index_df, rp,
            )
            for sig in signals:
                if not engine.can_open():
                    break
                if engine.cash < sig["entry"] * 100:
                    continue
                engine.open_position(
                    sig["symbol"], sig["name"],
                    sig["entry"], sig["stop"], sig["shares"],
                    trade_date, sig["reasoning"],
                )
                if engine.positions:
                    engine.positions[-1].entry_bar_idx = day_idx

        # 记录每日 RP 模式
        rp.record_day()
        engine.snapshot(trade_date)

    # ── 6. 期末清算 (同 V1) ──
    if engine.positions:
        last_date = bt_dates[-1]
        for sym, df in all_data.items():
            rows = df[df["trade_date"] == last_date]
            if not rows.empty:
                close_price = float(rows.iloc[0]["close"])
                for pos in list(engine.positions):
                    if pos.symbol == sym:
                        engine.close_position(pos, close_price, last_date, "期末清算")

    # ── 7. 构建结果 (同 V1 + RP) ──
    sells = [t for t in engine.trades if t.action == "SELL"]
    wins = [t for t in sells if t.pnl > 0]
    final_eq = engine.equity
    ret_pct = (final_eq / engine.initial_capital - 1) * 100
    max_dd = min(eq[2] for eq in engine.equity_curve) if engine.equity_curve else 0.0

    avg_win = sum(t.pnl for t in wins) / len(wins) if wins else 0
    losses = [t for t in sells if t.pnl <= 0]
    avg_loss = abs(sum(t.pnl for t in losses) / len(losses)) if losses else 1
    payoff_ratio = round(avg_win / avg_loss, 2) if avg_loss > 0 else 0

    summary = {
        "initial_capital": engine.initial_capital,
        "final_equity": round(final_eq, 2),
        "total_return_pct": round(ret_pct, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "total_pnl": round(final_eq - engine.initial_capital, 2),
        "trades_count": len(engine.trades),
        "buys_count": len([t for t in engine.trades if t.action == "BUY"]),
        "sells_count": len(sells),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(sells) * 100, 1) if sells else 0,
        "payoff_ratio": payoff_ratio,
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "trading_days": len(bt_dates),
        "start_date": bt_dates[0].isoformat(),
        "end_date": bt_dates[-1].isoformat(),
        "strategy": "hv_alpha_v2_risk_protocol",
    }

    trades_out: List[Dict] = []
    for t in engine.trades:
        trades_out.append({
            "date": t.date.isoformat(),
            "action": t.action,
            "symbol": t.symbol,
            "name": t.name,
            "price": round(t.price, 2),
            "shares": t.shares,
            "amount": round(t.amount, 2),
            "pnl": round(t.pnl, 2),
            "reason": t.reason,
            "reasoning": t.reasoning,
        })

    equity_curve = [
        {"date": d.isoformat(), "equity": round(eq, 2), "drawdown_pct": round(dd, 2)}
        for d, eq, dd in engine.equity_curve
    ]

    weekly_snapshots = _build_weekly_snapshots(engine)
    benchmark_curve = _build_benchmark_curve(benchmark_df, bt_dates, params.initial_capital)

    report = _generate_report_v2(summary, trades_out, benchmark_curve, params, rp)

    # RP 事件 (序列化)
    rp_events = [
        {
            "date": e["date"],
            "event": e["event"],
            "detail": e.get("detail", ""),
            "mode": e.get("mode", ""),
        }
        for e in rp.events
    ]

    result: Dict[str, Any] = {
        "summary": summary,
        "trades": trades_out,
        "equity_curve": equity_curve,
        "benchmark_curve": benchmark_curve,
        "report": report,
        "weekly_snapshots": weekly_snapshots,
        "risk_protocol_events": rp_events,
        "params": {
            "initial_capital": params.initial_capital,
            "max_positions": params.max_positions,
            "risk_per_trade_pct": params.risk_per_trade_pct,
            "risk_neutral_pct": params.risk_neutral_pct,
            "max_position_value_pct": params.max_position_value_pct,
            "stop_atr_mult": params.stop_atr_mult,
            "trailing_atr_mult": params.trailing_atr_mult,
            "pool_source": params.pool_source,
            "start_date": params.start_date,
            "end_date": params.end_date,
            "symbols_count": len(pool),
            "pyramid_enabled": params.pyramid_enabled,
            "pyramid_max_adds": params.pyramid_max_adds,
            "pyramid_risk_frac": params.pyramid_risk_frac,
            "pyramid_trigger_r": params.pyramid_trigger_r,
            "breakout_lookback": params.breakout_lookback,
            "volume_mult": params.volume_mult,
            "index_code": params.index_code,
            "screen_interval_days": params.screen_interval_days,
            "strategy": "hv_alpha_v2_risk_protocol",
            # RP 参数
            "rp_normal_risk_pct": rp_config.normal_risk_pct,
            "rp_degraded_risk_pct": rp_config.degraded_risk_pct,
            "rp_conservative_risk_pct": rp_config.conservative_risk_pct,
            "rp_streak_threshold": rp_config.streak_threshold,
            "rp_dd_enter_threshold": rp_config.dd_enter_threshold,
            "rp_capital_lock_mult": rp_config.capital_lock_mult,
        },
    }
    if pool_changes:
        result["pool_changes"] = pool_changes
    return result
