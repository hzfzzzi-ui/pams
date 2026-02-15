"""广发证券 A股持仓截图解析"""
import re
import logging
from typing import Optional

from .base import BaseBrokerParser, TradeData

logger = logging.getLogger(__name__)


class GuangfaParser(BaseBrokerParser):
    broker_code = 'guangfa'

    def parse(self, text: str) -> Optional[TradeData]:
        """
        解析广发证券持仓截图 OCR 文本。
        布局类似同花顺，但关键词可能略有差异。
        """
        name = None
        symbol = None

        # 提取股票名称和代码
        m = re.search(r'([\u4e00-\u9fa5]{2,8})\s*(\d{6})', text)
        if m:
            name = m.group(1)
            code = m.group(2)
            symbol = ('sh' if code.startswith(('6', '5')) else 'sz') + code

        if not name:
            m = re.search(r'(\d{6})\s*([\u4e00-\u9fa5]{2,8})', text)
            if m:
                code = m.group(1)
                name = m.group(2)
                symbol = ('sh' if code.startswith(('6', '5')) else 'sz') + code

        if not name:
            logger.warning("GF: 未找到股票名称")
            return None

        quantity = self._extract_number(text, r'(?:持仓|可用)[数量]*[：:\s]*([0-9,.]+)')
        buy_price = self._extract_number(text, r'(?:成本|均价)[价]*[：:\s]*([0-9,.]+)')
        current_price = self._extract_number(text, r'(?:现价|最新|当前)[价]*[：:\s]*([0-9,.]+)')
        market_value = self._extract_number(text, r'(?:市值|持仓市值)[：:\s]*([0-9,.]+)')

        if quantity is None:
            logger.warning("GF: 未找到持仓数量")
            return None

        return TradeData(
            account_code='guangfa',
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
