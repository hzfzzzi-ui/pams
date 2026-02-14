FROM python:3.11-slim

LABEL maintainer="PAMS Project"
LABEL description="PAMS Telegram Bot - 收支记账助手"

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制代码
COPY bot.py .

# 启动
CMD ["python", "-u", "bot.py"]
