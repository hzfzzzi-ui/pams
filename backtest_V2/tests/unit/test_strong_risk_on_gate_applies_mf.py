from __future__ import annotations

from datetime import date

from backtest_v2.core.models import Signal
from backtest_v2.core.types import Side
from backtest_v2.portfolio.portfolio_builder import build_rebalance_orders, resolve_mf_gate_on_today


def _buy(day: date, symbol: str) -> Signal:
    return Signal(
        signal_date=day,
        symbol=symbol,
        side=Side.BUY,
        qty=100,
        action="BUY",
        reason="SMA_CROSS_UP",
        reason_code="SMA_CROSS_UP",
        meta={"order_value_scale": 1.0, "trend_tier": "STRONG", "strong_trend": True},
    )


def test_strong_risk_on_gate_controls_mf_apply() -> None:
    day = date(2025, 1, 7)
    buys = [_buy(day, f"sh600{100+i}") for i in range(10)]
    next_open = {str(s.symbol).lower(): 10.0 for s in buys}
    mf_scores = {str(s.symbol).lower(): float(i) / 10.0 for i, s in enumerate(buys)}

    # strong_risk_on=False: gate closed, MF should not apply.
    debug_off: dict[str, object] = {}
    gate_off = resolve_mf_gate_on_today(
        enable_multifactor_rank=True,
        mf_rank_gate_mode="STRONG_RISK_ON",
        risk_on_today=True,
        strong_risk_on_today=False,
    )
    _ = build_rebalance_orders(
        positions={},
        cash_available=1_000_000.0,
        nav_close=1_000_000.0,
        max_positions=3,
        lot_size=100,
        sell_signals=[],
        buy_signals=buys,
        next_open=next_open,
        enable_multifactor_rank=True,
        mf_topk=5,
        mf_concentrate_topn=3,
        mf_concentrate_scale=1.5,
        mf_gate_on_today=gate_off,
        mf_score_map=mf_scores,
        decision_date=day,
        debug_plan=debug_off,
    )
    assert gate_off is False
    assert bool(debug_off.get("mf_rank_applied", True)) is False
    assert int(debug_off.get("mf_topk_reject_count", 1)) == 0
    assert int(debug_off.get("mf_concentrated_selected_count", 1)) == 0

    # strong_risk_on=True: gate open, MF should apply under candidates>slots.
    debug_on: dict[str, object] = {}
    gate_on = resolve_mf_gate_on_today(
        enable_multifactor_rank=True,
        mf_rank_gate_mode="STRONG_RISK_ON",
        risk_on_today=True,
        strong_risk_on_today=True,
    )
    _ = build_rebalance_orders(
        positions={},
        cash_available=1_000_000.0,
        nav_close=1_000_000.0,
        max_positions=3,
        lot_size=100,
        sell_signals=[],
        buy_signals=buys,
        next_open=next_open,
        enable_multifactor_rank=True,
        mf_topk=5,
        mf_concentrate_topn=3,
        mf_concentrate_scale=1.5,
        mf_gate_on_today=gate_on,
        mf_score_map=mf_scores,
        decision_date=day,
        debug_plan=debug_on,
    )
    assert gate_on is True
    assert bool(debug_on.get("mf_rank_applied", False)) is True
    assert int(debug_on.get("mf_topk_reject_count", 0)) > 0
    assert int(debug_on.get("mf_concentrated_selected_count", 0)) > 0

