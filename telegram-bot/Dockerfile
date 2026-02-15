FROM python:3.11-slim

LABEL maintainer="PAMS Project"
LABEL description="PAMS Telegram Bot - 收支记账 + 交易识别"

WORKDIR /app

# 安装 Tesseract OCR + 中英文语言包
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-chi-sim tesseract-ocr-eng && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制代码
COPY bot.py .
COPY ocr/ ./ocr/

# 启动
CMD ["python", "-u", "bot.py"]
