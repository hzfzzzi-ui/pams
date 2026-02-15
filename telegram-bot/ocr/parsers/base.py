"""OCR 解析基类和数据模型"""
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class TradeData:
    """从截图/文字识别出的交易数据"""
    account_code: str
    symbol: Optional[str]
    name: str
    quantity: float
    buy_price: Optional[float]
    current_price: Optional[float]
    market_value: Optional[float]
    currency: str
    major_category: str
    minor_category: str


class BaseBrokerParser:
    """券商截图解析基类"""
    broker_code: str = ''

    def parse(self, text: str) -> Optional[TradeData]:
        """解析 OCR 文本，返回 TradeData 或 None"""
        raise NotImplementedError
