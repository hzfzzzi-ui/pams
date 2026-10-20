"""
PAMS scheduler.

Key investment refresh windows:
- Asia-Pacific post-close: refresh A-share, Korea close, A-share fund estimates,
  Hong Kong estimates, US overnight estimates, precious metals, then publish
  return-series snapshots.
- North America post-close: sync IBKR Flex, refresh confirmed fund/HK/US prices,
  then publish confirmed return-series snapshots.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
import schedule


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("scheduler")

API_BASE = os.environ.get("PAMS_API_BASE", "http://pams-api:8000/api")
APP_BASE = os.environ.get("PAMS_APP_BASE") or (
    API_BASE[: -len("/api")] if API_BASE.rstrip("/").endswith("/api") else API_BASE
)
REQUEST_TIMEOUT = 60
LEGACY_STRATEGY_JOBS_ENV = "PAMS_LEGACY_STRATEGY_JOBS_ENABLED"
ASIA_PACIFIC_REFRESH_TIME = os.environ.get("PAMS_ASIA_PACIFIC_REFRESH_TIME", "15:05")
ASIA_PACIFIC_REFRESH_TZ = os.environ.get("PAMS_ASIA_PACIFIC_REFRESH_TZ", "Asia/Shanghai")
NORTH_AMERICA_REFRESH_TIME = os.environ.get("PAMS_NORTH_AMERICA_REFRESH_TIME", "16:30")
NORTH_AMERICA_REFRESH_TZ = os.environ.get("PAMS_NORTH_AMERICA_REFRESH_TZ", "America/New_York")
INVESTMENT_RETURN_START = os.environ.get("PAMS_INVESTMENT_RETURN_START", "2026-01-01")


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def legacy_strategy_jobs_enabled() -> bool:
    """Production default is false: live advice must come from package ingestion."""
    return _env_bool(LEGACY_STRATEGY_JOBS_ENV, False)


def _call(endpoint: str, label: str):
    """Call an /api write endpoint."""
    url = f"{API_BASE}/{endpoint}"
    try:
        resp = requests.post(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        logger.info("[%s] success: %s", label, data)
        return data
    except requests.RequestException as exc:
        logger.error("[%s] failed: %s", label, exc)
        return None


def _get(endpoint: str, label: str):
    """Call a root-level read endpoint."""
    url = f"{APP_BASE}/{endpoint}"
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        logger.info("[%s] success: %s", label, data)
        return data
    except requests.RequestException as exc:
        logger.error("[%s] failed: %s", label, exc)
        return None


def _post_root(endpoint: str, label: str):
    """Call a root-level write endpoint."""
    url = f"{APP_BASE}/{endpoint}"
    try:
        resp = requests.post(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        logger.info("[%s] success: %s", label, data)
        return data
    except requests.RequestException as exc:
        logger.error("[%s] failed: %s", label, exc)
        return None


def _call_legacy_strategy(endpoint: str, label: str):
    if not legacy_strategy_jobs_enabled():
        logger.warning("[%s] skipped: %s=false", label, LEGACY_STRATEGY_JOBS_ENV)
        return {"skipped": True, "reason": "legacy_strategy_jobs_disabled", "endpoint": endpoint}
    return _call(endpoint, label)


def sync_ibkr_flex():
    """Run the IBKR Flex sync when API credentials are configured."""
    try:
        resp = requests.get(f"{API_BASE}/ibkr/flex/status", timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        status = resp.json()
    except requests.RequestException as exc:
        logger.error("[IBKR Flex] status check failed: %s", exc)
        return None

    if not status.get("configured"):
        logger.warning("[IBKR Flex] skipped: missing API-side Flex token/query id")
        return {"skipped": True, "reason": "ibkr_flex_not_configured"}

    try:
        resp = requests.post(
            f"{API_BASE}/ibkr/flex/sync",
            json={"align_to_latest_snapshot": True},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        logger.info("[IBKR Flex] success: %s", data)
        return data
    except requests.RequestException as exc:
        logger.error("[IBKR Flex] failed: %s", exc)
        return None


def refresh_daily():
    """Backward-compatible daily refresh entrypoint."""
    refresh_north_america_post_close_investments()


def refresh_fund_nav_estimates(use_intraday_estimate: bool | None = None):
    """Refresh A-share fund NAVs with an explicit estimate/confirmed mode."""
    endpoint = "prices/refresh/fund-nav-estimates"
    if use_intraday_estimate is not None:
        endpoint += f"?useIntradayEstimate={'true' if use_intraday_estimate else 'false'}"
    label = (
        "fund NAV estimate"
        if use_intraday_estimate is True
        else "fund NAV confirmed"
        if use_intraday_estimate is False
        else "fund NAV"
    )
    return _call(endpoint, label)


def refresh_investment_return_snapshots(sections: tuple[str, ...]):
    """Rebuild section and total return snapshots after market data refresh."""
    for section in sections:
        if section == "total":
            endpoint = (
                "investment/return-series/snapshot"
                f"?start={INVESTMENT_RETURN_START}&interval=daily&baseCurrency=CNY"
            )
            _post_root(endpoint, "total return snapshot")
        else:
            endpoint = (
                "investment/section-return-series/snapshot"
                f"?section={section}&start={INVESTMENT_RETURN_START}"
                "&interval=daily&baseCurrency=CNY"
            )
            _post_root(endpoint, f"{section} return snapshot")


def refresh_post_close_investments():
    """Asia-Pacific post-close investment refresh at 15:05 Beijing time."""
    logger.info("=== Asia-Pacific post-close investment refresh started ===")
    _call("fx-rates/refresh", "FX rates")
    sync_ibkr_flex()
    _call("prices/refresh/a-stock", "A-share close")
    _call("prices/refresh/us-stock?market=kr&priceBasis=close", "Korea close")
    refresh_fund_nav_estimates(use_intraday_estimate=True)
    _call("prices/refresh/us-stock?market=hk&priceBasis=estimate", "Hong Kong estimate")
    _call("prices/refresh/us-stock?market=us&priceBasis=estimate", "US overnight estimate")
    _call("prices/refresh/precious-metal", "Precious metals")
    refresh_investment_return_snapshots(("a_stock", "a_fund", "us_stock", "precious_metal", "total"))
    logger.info("=== Asia-Pacific post-close investment refresh finished ===")


def refresh_north_america_post_close_investments():
    """North America post-close refresh in New York market time."""
    logger.info("=== North America post-close investment refresh started ===")
    _call("fx-rates/refresh", "FX rates")
    sync_ibkr_flex()
    refresh_fund_nav_estimates(use_intraday_estimate=False)
    _call("prices/refresh/us-stock?market=hk&priceBasis=close", "Hong Kong confirmed close")
    _call("prices/refresh/us-stock?market=us&priceBasis=close", "US confirmed close")
    refresh_investment_return_snapshots(("a_fund", "us_stock", "precious_metal", "total"))
    logger.info("=== North America post-close investment refresh finished ===")


def refresh_crypto():
    """Refresh crypto prices."""
    return _call("prices/refresh/crypto", "Crypto")


def refresh_trend_state():
    """Refresh legacy macro trend state only when legacy jobs are enabled."""
    return _call_legacy_strategy("strategy/state/refresh", "Macro trend")


def sentiment_refresh():
    """Refresh legacy market sentiment only when legacy jobs are enabled."""
    return _call_legacy_strategy("strategy/sentiment/refresh", "Sentiment")


def intraday_refresh():
    """Refresh legacy intraday candidates only when legacy jobs are enabled."""
    return _call_legacy_strategy("strategy/intraday/refresh", "Intraday")


def cooldowns_expire():
    """Expire legacy cooldowns only when legacy jobs are enabled."""
    return _call_legacy_strategy("strategy/cooldowns/expire", "Cooldowns")


def watchlist_daily_exit_check():
    """Run legacy watchlist exit check only when legacy jobs are enabled."""
    return _call_legacy_strategy("strategy/watchlist/exit-check", "Watchlist exit")


def watchlist_weekly_scan():
    """Run legacy weekly watchlist scan only when legacy jobs are enabled."""
    logger.info("=== Legacy watchlist weekly scan started ===")
    result = _call_legacy_strategy("strategy/watchlist/scan", "Watchlist scan")
    _call_legacy_strategy("strategy/watchlist/exit-check", "Watchlist exit")
    logger.info("=== Legacy watchlist weekly scan finished ===")
    return result


def strategy_eod_scan():
    """Run legacy A-share EOD strategy pipeline only when explicitly enabled."""
    logger.info("=== Legacy A-share EOD pipeline started ===")
    _call_legacy_strategy("strategy/risk-snapshot/refresh", "Risk snapshot")
    _call_legacy_strategy("strategy/sentiment/refresh", "Sentiment")
    _call_legacy_strategy("strategy/cooldowns/expire", "Cooldowns")
    result = _call_legacy_strategy("strategy/scan/eod", "A-share EOD scan")
    _call_legacy_strategy("strategy/orders/expire", "Order expiry")
    logger.info("=== Legacy A-share EOD pipeline finished ===")
    return result


def _should_catch_up_local(
    *,
    local_time: str,
    timezone_name: str,
    now_utc: datetime | None = None,
) -> bool:
    """Return whether today's local cutoff has passed."""
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local_now = now.astimezone(ZoneInfo(timezone_name))
    hour, minute = [int(part) for part in local_time.split(":", 1)]
    cutoff = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return local_now >= cutoff


def should_catch_up_post_close_investments(now_utc: datetime | None = None) -> bool:
    return _should_catch_up_local(
        local_time=ASIA_PACIFIC_REFRESH_TIME,
        timezone_name=ASIA_PACIFIC_REFRESH_TZ,
        now_utc=now_utc,
    )


def should_catch_up_north_america_investments(now_utc: datetime | None = None) -> bool:
    return _should_catch_up_local(
        local_time=NORTH_AMERICA_REFRESH_TIME,
        timezone_name=NORTH_AMERICA_REFRESH_TZ,
        now_utc=now_utc,
    )


def main():
    logger.info("PAMS Scheduler started")
    logger.info("API base: %s", API_BASE)
    logger.info("App base: %s", APP_BASE)
    logger.info(
        "Investment refresh: Asia-Pacific %s %s, North America %s %s",
        ASIA_PACIFIC_REFRESH_TIME,
        ASIA_PACIFIC_REFRESH_TZ,
        NORTH_AMERICA_REFRESH_TIME,
        NORTH_AMERICA_REFRESH_TZ,
    )

    schedule.every().day.at(ASIA_PACIFIC_REFRESH_TIME, ASIA_PACIFIC_REFRESH_TZ).do(refresh_post_close_investments)
    schedule.every().day.at(NORTH_AMERICA_REFRESH_TIME, NORTH_AMERICA_REFRESH_TZ).do(
        refresh_north_america_post_close_investments
    )
    schedule.every(30).minutes.do(refresh_crypto)
    schedule.every().day.at("07:25").do(refresh_trend_state)
    schedule.every().day.at("07:30").do(strategy_eod_scan)
    schedule.every().day.at("07:32").do(watchlist_daily_exit_check)
    schedule.every().friday.at("07:35").do(watchlist_weekly_scan)

    for refresh_time in ["01:45", "02:00", "02:15", "02:30", "02:45", "03:00", "03:15"]:
        schedule.every().day.at(refresh_time).do(intraday_refresh)
    for refresh_time in ["05:15", "05:30", "05:45", "06:00", "06:15", "06:30", "06:45"]:
        schedule.every().day.at(refresh_time).do(intraday_refresh)

    logger.info("Waiting for API readiness (10 seconds)...")
    time.sleep(10)
    refresh_crypto()
    if should_catch_up_post_close_investments():
        refresh_post_close_investments()
    if should_catch_up_north_america_investments():
        refresh_north_america_post_close_investments()

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
