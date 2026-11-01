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
import re
import time
from datetime import UTC, date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import schedule
from refresh_state import RefreshJournal, job_lock, latest_due_date, read_state, result_issues, store_job
from flex_confirmation import FlexConfirmationJournal


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
ASIA_PACIFIC_REFRESH_TIME = os.environ.get("PAMS_ASIA_PACIFIC_REFRESH_TIME", "15:05")
ASIA_PACIFIC_REFRESH_TZ = os.environ.get("PAMS_ASIA_PACIFIC_REFRESH_TZ", "Asia/Shanghai")
NORTH_AMERICA_REFRESH_TIME = os.environ.get("PAMS_NORTH_AMERICA_REFRESH_TIME", "16:30")
NORTH_AMERICA_REFRESH_TZ = os.environ.get("PAMS_NORTH_AMERICA_REFRESH_TZ", "America/New_York")
INVESTMENT_RETURN_START = os.environ.get("PAMS_INVESTMENT_RETURN_START", "2026-01-01")
STATE_PATH = Path(os.environ.get("PAMS_REFRESH_STATE_PATH", "/state/refresh.json"))


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}




def _call(endpoint: str, label: str):
    """Call an /api write endpoint."""
    url = f"{API_BASE}/{endpoint}"
    return _request_json("post", url, label)


def _retry_after(value):
    try:
        text = str(value or "").strip()
        if text.isdigit():
            return (datetime.now(timezone.utc) + timedelta(seconds=int(text))).isoformat()
        parsed = parsedate_to_datetime(text)
        return parsed.astimezone(timezone.utc).isoformat() if parsed.tzinfo else None
    except (ValueError, TypeError, OverflowError):
        return None


def _request_json(method: str, url: str, label: str, *, failure_details=False, max_attempts=3, **kwargs):
    if os.environ.get("PAMS_SOURCE_MODE", "").strip().lower() == "development":
        logger.warning("Scheduler network calls are disabled in development mode")
        return {"success": False, "status": "disabled", "reason": "development_mode"} if failure_details else None
    failure = None
    for attempt in range(max_attempts):
        try:
            resp = getattr(requests, method)(url, timeout=REQUEST_TIMEOUT, **kwargs)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                raise ValueError("expected object response")
            logger.info("[%s] response received", label)
            return data
        except (requests.RequestException, ValueError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if failure_details:
                headers = getattr(getattr(exc, "response", None), "headers", {}) or {}
                failure = {"success": False, "status": "error", "http_status": status,
                           "retry_after": _retry_after(headers.get("Retry-After"))}
            logger.warning("[%s] attempt=%s failed=%s status=%s", label, attempt + 1, type(exc).__name__, status)
            if status and 400 <= status < 500 and status != 429:
                break
            if attempt < max_attempts - 1:
                time.sleep(attempt + 1)
    return failure


def _get(endpoint: str, label: str):
    """Call a root-level read endpoint."""
    if os.environ.get("PAMS_SOURCE_MODE", "").strip().lower() == "development":
        logger.warning("Scheduler network calls are disabled in development mode")
        return None
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
    return _request_json("post", url, label)




def sync_ibkr_flex(expected_reference=None):
    """Run the IBKR Flex sync when API credentials are configured."""
    status = _flex_status()
    if not status:
        return None
    if status.get("success") is False:
        return status

    if not status.get("configured"):
        logger.warning("[IBKR Flex] skipped: missing API-side Flex token/query id")
        return {"skipped": True, "reason": "ibkr_flex_not_configured"}

    return _request_json(
        "post", f"{API_BASE}/ibkr/flex/sync", "IBKR Flex",
        json={"align_to_latest_snapshot": False, **(
            {"expected_statement_reference": expected_reference.isoformat()} if expected_reference else {}
        )},
        failure_details=True, max_attempts=1,
    )


def _flex_status():
    return _request_json("get", f"{API_BASE}/ibkr/flex/status", "IBKR Flex status",
                         failure_details=True, max_attempts=1)


def run_flex_confirmation(now=None):
    """This task may acquire an official report after the quote window has expired."""
    try:
        result = FlexConfirmationJournal(STATE_PATH).run(_flex_status, sync_ibkr_flex, now=now)
        logger.info("[Flex confirmation] status=%s reason=%s next_retry=%s",
                    result.get("status"), result.get("reason"), result.get("next_retry_at"))
        return result
    except (OSError, ValueError):
        logger.exception("Flex confirmation checkpoint unavailable; sync blocked")
        return {"success": False, "status": "error", "reason": "checkpoint_unavailable"}


def _daily_close_info(payload):
    quality = payload.get("report_quality") or {}
    target = date.fromisoformat(quality["expected_statement_as_of"]) if quality.get("expected_statement_as_of") else None
    observed = date.fromisoformat(quality["statement_as_of"]) if quality.get("statement_as_of") else None
    verified = bool(payload.get("source") == "ibkr_daily_close" and target and observed == target
                    and payload.get("publication_verified") is True and quality.get("status") == "ok"
                    and re.fullmatch(r"[0-9a-f]{64}", str(payload.get("source_sha256", ""))))
    return target, observed, verified


def run_daily_close_confirmation(now=None):
    """Use the existing durable journal; never invoke ledger or quote writers."""
    if not _env_bool("PAMS_IBKR_DAILY_CLOSE_SYNC_ENABLED"):
        return {"status": "skipped", "reason": "daily_close_sync_disabled"}
    def probe():
        return _request_json("get", f"{API_BASE}/ibkr/daily-close/status", "IBKR close status",
                             failure_details=True, max_attempts=1)
    def sync(target):
        return _request_json("post", f"{API_BASE}/ibkr/daily-close/sync", "IBKR close confirmation",
            json={"expected_statement_reference": target.isoformat()}, failure_details=True, max_attempts=1)
    try:
        result = FlexConfirmationJournal(STATE_PATH, key="ibkr_daily_close", probe_info=_daily_close_info).run(
            probe, sync, now=now)
        logger.info("[Close confirmation] status=%s reason=%s next_retry=%s",
                    result.get("status"), result.get("reason"), result.get("next_retry_at"))
        return result
    except (OSError, ValueError):
        logger.exception("Close confirmation checkpoint unavailable; publication blocked")
        return {"success": False, "status": "error", "reason": "checkpoint_unavailable"}


def _cash_event_info(payload):
    value = payload.get("confirmed_day")
    target = date.fromisoformat(value) if value else None
    blocked_events = any(item.get("reason") != "bank_match_pending" for item in payload.get("pending", []))
    verified = bool(payload.get("status") == "ready" and target
        and re.fullmatch(r"[0-9a-f]{64}", str(payload.get("source_sha256", "")))
        and payload.get("queued_day_count") == 0 and payload.get("retryable_pending_count") == 0
        and not blocked_events)
    return target, target, verified


def run_cash_event_confirmation(now=None):
    """Cash facts retry independently from already confirmed official close evidence."""
    if not _env_bool("PAMS_IBKR_CASH_EVENTS_SYNC_ENABLED"):
        return {"status": "skipped", "reason": "cash_event_sync_disabled"}
    def probe():
        value = _request_json("get", f"{API_BASE}/ibkr/cash-events/status", "IBKR cash fact status",
                              failure_details=True, max_attempts=1)
        if isinstance(value, dict):
            value = {**value, "configured": value.get("status") == "ready"}
        return value
    def sync(_target):
        return _request_json("post", f"{API_BASE}/ibkr/cash-events/sync", "IBKR cash facts",
                             json={}, failure_details=True, max_attempts=1)
    try:
        return FlexConfirmationJournal(STATE_PATH, key="ibkr_cash_events", probe_info=_cash_event_info).run(
            probe, sync, now=now)
    except (OSError, ValueError):
        logger.exception("Cash fact checkpoint unavailable; import blocked")
        return {"success": False, "status": "error", "reason": "checkpoint_unavailable"}


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
            result = _post_root(endpoint, "total return snapshot")
        else:
            endpoint = (
                "investment/section-return-series/snapshot"
                f"?section={section}&start={INVESTMENT_RETURN_START}"
                "&interval=daily&baseCurrency=CNY"
            )
            result = _post_root(endpoint, f"{section} return snapshot")
        if result_issues(result) or int(result.get("written") or 0) <= 0:
            return {"status": "blocked", "errors": [f"snapshot_{section}"]}
    return {"status": "ok"}


def refresh_post_close_investments(target_date=None):
    """Asia-Pacific post-close investment refresh at 15:05 Beijing time."""
    logger.info("=== Asia-Pacific post-close investment refresh started ===")
    target = target_date or latest_due_date(ASIA_PACIFIC_REFRESH_TIME, ASIA_PACIFIC_REFRESH_TZ)
    return _refresh_window(target, [
        ("fx", lambda: _call("fx-rates/refresh", "FX rates")),
        ("flex", run_flex_confirmation),
        ("a_stock", lambda: _call("prices/refresh/a-stock", "A-share close")),
        ("korea", lambda: _call("prices/refresh/us-stock?market=kr&priceBasis=close", "Korea close")),
        ("fund", lambda: refresh_fund_nav_estimates(use_intraday_estimate=True)),
        ("hk", lambda: _call("prices/refresh/us-stock?market=hk&priceBasis=estimate", "Hong Kong estimate")),
        ("us", lambda: _call("prices/refresh/us-stock?market=us&priceBasis=estimate&scheduled=true", "US overnight estimate")),
        ("metal", lambda: _call("prices/refresh/precious-metal", "Precious metals")),
    ], ("a_stock", "a_fund", "us_stock", "precious_metal", "total"))


def refresh_north_america_post_close_investments(target_date=None):
    """North America post-close refresh in New York market time."""
    logger.info("=== North America post-close investment refresh started ===")
    target = target_date or latest_due_date(NORTH_AMERICA_REFRESH_TIME, NORTH_AMERICA_REFRESH_TZ)
    return _refresh_window(target, [
        ("fx", lambda: _call("fx-rates/refresh", "FX rates")),
        ("flex", run_flex_confirmation),
        ("fund", lambda: refresh_fund_nav_estimates(use_intraday_estimate=False)),
        ("hk", lambda: _call("prices/refresh/us-stock?market=hk&priceBasis=close", "Hong Kong confirmed close")),
        ("us", lambda: _call("prices/refresh/us-stock?market=us&priceBasis=close&scheduled=true", "US confirmed close")),
    ], ("a_fund", "us_stock", "precious_metal", "total"))


def _refresh_window(target, steps, sections):
    reports = []
    for name, callback in steps:
        try:
            result = callback()
        except Exception as exc:
            reports.append({"name": name, "issues": [type(exc).__name__]})
            continue
        expected = target
        if name == "flex" and "a_stock" in sections:
            expected -= timedelta(days=1)
            while expected.weekday() >= 5:
                expected -= timedelta(days=1)
        if name == "flex" and isinstance(result, dict) and result.get("expected_statement_as_of"):
            # The API normalizes the reference with the offline exchange calendar before writes.
            try:
                normalized = date.fromisoformat(result["expected_statement_as_of"])
                if normalized > expected:
                    reports.append({"name": name, "issues": ["invalid_expected_date"]})
                    continue
                else:
                    expected = normalized
            except (ValueError, TypeError):
                reports.append({"name": name, "issues": ["invalid_expected_date"]})
                continue
        issues = result_issues(result, target=expected)
        if isinstance(result, dict):
            if name == "fx" and int(result.get("stored") or 0) <= 0:
                issues.append("missing_fx_rows")
            elif name == "flex" and not result.get("statement_as_of"):
                issues.append("missing_statement_date")
            elif name == "fund" and ("processed" not in result or len(result.get("rows") or []) != result["processed"]):
                issues.append("incomplete_fund_response")
            elif name in {"a_stock", "korea", "hk", "us", "metal"} and "found" not in result:
                issues.append("missing_coverage")
        reports.append({"name": name, "issues": issues})
        if issues:
            logger.error("Refresh blocked: step=%s issues=%s", name, issues)
    if any(report["issues"] for report in reports):
        return {"status": "blocked", "steps": reports}
    result = refresh_investment_return_snapshots(sections)
    issues = result_issues(result)
    reports.append({"name": "snapshots", "issues": issues + (result.get("errors", []) if isinstance(result, dict) else [])})
    return {"status": "blocked" if issues else "ok", "steps": reports}


def run_due_window(key, now=None):
    current = now or datetime.now(timezone.utc)
    if key == "asia":
        target = latest_due_date(ASIA_PACIFIC_REFRESH_TIME, ASIA_PACIFIC_REFRESH_TZ, current)
        callback = refresh_post_close_investments
    else:
        target = latest_due_date(NORTH_AMERICA_REFRESH_TIME, NORTH_AMERICA_REFRESH_TZ, current)
        callback = refresh_north_america_post_close_investments
    beijing = current.astimezone(ZoneInfo("Asia/Shanghai"))
    writable_date = beijing.date()
    if key != "asia" and beijing.hour < 7:
        writable_date -= timedelta(days=1)
    if target != writable_date:
        # Current-price endpoints cannot safely write a past trading date.
        callback = lambda _target: {"status": "blocked", "steps": [{"name": "history", "issues": ["historical_backfill_required"]}]}
    try:
        return RefreshJournal(STATE_PATH).run(key, target, callback)
    except (OSError, ValueError):
        logger.exception("Refresh checkpoint unavailable; publication blocked")
        return {"status": "error", "reason": "checkpoint_unavailable"}


def refresh_crypto():
    """Refresh crypto prices."""
    return _call("prices/refresh/crypto", "Crypto")


def refresh_us_quotes(now=None):
    """Poll current US quotes independently; never publish an all-market snapshot."""
    current = now or datetime.now(timezone.utc)
    try:
        with job_lock(STATE_PATH, "us_quotes"):
            previous = read_state(STATE_PATH)["jobs"].get("us_quotes", {})
            checked = previous.get("last_checked_at")
            if checked and current - datetime.fromisoformat(checked) < timedelta(minutes=5):
                return {"status": "deferred", "reason": "quote_poll_not_due"}
            job = {**previous, "status": "running", "last_checked_at": current.isoformat()}
            store_job(STATE_PATH, "us_quotes", job)
            try:
                result = _call("prices/refresh/us-stock?market=us&scheduled=true", "US current quotes")
                if isinstance(result, dict) and result.get("status") == "skipped" and result.get("reason") == "outside_us_quote_session":
                    job.update(status="idle", reason=result["reason"], issues=[])
                elif isinstance(result, dict) and result.get("found") == 0 and not result_issues(result):
                    job.update(status="idle", reason="no_us_holdings", issues=[])
                else:
                    issues = result_issues(result)
                    if not isinstance(result, dict) or "found" not in result or "as_of" not in result:
                        issues.append("missing_quote_coverage")
                    job.update(status="blocked" if issues else "success", issues=issues,
                               reason="quote_refresh_incomplete" if issues else "quotes_refreshed")
                    if isinstance(result, dict):
                        job.update(target_date=result.get("as_of"), found=result.get("found"),
                                   updated=result.get("updated"), quote_sources=result.get("quote_sources", {}))
                    if not issues:
                        job["last_success_date"] = result["as_of"]
            except Exception as exc:
                job.update(status="error", issues=[type(exc).__name__], reason="quote_refresh_failed")
            job["finished_at"] = datetime.now(timezone.utc).isoformat()
            store_job(STATE_PATH, "us_quotes", job)
            logger.info("[US current quotes] status=%s reason=%s", job["status"], job["reason"])
            return job
    except BlockingIOError:
        return {"status": "deferred", "reason": "job_busy"}
    except (OSError, ValueError):
        logger.exception("US quote checkpoint unavailable")
        return {"status": "error", "reason": "checkpoint_unavailable"}


def _refresh_overseas_market_quotes(market, current):
    """Each local market has its own durable lock, polling limit and outcome."""
    key = f"{market}_quotes"
    try:
        with job_lock(STATE_PATH, key):
            previous = read_state(STATE_PATH)["jobs"].get(key, {})
            checked = previous.get("last_checked_at")
            if checked and current - datetime.fromisoformat(checked) < timedelta(minutes=5):
                return {"status": "deferred", "reason": "quote_poll_not_due"}
            job = {**previous, "status": "running", "last_checked_at": current.isoformat(),
                   "covered": None, "unchanged": False}
            store_job(STATE_PATH, key, job)
            try:
                result = _call(f"prices/refresh/us-stock?market={market}&scheduled=true", f"{market.upper()} close quotes")
                if (isinstance(result, dict) and result.get("status") == "skipped"
                        and result.get("reason") == "no_completed_overseas_session"):
                    job.update(status="idle", reason=result["reason"], issues=[])
                elif isinstance(result, dict) and result.get("found") == 0 and not result_issues(result):
                    job.update(status="idle", reason="no_overseas_holdings", issues=[])
                else:
                    issues = result_issues(result)
                    if not isinstance(result, dict) or "found" not in result or not result.get("as_of"):
                        issues.append("missing_quote_coverage")
                    elif result.get("as_of"):
                        target = date.fromisoformat(result["as_of"]).isoformat()
                        job.update(target_date=target, found=result["found"], updated=result.get("updated"), covered=result.get("covered"),
                                   quote_sources=result.get("quote_sources", {}))
                    if isinstance(result, dict) and result.get("unchanged") is True:
                        found, covered, updated = result.get("found"), result.get("covered"), result.get("updated")
                        valid_coverage = (type(found) is int and found > 0 and type(covered) is int
                                          and covered == found and type(updated) is int and updated == 0)
                        if valid_coverage and issues == ["partial_update"]:
                            issues = []
                        elif not valid_coverage:
                            issues.append("invalid_unchanged_coverage")
                        job.update(covered=covered, unchanged=True)
                    job.update(status="blocked" if issues else "success", issues=issues,
                               reason="quote_refresh_incomplete" if issues else "quotes_refreshed")
                    if not issues:
                        job["last_success_date"] = job["target_date"]
            except Exception as exc:  # noqa: BLE001
                job.update(status="error", issues=[type(exc).__name__], reason="quote_refresh_failed")
            job["finished_at"] = datetime.now(UTC).isoformat()
            store_job(STATE_PATH, key, job)
            logger.info("[%s close quotes] status=%s reason=%s", market.upper(), job["status"], job["reason"])
            return job
    except BlockingIOError:
        return {"status": "deferred", "reason": "job_busy"}
    except (OSError, ValueError, TypeError):
        logger.exception("%s quote checkpoint unavailable", market.upper())
        return {"status": "error", "reason": "checkpoint_unavailable"}


def refresh_overseas_quotes(now=None):
    """Quote-only polling does not depend on Flex, funds or all-market publishing."""
    current = now or datetime.now(UTC)
    return {market: _refresh_overseas_market_quotes(market, current) for market in ("kr", "hk", "se", "ch")}
















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
    if os.environ.get("PAMS_SOURCE_MODE", "").strip().lower() == "development":
        logger.warning("Scheduler is disabled in development mode")
        return
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

    schedule.every().day.at(ASIA_PACIFIC_REFRESH_TIME, ASIA_PACIFIC_REFRESH_TZ).do(lambda: run_due_window("asia"))
    schedule.every().day.at(NORTH_AMERICA_REFRESH_TIME, NORTH_AMERICA_REFRESH_TZ).do(
        lambda: run_due_window("north_america")
    )
    schedule.every(30).minutes.do(refresh_crypto)
    schedule.every(5).minutes.do(refresh_us_quotes)
    schedule.every(5).minutes.do(refresh_overseas_quotes)


    logger.info("Waiting for API readiness (10 seconds)...")
    time.sleep(10)
    if _env_bool("PAMS_SCHEDULER_SKIP_STARTUP_REFRESH"):
        logger.info("Startup refresh skipped; scheduled jobs remain enabled")
    else:
        refresh_us_quotes()
        refresh_overseas_quotes()
        refresh_crypto()
        run_flex_confirmation()
        run_daily_close_confirmation()
        run_cash_event_confirmation()
        run_due_window("asia")
        run_due_window("north_america")
    schedule.every(30).minutes.do(lambda: run_due_window("asia"))
    schedule.every(30).minutes.do(lambda: run_due_window("north_america"))
    schedule.every(30).minutes.do(run_flex_confirmation)
    schedule.every(30).minutes.do(run_daily_close_confirmation)
    schedule.every(30).minutes.do(run_cash_event_confirmation)

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
