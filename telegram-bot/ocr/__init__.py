"""
OCR 交易识别管线

用法：
    from ocr import recognize_trade
    trade_data = recognize_trade(image_bytes)
"""
import logging

from .engine import extract_text
from .broker_detector import detect_broker
from .parsers.base import TradeData
from .parsers.tonghuashun import TonghuashunParser
from .parsers.guangfa import GuangfaParser
from .parsers.ibkr import IbkrParser
from .parsers.binance_parser import BinanceParser

logger = logging.getLogger(__name__)

PARSERS = {
    'tonghuashun': TonghuashunParser(),
    'guangfa':     GuangfaParser(),
    'ibkr':        IbkrParser(),
    'binance':     BinanceParser(),
}


def recognize_trade(image_bytes: bytes) -> TradeData | None:
    """
    完整 OCR 管线：图片 → 文字 → 券商识别 → 字段解析 → TradeData
    识别失败返回 None。
    """
    text = extract_text(image_bytes)
    if not text.strip():
        logger.warning("OCR returned empty text")
        return None

    broker = detect_broker(text)
    if not broker or broker not in PARSERS:
        logger.warning("Unknown broker or detection failed")
        return None

    result = PARSERS[broker].parse(text)
    if result:
        logger.info("Trade recognized: %s %s qty=%.2f", result.account_code, result.name, result.quantity)
    else:
        logger.warning("Parser %s failed to extract trade data", broker)

    return result


def get_ocr_text(image_bytes: bytes) -> str:
    """仅返回 OCR 文本（用于调试）"""
    return extract_text(image_bytes)
