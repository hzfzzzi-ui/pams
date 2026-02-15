"""币安 Binance 加密货币持仓截图解析"""
import re
import logging
from typing import Optional

from .base import BaseBrokerParser, TradeData

logger = logging.getLogger(__name__)

# 常见加密货币 symbol → CoinGecko ID 映射
CRYPTO_IDS = {
    'BTC': 'bitcoin',
    'ETH': 'ethereum',
    'SOL': 'solana',
    'BNB': 'binancecoin',
    'XRP': 'ripple',
    'ADA': 'cardano',
    'DOGE': 'dogecoin',
    'DOT': 'polkadot',
    'AVAX': 'avalanche-2',
    'MATIC': 'matic-network',
    'LINK': 'chainlink',
    'UNI': 'uniswap',
}


class BinanceParser(BaseBrokerParser):
    broker_code = 'binance'

    def parse(self, text: str) -> Optional[TradeData]:
        """
        解析币安持仓截图 OCR 文本。

        典型模式：
        - 币种名称（BTC, ETH 等大写缩写）
        - 数量 / 可用
        - 均价 / 成本
        - 当前价格
        - PNL / 盈亏
        """
        # 提取加密货币 symbol
        symbol = None
        name = None

        # 查找已知的 crypto ticker
        text_upper = text.upper()
        for crypto, cg_id in CRYPTO_IDS.items():
            if crypto in text_upper:
                symbol = cg_id
                name = crypto
                break

        # 通用模式：BTC/USDT 或 ETH/USDT
        if not symbol:
            m = re.search(r'([A-Z]{2,6})\s*/?\s*USDT', text_upper)
            if m:
                ticker = m.group(1)
                symbol = CRYPTO_IDS.get(ticker, ticker.lower())
                name = ticker

        if not name:
            logger.warning("Binance: 未找到加密货币名称")
            return None

        quantity = self._extract_number(text, r'(?:数量|可用|Amount|Qty|持仓)[：:\s]*([0-9,.]+)')
        buy_price = self._extract_number(text, r'(?:均价|成本|Avg|Cost)[：:\s]*\$?([0-9,.]+)')
        current_price = self._extract_number(text, r'(?:现价|当前|Price|Last)[：:\s]*\$?([0-9,.]+)')
        market_value = self._extract_number(text, r'(?:市值|总额|Value|Total)[：:\s]*\$?([0-9,.]+)')

        if quantity is None:
            logger.warning("Binance: 未找到持仓数量")
            return None

        return TradeData(
            account_code='binance',
            symbol=symbol,
            name=name,
            quantity=quantity,
            buy_price=buy_price,
            current_price=current_price,
            market_value=market_value,
            currency='USD',
            major_category='加密',
            minor_category='ETH',
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
