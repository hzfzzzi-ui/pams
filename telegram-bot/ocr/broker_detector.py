"""券商识别 — 基于关键词评分"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# 每个券商的特征关键词（出现 ≥2 个则认为匹配）
BROKER_SIGNATURES = {
    'tonghuashun': ['同花顺', '10jqka', '成本价', '持仓数量', '可用数量', '盈亏', '持仓股'],
    'guangfa':     ['广发', '广发证券', 'gf.com', '持仓', '成本', '广发金管家'],
    'ibkr':        ['Interactive Brokers', 'IBKR', 'Portfolio', 'Market Value',
                    'Position', 'Average Cost', 'Unrealized'],
    'binance':     ['Binance', '币安', '现货', 'Spot', 'USDT', 'PNL', '合约'],
}


def detect_broker(text: str) -> Optional[str]:
    """
    对 OCR 文本中的关键词计分，返回得分最高且 ≥2 的券商 code。
    返回 None 表示无法识别。
    """
    scores = {}
    text_lower = text.lower()

    for broker, keywords in BROKER_SIGNATURES.items():
        score = sum(1 for kw in keywords if kw.lower() in text_lower)
        if score >= 2:
            scores[broker] = score

    if not scores:
        logger.info("No broker detected from OCR text")
        return None

    best = max(scores, key=scores.get)
    logger.info("Detected broker: %s (score=%d)", best, scores[best])
    return best
