"""Tesseract OCR 封装"""
import io
import logging

import pytesseract
from PIL import Image

logger = logging.getLogger(__name__)


def extract_text(image_bytes: bytes, lang: str = 'chi_sim+eng') -> str:
    """对图片执行 OCR，返回识别文本"""
    try:
        image = Image.open(io.BytesIO(image_bytes))
        text = pytesseract.image_to_string(image, lang=lang)
        logger.info("OCR extracted %d chars", len(text))
        return text
    except Exception as e:
        logger.error("OCR failed: %s", e)
        return ""
