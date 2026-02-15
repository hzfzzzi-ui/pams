"""同花顺 A股持仓截图解析"""
import re
import logging
from typing import Optional

from .base import BaseBrokerParser, TradeData

logger = logging.getLogger(__name__)


class TonghuashunParser(BaseBrokerParser):
    broker_code = 'tonghuashun'

    def parse(self, text: str) -> Optional[TradeData]:
        """
        解析同花顺持仓截图 OCR 文本。

        典型文本模式：
        - 股票名称 + 6位代码（如 "贵州茅台 600519"）
        - 关键词行：持仓数量、成本价、现价、市值、盈亏
        - 数值紧跟关键词后
        """
        # 提取股票名称和代码
        name = None
        symbol = None

        # 模式1：名称 + 6位代码
        m = re.search(r'([\u4e00-\u9fa5]{2,8})\s*(\d{6})', text)
        if m:
            name = m.group(1)
            code = m.group(2)
            # 推断交易所前缀
            symbol = ('sh' if code.startswith(('6', '5')) else 'sz') + code

        # 模式2：代码 + 名称
        if not name:
            m = re.search(r'(\d{6})\s*([\u4e00-\u9fa5]{2,8})', text)
            if m:
                code = m.group(1)
                name = m.group(2)
                symbol = ('sh' if code.startswith(('6', '5')) else 'sz') + code

        if not name:
            logger.warning("THS: 未找到股票名称")
            return None

        # 提取数值字段
        quantity = self._extract_number(text, r'持仓[数量]*[：:\s]*([0-9,.]+)')
        buy_price = self._extract_number(text, r'成本[价]*[：:\s]*([0-9,.]+)')
        current_price = self._extract_number(text, r'(?:现价|最新[价]*)[：:\s]*([0-9,.]+)')
        market_value = self._extract_number(text, r'(?:市值|持仓市值)[：:\s]*([0-9,.]+)')

        # 如果没找到持仓数量，尝试更宽松的匹配
        if quantity is None:
            quantity = self._extract_number(text, r'(?:可用|持仓)\s*(\d+(?:\.\d+)?)')

        if quantity is None:
            logger.warning("THS: 未找到持仓数量")
            return None

        return TradeData(
            account_code='tonghuashun',
            symbol=symbol,
            name=name,
            quantity=quantity,
            buy_price=buy_price,
            current_price=current_price,
            market_value=market_value,
            currency='CNY',
            major_category='A股',
            minor_category='A股',
        )

    @staticmethod
    def _extract_number(text: str, pattern: str) -> Optional[float]:
        m = re.search(pattern, text)
        if m:
            try:
                return float(m.group(1).replace(',', ''))
            except ValueError:
                pass
        return None
