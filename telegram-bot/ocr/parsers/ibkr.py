"""IBKR (盈透证券) 美股持仓截图解析"""
import re
import logging
from typing import Optional

from .base import BaseBrokerParser, TradeData

logger = logging.getLogger(__name__)


class IbkrParser(BaseBrokerParser):
    broker_code = 'ibkr'

    def parse(self, text: str) -> Optional[TradeData]:
        """
        解析 IBKR 持仓截图 OCR 文本。

        典型模式（英文为主）：
        - Ticker symbol (大写字母 1-5 个字符)
        - Position / Quantity
        - Market Value
        - Average Cost / Avg Price
        - Unrealized P&L
        """
        # 提取美股 ticker（1-5个大写字母）
        symbol = None
        name = None

        # 模式: 寻找看起来像 ticker 的大写字母序列
        tickers = re.findall(r'\b([A-Z]{1,5})\b', text)
        # 过滤掉常见非 ticker 词
        skip_words = {'IBKR', 'USD', 'EUR', 'GBP', 'MXN', 'CNY', 'PNL', 'AVG',
                      'NET', 'THE', 'FOR', 'AND', 'ALL', 'BUY', 'SELL', 'ETF',
                      'PORTFOLIO', 'POSITION', 'MARKET', 'VALUE', 'TOTAL', 'COST'}
        tickers = [t for t in tickers if t not in skip_words and len(t) >= 2]

        if tickers:
            symbol = tickers[0]
            name = symbol  # IBKR 截图中名称通常就是 ticker

        if not symbol:
            logger.warning("IBKR: 未找到 ticker symbol")
            return None

        quantity = self._extract_number(text, r'(?:Position|Quantity|Shares?|Qty)[:\s]*([0-9,.]+)')
        buy_price = self._extract_number(text, r'(?:Average\s*Cost|Avg\s*Price|Cost\s*Basis)[:\s]*\$?([0-9,.]+)')
        current_price = self._extract_number(text, r'(?:Last|Price|Current)[:\s]*\$?([0-9,.]+)')
        market_value = self._extract_number(text, r'(?:Market\s*Value|Mkt\s*Val)[:\s]*\$?([0-9,.]+)')

        # 备选：从纯数字行提取
        if quantity is None:
            nums = re.findall(r'(?<!\d)(\d{1,6}(?:\.\d+)?)(?!\d)', text)
            if len(nums) >= 1:
                try:
                    quantity = float(nums[0])
                except ValueError:
                    pass

        if quantity is None:
            logger.warning("IBKR: 未找到持仓数量")
            return None

        return TradeData(
            account_code='ibkr',
            symbol=symbol,
            name=name,
            quantity=quantity,
            buy_price=buy_price,
            current_price=current_price,
            market_value=market_value,
            currency='USD',
            major_category='美股',
            minor_category='美股基金',
        )

    @staticmethod
    def _extract_number(text: str, pattern: str) -> Optional[float]:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            try:
                return float(m.group(1).replace(',', ''))
            except ValueError:
                pass
        return None
