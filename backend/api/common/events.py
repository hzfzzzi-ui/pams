"""
回测/实盘共享事件模型。
frozen dataclass 保证不可变，用于信号→订单→成交全链路审计。
"""
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class SignalEvent:
    """信号事件：策略管线产出的交易信号。"""
    symbol: str
    name: str
    signal_date: str                    # ISO date: 信号产生日
    signal_strength: float              # 综合评分 (0-100)
    entry_hint: float                   # 建议入场价
    stop_hint: float                    # 建议止损价
    position_size: int                  # 建议仓位 (股)
    reasons: tuple                      # 不可变 reason codes
    regime: str = "UNKNOWN"             # RISK_ON / NEUTRAL / RISK_OFF
    flow_result: str = "WEAK"           # PASS / WEAK / FAIL
    rs: float = 0.0                     # 相对强度
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OrderEvent:
    """订单事件：由信号或止损逻辑生成的挂单。"""
    symbol: str
    name: str
    side: str                           # "BUY" / "SELL"
    qty: int                            # 股数
    order_type: str                     # "MARKET" / "STOP"
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    created_at: str = ""                # ISO date: 挂单日
    source_signal_date: str = ""        # 对应信号日
    reason: str = ""                    # 开仓/止损/清仓原因
    force_exit: bool = False            # True = 触发后次日必须尽力卖出
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FillEvent:
    """成交事件：订单撮合后的结果。"""
    symbol: str
    name: str
    side: str                           # "BUY" / "SELL"
    fill_price: float
    qty: int
    fill_date: str                      # ISO date: 成交日
    fees: float = 0.0                   # 佣金
    tax: float = 0.0                    # 印花税
    slippage: float = 0.0               # 滑点金额
    order_date: str = ""                # 订单提交日
    signal_date: str = ""               # 信号产生日
    reason: str = ""
    fail_reason: str = ""               # 成交失败原因: "LIMIT_UP" / "LIMIT_DOWN" / "SUSPENDED"
