"""
PAMS Risk Protocol — 风险状态机
──────────────────────────────
三种模式: NORMAL → DEGRADED → CONSERVATIVE
触发条件: 连续亏损 / 回撤 / 结构性失败 / 资金锁
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class RiskMode(Enum):
    NORMAL = "NORMAL"
    DEGRADED = "DEGRADED"
    CONSERVATIVE = "CONSERVATIVE"


@dataclass(frozen=True)
class RiskProtocolConfig:
    """Risk Protocol 配置 (不可变)。"""

    # Trigger A: 连续亏损
    streak_threshold: int = 12           # 连续 N 次亏损 → DEGRADED
    streak_degraded_trades: int = 20     # DEGRADED 持续 N 笔交易

    # Trigger B: 回撤
    dd_enter_threshold: float = -25.0    # DD <= -25% → DEGRADED
    dd_exit_threshold: float = -15.0     # DD > -15% → 恢复 NORMAL

    # Trigger C: 结构性失败
    neg_months_threshold: int = 9        # 连续 N 月负收益 + 指数 < MA200

    # 资金锁
    capital_lock_mult: float = 2.0       # 权益翻倍触发
    capital_lock_r_reduction: float = 0.003  # R 永久降 0.3%

    # 风险参数
    normal_risk_pct: float = 0.025       # NORMAL R = 2.5%
    degraded_risk_pct: float = 0.015     # DEGRADED R = 1.5%
    conservative_risk_pct: float = 0.01  # CONSERVATIVE R = 1.0%

    # CONSERVATIVE 信号过滤
    conservative_top_pct: float = 0.20   # 仅保留 top 20%

    # 综合评分权重
    score_rs_weight: float = 0.40
    score_volume_weight: float = 0.30
    score_breakout_weight: float = 0.30


class RiskProtocol:
    """Risk Protocol 状态机。

    在回测主循环中使用:
    - on_trade_close(): 每笔平仓后调用
    - on_month_end(): 月末调用
    - check_structural_failure(): 月末检查 Trigger C
    - get_effective_risk(): 获取当前有效 R
    - filter_signals(): CONSERVATIVE 模式信号过滤
    - should_allow_pyramid(): CONSERVATIVE 禁止加仓
    """

    def __init__(self, config: RiskProtocolConfig, initial_capital: float) -> None:
        self.config = config
        self.initial_capital = initial_capital

        # 状态
        self.mode: RiskMode = RiskMode.NORMAL
        self.r_reduction: float = 0.0
        self.consec_losses: int = 0
        self.degraded_trades_remaining: int = 0
        self.monthly_returns: Dict[str, float] = {}
        self.capital_lock_triggered: bool = False

        # 统计
        self.events: List[Dict[str, Any]] = []
        self.blocked_entries: int = 0
        self.blocked_pyramids: int = 0
        self.mode_days: Dict[str, int] = {
            "NORMAL": 0, "DEGRADED": 0, "CONSERVATIVE": 0,
        }
        self.r_history: List[float] = []

    def get_effective_risk(self) -> float:
        """当前有效 R (含资金锁扣减)，下限 0.5%。"""
        base = {
            RiskMode.NORMAL: self.config.normal_risk_pct,
            RiskMode.DEGRADED: self.config.degraded_risk_pct,
            RiskMode.CONSERVATIVE: self.config.conservative_risk_pct,
        }[self.mode]
        return max(base - self.r_reduction, 0.005)

    def record_day(self) -> None:
        """每日调用，记录模式分布。"""
        self.mode_days[self.mode.value] += 1
        self.r_history.append(self.get_effective_risk())

    def on_trade_close(
        self,
        pnl: float,
        trade_date_str: str,
        equity: float,
        drawdown_pct: float,
    ) -> None:
        """每笔平仓后调用，更新连亏 / 回撤 / 资金锁状态。"""
        # 1. 连续亏损
        if pnl <= 0:
            self.consec_losses += 1
        else:
            self.consec_losses = 0

        # 2. DEGRADED 倒计时
        if self.mode == RiskMode.DEGRADED and self.degraded_trades_remaining > 0:
            self.degraded_trades_remaining -= 1

        # 3. 触发检查

        # Trigger A: 连续亏损 → DEGRADED
        if (
            self.consec_losses >= self.config.streak_threshold
            and self.mode == RiskMode.NORMAL
        ):
            self._enter_degraded(
                trade_date_str,
                f"RP_ENTER_DEGRADED_STREAK{self.config.streak_threshold}",
                f"连续亏损 {self.consec_losses} 笔",
            )

        # Trigger B: 回撤 → DEGRADED
        if (
            drawdown_pct <= self.config.dd_enter_threshold
            and self.mode == RiskMode.NORMAL
        ):
            self._enter_degraded(
                trade_date_str,
                "RP_ENTER_DEGRADED_DD",
                f"回撤 {drawdown_pct:.1f}% <= {self.config.dd_enter_threshold:.0f}%",
            )

        # 恢复检查: DEGRADED → NORMAL
        if self.mode == RiskMode.DEGRADED:
            can_recover_trades = self.degraded_trades_remaining <= 0
            can_recover_dd = drawdown_pct > self.config.dd_exit_threshold
            if can_recover_trades and can_recover_dd:
                self.mode = RiskMode.NORMAL
                self.events.append({
                    "date": trade_date_str,
                    "event": "RP_EXIT_DEGRADED_RECOVER",
                    "detail": f"回撤恢复至 {drawdown_pct:.1f}%，退出 DEGRADED",
                    "mode": self.mode.value,
                })
                logger.info("RP: 恢复 NORMAL (DD=%.1f%%)", drawdown_pct)

        # 资金锁
        if (
            equity >= self.initial_capital * self.config.capital_lock_mult
            and not self.capital_lock_triggered
        ):
            self.capital_lock_triggered = True
            self.r_reduction += self.config.capital_lock_r_reduction
            self.events.append({
                "date": trade_date_str,
                "event": "RP_CAPITAL_LOCK",
                "detail": (
                    f"权益 ¥{equity:,.0f} >= "
                    f"{self.config.capital_lock_mult:.1f}x 初始，"
                    f"R 永久降 {self.config.capital_lock_r_reduction * 100:.1f}%"
                ),
                "mode": self.mode.value,
            })
            logger.info("RP: 资金锁触发，R reduction=%.3f", self.r_reduction)

    def on_month_end(self, month_str: str, month_return_pct: float) -> None:
        """月末调用，记录月度收益。"""
        self.monthly_returns[month_str] = month_return_pct

    def check_structural_failure(
        self,
        trade_date_str: str,
        index_close: Optional[float],
        index_ma200: Optional[float],
    ) -> bool:
        """Trigger C: 连续 N 月负收益 + 指数 < MA200 → CONSERVATIVE。"""
        if index_close is None or index_ma200 is None:
            return False
        if len(self.monthly_returns) < self.config.neg_months_threshold:
            return False

        recent = list(self.monthly_returns.values())[
            -self.config.neg_months_threshold:
        ]
        all_negative = all(r < 0 for r in recent)

        if all_negative and index_close < index_ma200:
            if self.mode != RiskMode.CONSERVATIVE:
                self.mode = RiskMode.CONSERVATIVE
                self.events.append({
                    "date": trade_date_str,
                    "event": "RP_ENTER_CONSERVATIVE",
                    "detail": (
                        f"连续 {self.config.neg_months_threshold} 月负收益 + "
                        f"指数 {index_close:.0f} < MA200 {index_ma200:.0f}"
                    ),
                    "mode": self.mode.value,
                })
                logger.info("RP: 进入 CONSERVATIVE")
            return True
        return False

    def filter_signals(self, signals: List[Dict]) -> List[Dict]:
        """CONSERVATIVE 模式: 综合评分，仅保留 top N%。"""
        if self.mode != RiskMode.CONSERVATIVE or not signals:
            return signals

        cfg = self.config
        for s in signals:
            s["_score"] = (
                s.get("rs", 0) * cfg.score_rs_weight
                + s.get("volume_ratio", 0) * cfg.score_volume_weight
                + s.get("breakout_mag", 0) * cfg.score_breakout_weight
            )
        signals.sort(key=lambda x: x.get("_score", 0), reverse=True)

        keep = max(1, int(len(signals) * cfg.conservative_top_pct))
        blocked = len(signals) - keep
        self.blocked_entries += blocked
        return signals[:keep]

    def should_allow_pyramid(self) -> bool:
        """CONSERVATIVE 模式禁止金字塔加仓。"""
        if self.mode == RiskMode.CONSERVATIVE:
            self.blocked_pyramids += 1
            return False
        return True

    def get_summary(self) -> Dict[str, Any]:
        """输出 RP 统计摘要。"""
        total_days = sum(self.mode_days.values()) or 1
        avg_r = (
            sum(self.r_history) / len(self.r_history) * 100
            if self.r_history
            else self.config.normal_risk_pct * 100
        )
        return {
            "normal_pct": round(self.mode_days["NORMAL"] / total_days * 100, 1),
            "degraded_pct": round(self.mode_days["DEGRADED"] / total_days * 100, 1),
            "conservative_pct": round(
                self.mode_days["CONSERVATIVE"] / total_days * 100, 1,
            ),
            "avg_r_effective": round(avg_r, 2),
            "r_reduction": round(self.r_reduction * 100, 2),
            "capital_lock_triggered": self.capital_lock_triggered,
            "blocked_entries": self.blocked_entries,
            "blocked_pyramids": self.blocked_pyramids,
            "events_count": len(self.events),
            "max_consec_losses": self.consec_losses,
        }

    # ---- 内部 ----

    def _enter_degraded(
        self, trade_date_str: str, event_code: str, detail: str,
    ) -> None:
        self.mode = RiskMode.DEGRADED
        self.degraded_trades_remaining = self.config.streak_degraded_trades
        self.events.append({
            "date": trade_date_str,
            "event": event_code,
            "detail": detail,
            "mode": self.mode.value,
        })
        logger.info("RP: %s — %s", event_code, detail)
