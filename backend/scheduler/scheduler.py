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


def refresh_trend_state():
    """每日刷新宏观趋势状态 (SPX 10月MA)"""
    _call("strategy/state/refresh", "宏观趋势")


def sentiment_refresh():
    """EOD 扫描前刷新市场情绪"""
    _call("strategy/sentiment/refresh", "情绪刷新")


def intraday_refresh():
    """盘中 15 分钟刷新候选状态"""
    _call("strategy/intraday/refresh", "盘中刷新")


def cooldowns_expire():
    """过期冷却期"""
    _call("strategy/cooldowns/expire", "冷却期过期")


def watchlist_daily_exit_check():
    """每日退出检查 (15:32 CST = 07:32 UTC)"""
    _call("strategy/watchlist/exit-check", "观察池退出检查")


def watchlist_weekly_scan():
    """每周五收盘后扫描新候选入池 (15:35 CST = 07:35 UTC)"""
    logger.info("=== 开始观察池周度扫描 ===")
    _call("strategy/watchlist/scan", "观察池扫描")
    _call("strategy/watchlist/exit-check", "观察池退出检查")
    logger.info("=== 观察池周度扫描完成 ===")


def strategy_eod_scan():
    """A股收盘后运行 EOD 策略流水线 (15:30 CST = 07:30 UTC)"""
    logger.info("=== 开始 A股 EOD 策略流水线 ===")
    _call("strategy/risk-snapshot/refresh", "风控快照")
    _call("strategy/sentiment/refresh", "情绪刷新")
    _call("strategy/cooldowns/expire", "冷却期过期")
    _call("strategy/scan/eod", "A股EOD扫描")
    _call("strategy/orders/expire", "过期订单")
    logger.info("=== A股 EOD 策略流水线完成 ===")


def main():
    logger.info("PAMS Scheduler 启动")
    logger.info("API地址: %s", API_BASE)
    logger.info(
        "调度规则: 每天14:30 UTC 刷新汇率+股票, 每30分钟刷新加密, "
        "每天07:25 UTC 宏观趋势, 每天07:30 UTC A股策略流水线, "
        "每天07:32 UTC 观察池退出检查, 每周五07:35 UTC 观察池扫描, "
        "盘中 01:45-03:15/05:15-06:45 UTC 每15分钟刷新"
    )

    schedule.every().day.at("14:30").do(refresh_daily)
    schedule.every(30).minutes.do(refresh_crypto)
    schedule.every().day.at("07:25").do(refresh_trend_state)
    schedule.every().day.at("07:30").do(strategy_eod_scan)
    schedule.every().day.at("07:32").do(watchlist_daily_exit_check)
    schedule.every().friday.at("07:35").do(watchlist_weekly_scan)

    # 盘中刷新: A股交易时段 (CST → UTC)
    # 上午: 09:45-11:15 CST = 01:45-03:15 UTC (每15分钟)
    for t in ["01:45", "02:00", "02:15", "02:30", "02:45", "03:00", "03:15"]:
        schedule.every().day.at(t).do(intraday_refresh)
    # 下午: 13:15-14:45 CST = 05:15-06:45 UTC (每15分钟)
    for t in ["05:15", "05:30", "05:45", "06:00", "06:15", "06:30", "06:45"]:
        schedule.every().day.at(t).do(intraday_refresh)

    # 等待API就绪后执行首次刷新
    logger.info("等待API就绪 (10秒)...")
    time.sleep(10)
    refresh_crypto()

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
