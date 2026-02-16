"""
Pydantic 数据模型 - 策略系统请求/响应 schema。
"""
from typing import List, Optional

from pydantic import BaseModel


class OHLCVBar(BaseModel):
    """OHLCV 日线数据。"""
    symbol: str
    trade_date: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    turnover: float = 0.0


class TechnicalSnapshot(BaseModel):
    """技术指标快照。"""
    close: float
    open: float
    low: float
    ma20: float
    ma60: float
    atr14: float
    high_20d: float
    low_20d: float
    max_return_40d: float
    avg_vol_20d: float


class CandidateResult(BaseModel):
    """候选标的扫描结果。"""
    symbol: str
    name: str = ""
    market: str = "CN_A"
    scan_date: str
    stage: str
    close: float
    ma20: float
    ma60: float
    atr14: float
    high_20d: float
    low_20d: float = 0.0
    max_return_40d: float
    flow_result: Optional[str] = None
    flow_score: Optional[float] = None
    confirm_score: Optional[float] = None
    eligible: bool = True
    hard_veto_hit: bool = False
    entry_price: Optional[float] = None
    stop_price: Optional[float] = None
    valid_until: Optional[str] = None
    position_size: Optional[int] = None
    risk_amount_cny: Optional[float] = None
    reason_codes: List[str] = []


class OrderDraftCreate(BaseModel):
    """手动创建订单草稿。"""
    candidate_id: int
    symbol: str
    risk_mode: str = "defensive"
    entry_price: float
    stop_price: float
    valid_until: str
    position_size: int


class OrderAction(BaseModel):
    """订单操作（确认/取消）。"""
    action: str  # confirm / cancel


class OrderExecute(BaseModel):
    """订单执行（可选录入实际成交价和数量）。"""
    actual_price: Optional[float] = None
    actual_size: Optional[int] = None


class BlacklistAdd(BaseModel):
    """添加黑名单。"""
    symbol: str
    market: str = "CN_A"
    reason: str
    expires_at: Optional[str] = None


class StopLossRecord(BaseModel):
    """记录止损。"""
    stop_date: Optional[str] = None  # 默认今天


class ScanSummary(BaseModel):
    """扫描摘要。"""
    scan_date: str
    total_scanned: int
    universe_pass: int
    trend_pass: int
    pullback_zone: int
    triggered: int
    orders_created: int
    candidates: List[CandidateResult] = []
