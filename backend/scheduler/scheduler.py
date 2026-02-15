"""
PAMS 定时调度器 - 定期调用API刷新价格和汇率

调度规则:
- 每天 14:30 UTC (8:30 UTC-6): 刷新汇率 + A股 + 美股
- 每 30 分钟: 刷新加密货币价格
"""
import os
import time
import logging

import schedule
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("scheduler")

API_BASE = os.environ.get("PAMS_API_BASE", "http://pams-api:8000/api")
REQUEST_TIMEOUT = 60


def _call(endpoint: str, label: str):
    """调用API端点并记录结果"""
    url = f"{API_BASE}/{endpoint}"
    try:
        resp = requests.post(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        logger.info("[%s] 成功: %s", label, data)
    except requests.RequestException as e:
        logger.error("[%s] 失败: %s", label, e)


def refresh_daily():
    """每天执行: 汇率 + A股 + 美股 + 贵金属"""
    logger.info("=== 开始每日刷新 ===")
    _call("fx-rates/refresh", "汇率")
    _call("prices/refresh/a-stock", "A股")
    _call("prices/refresh/us-stock", "美股")
    _call("prices/refresh/precious-metal", "贵金属")
    logger.info("=== 每日刷新完成 ===")


def refresh_crypto():
    """每30分钟执行: 加密货币"""
    _call("prices/refresh/crypto", "加密")


def main():
    logger.info("PAMS Scheduler 启动")
    logger.info("API地址: %s", API_BASE)
    logger.info("调度规则: 每天14:30 UTC 刷新汇率+股票, 每30分钟刷新加密")

    schedule.every().day.at("14:30").do(refresh_daily)
    schedule.every(30).minutes.do(refresh_crypto)

    # 等待API就绪后执行首次刷新
    logger.info("等待API就绪 (10秒)...")
    time.sleep(10)
    refresh_crypto()

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
