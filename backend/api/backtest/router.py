"""
回测 API 路由。
POST /backtest/run   - 运行回测 (30-60 秒)
GET  /backtest/watchlist - 默认股票池
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .engine import BacktestParams, build_watchlist_from_db, run_backtest
from .engine_v2 import run_backtest_v2
from .risk_protocol import RiskProtocolConfig

logger = logging.getLogger(__name__)
router = APIRouter(tags=["backtest"])


class BacktestRequest(BaseModel):
    initial_capital: float = Field(100_000.0, ge=10_000, le=10_000_000)
    max_positions: int = Field(3, ge=1, le=10)
    risk_per_trade_pct: float = Field(0.02, ge=0.005, le=0.10)
    risk_neutral_pct: float = Field(0.01, ge=0.005, le=0.05)
    max_position_value_pct: float = Field(0.50, ge=0.10, le=1.0)
    stop_atr_mult: float = Field(2.5, ge=1.0, le=5.0)
    trailing_atr_mult: float = Field(3.0, ge=1.0, le=10.0)
    pool_source: str = Field("dynamic", pattern=r"^(db|custom|dynamic)$")
    start_date: str = "2025-10-01"
    end_date: str = "2026-02-01"
    symbols: Optional[List[str]] = None
    index_code: str = "sh000300"
    screen_interval_days: int = Field(5, ge=1, le=20)
    screen_min_turnover: float = Field(200_000_000, ge=0)
    # 金字塔
    pyramid_enabled: bool = True
    pyramid_max_adds: int = Field(2, ge=0, le=5)
    pyramid_risk_frac: float = Field(0.5, ge=0.1, le=1.0)
    pyramid_trigger_r: float = Field(1.0, ge=0.5, le=5.0)
    # 突破
    breakout_lookback: int = Field(20, ge=5, le=60)
    volume_mult: float = Field(1.2, ge=0.5, le=3.0)


class BacktestRequestV2(BacktestRequest):
    """V2 请求: V1 全部参数 + Risk Protocol 参数。"""
    rp_streak_threshold: int = Field(12, ge=3, le=30)
    rp_streak_trades: int = Field(20, ge=5, le=50)
    rp_dd_enter: float = Field(-25.0, le=-5.0, ge=-50.0)
    rp_dd_exit: float = Field(-15.0, le=-5.0, ge=-40.0)
    rp_neg_months: int = Field(9, ge=3, le=24)
    rp_capital_lock_mult: float = Field(2.0, ge=1.5, le=5.0)
    rp_capital_lock_r_reduction: float = Field(0.003, ge=0.001, le=0.02)
    rp_normal_risk: float = Field(0.025, ge=0.005, le=0.10)
    rp_degraded_risk: float = Field(0.015, ge=0.005, le=0.05)
    rp_conservative_risk: float = Field(0.01, ge=0.005, le=0.03)
    rp_conservative_top_pct: float = Field(0.20, ge=0.05, le=1.0)


@router.post("/backtest/run")
def api_run_backtest(body: BacktestRequest):
    """运行 HV Alpha Model 回测。耗时 30-60 秒。"""
    try:
        params = BacktestParams(
            initial_capital=body.initial_capital,
            max_positions=body.max_positions,
            risk_per_trade_pct=body.risk_per_trade_pct,
            risk_neutral_pct=body.risk_neutral_pct,
            max_position_value_pct=body.max_position_value_pct,
            stop_atr_mult=body.stop_atr_mult,
            trailing_atr_mult=body.trailing_atr_mult,
            pool_source=body.pool_source,
            start_date=body.start_date,
            end_date=body.end_date,
            symbols=body.symbols,
            index_code=body.index_code,
            screen_interval_days=body.screen_interval_days,
            screen_min_turnover=body.screen_min_turnover,
            pyramid_enabled=body.pyramid_enabled,
            pyramid_max_adds=body.pyramid_max_adds,
            pyramid_risk_frac=body.pyramid_risk_frac,
            pyramid_trigger_r=body.pyramid_trigger_r,
            breakout_lookback=body.breakout_lookback,
            volume_mult=body.volume_mult,
        )
        result = run_backtest(params)
        return JSONResponse(content=result)
    except Exception as e:
        logger.exception("回测执行失败")
        raise HTTPException(status_code=500, detail=f"回测失败: {str(e)}")


@router.post("/backtest/run_v2")
def api_run_backtest_v2(body: BacktestRequestV2):
    """运行 HV Alpha V2 Risk Protocol 回测。"""
    try:
        params = BacktestParams(
            initial_capital=body.initial_capital,
            max_positions=body.max_positions,
            risk_per_trade_pct=body.risk_per_trade_pct,
            risk_neutral_pct=body.risk_neutral_pct,
            max_position_value_pct=body.max_position_value_pct,
            stop_atr_mult=body.stop_atr_mult,
            trailing_atr_mult=body.trailing_atr_mult,
            pool_source=body.pool_source,
            start_date=body.start_date,
            end_date=body.end_date,
            symbols=body.symbols,
            index_code=body.index_code,
            screen_interval_days=body.screen_interval_days,
            screen_min_turnover=body.screen_min_turnover,
            pyramid_enabled=body.pyramid_enabled,
            pyramid_max_adds=body.pyramid_max_adds,
            pyramid_risk_frac=body.pyramid_risk_frac,
            pyramid_trigger_r=body.pyramid_trigger_r,
            breakout_lookback=body.breakout_lookback,
            volume_mult=body.volume_mult,
        )
        rp_config = RiskProtocolConfig(
            streak_threshold=body.rp_streak_threshold,
            streak_degraded_trades=body.rp_streak_trades,
            dd_enter_threshold=body.rp_dd_enter,
            dd_exit_threshold=body.rp_dd_exit,
            neg_months_threshold=body.rp_neg_months,
            capital_lock_mult=body.rp_capital_lock_mult,
            capital_lock_r_reduction=body.rp_capital_lock_r_reduction,
            normal_risk_pct=body.rp_normal_risk,
            degraded_risk_pct=body.rp_degraded_risk,
            conservative_risk_pct=body.rp_conservative_risk,
            conservative_top_pct=body.rp_conservative_top_pct,
        )
        result = run_backtest_v2(params, rp_config)
        return JSONResponse(content=result)
    except Exception as e:
        logger.exception("V2 回测执行失败")
        raise HTTPException(status_code=500, detail=f"V2 回测失败: {str(e)}")


@router.get("/backtest/watchlist")
def api_get_watchlist():
    """返回观察池 (从 DB 读取，失败回退硬编码)。"""
    pool = build_watchlist_from_db()
    return JSONResponse(content={
        "rows": [{"symbol": s, "name": n} for s, n in pool]
    })
