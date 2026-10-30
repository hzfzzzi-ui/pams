"""Read sync diagnostics without writing portfolio state."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import exchange_calendars as xcals

from domain.investments.daily_return_state import normalize_return_status
from domain.assets.exposure_engine import _infer_stock_asset_class, resolve_market, resolveAssetType
from domain.investments.holdings_view import COMPLETE_ACCOUNT_SNAPSHOT_CTE
from domain.investments.investment_view_date import daily_pnl_window_date
from domain.investments.portfolio_state import _is_cash_like_holding


def read_overseas_quote_health(cur, now: datetime) -> dict[str, Any]:
    """Read current positions and their latest quote evidence, never NAV/returns."""
    cur.execute(
        f"""
        WITH {COMPLETE_ACCOUNT_SNAPSHOT_CTE}, current_members AS (
            SELECT DISTINCT h.account_code, h.currency, COALESCE(NULLIF(h.symbol, ''), h.name) AS position_key
            FROM pams_holdings_snapshot h
            JOIN latest_account la ON la.account_code = h.account_code AND la.account_as_of = h.as_of
            WHERE h.major_category = %s
        ), ranked AS (
            SELECT h.*, ROW_NUMBER() OVER (
                PARTITION BY account_code, currency, COALESCE(NULLIF(symbol, ''), name)
                ORDER BY as_of DESC, updated_at DESC, id DESC
            ) AS rn
            FROM pams_holdings_snapshot h
            WHERE major_category = %s
        )
        SELECT h.account_code, h.asset_id, h.symbol, h.name, h.currency,
               h.as_of, h.updated_at, h.current_price, h.quantity,
               h.asset_liability, h.major_category, h.minor_category,
               am.market, am.asset_type, am.listing_region,
               q.quote_date, q.price AS quote_price, q.return_status,
               q.source, q.quote_session, q.confidence, q.quote_market, q.quote_time, q.close_evidence, q.projection_only
        FROM ranked h
        JOIN current_members cm ON cm.account_code = h.account_code AND cm.currency = h.currency
          AND cm.position_key = COALESCE(NULLIF(h.symbol, ''), h.name)
        LEFT JOIN LATERAL (
            SELECT m.market, m.asset_type, m.listing_region
            FROM pams_asset_master m
            WHERE m.status = 'active'
              AND (m.asset_id = h.asset_id OR m.primary_symbol = h.symbol)
            ORDER BY (m.asset_id = h.asset_id) DESC NULLS LAST, m.asset_id
            LIMIT 1
        ) am ON TRUE
        LEFT JOIN LATERAL (
            SELECT evidence.* FROM (
                (SELECT quote_date, price, return_status, source, quote_session,
                        raw_json->>'confidence' AS confidence, market AS quote_market,
                        COALESCE(raw_json->>'as_of_utc', price_time::text) AS quote_time,
                        (quote_session = 'close' AND return_status = 'confirmed'
                         AND source LIKE '%%close%%' AND source NOT LIKE '%%estimate%%') AS close_evidence,
                        (raw_json->>'valuation_projection_only' = 'true') AS projection_only,
                        updated_at, 0 AS priority
                 FROM pams_market_quote_snapshot s
                 WHERE s.currency = h.currency
                   AND (s.asset_id = h.asset_id OR s.symbol = h.symbol)
                 ORDER BY quote_date DESC, updated_at DESC, quote_id DESC LIMIT 1)
                UNION ALL
                (SELECT as_of, COALESCE(estimated_unit_nav, close_price, unit_nav),
                        NULL, source, raw_json->>'session', confidence, market, raw_json->>'as_of_utc',
                        (source LIKE '%%close%%' AND source NOT LIKE '%%estimate%%'), FALSE, updated_at, 1
                 FROM pams_asset_market_daily d
                 WHERE d.currency = h.currency
                   AND (d.asset_id = h.asset_id OR d.symbol = h.symbol)
                 ORDER BY as_of DESC, updated_at DESC, source, source_key LIMIT 1)
                UNION ALL
                (SELECT trade_date, close, 'confirmed', 'yahoo_close', 'close',
                        NULL, 'YF', NULL, TRUE, FALSE, NULL::timestamptz, 2
                 FROM pams_ohlcv_daily o
                 WHERE o.symbol = h.symbol AND o.market = 'YF'
                 ORDER BY trade_date DESC LIMIT 1)
            ) evidence
            ORDER BY quote_date DESC, priority, updated_at DESC NULLS LAST
            LIMIT 1
        ) q ON TRUE
        WHERE h.rn = 1 AND h.quantity IS DISTINCT FROM 0
          AND h.asset_liability = '资产'
        ORDER BY h.account_code, h.symbol, h.name
        """,
        (now.astimezone(ZoneInfo("Asia/Shanghai")).date(), "美股", "美股"),
    )
    rows = [dict(row) for row in cur.fetchall()]
    return overseas_quote_health(rows, now)


@lru_cache(maxsize=5)
def _quote_calendar(name: str):
    return xcals.get_calendar(name)


def _overseas_calendar_name(row: dict[str, Any]) -> str | None:
    # These are calendar bindings for canonical market/listing labels, not FX rules.
    calendars = {"美股": "XNYS", "港股": "XHKG", "韩股": "XKRX"}
    listing_calendars = {"美国": "XNYS", "香港": "XHKG", "韩国": "XKRX", "瑞典": "XSTO"}
    market = resolve_market(row)
    name = listing_calendars.get(str(row.get("listing_region") or "")) or calendars.get(market)
    if not name and market in {"基金", "货币基金 / 债券", "黄金 / 商品", "其他"}:
        stock_class = _infer_stock_asset_class(row.get("symbol"), row.get("currency"), row.get("listing_region"))
        name = {"stock_us": "XNYS", "stock_hk": "XHKG", "stock_kr": "XKRX"}.get(stock_class)
    return name


def _overseas_due_date(row: dict[str, Any], now: datetime) -> date | None:
    name = _overseas_calendar_name(row)
    if name is None:
        return None
    calendar = _quote_calendar(name)
    local_date = daily_pnl_window_date(now=now) if name == "XNYS" else now.astimezone(calendar.tz).date()
    session = calendar.date_to_session(local_date.isoformat(), direction="previous")
    if (name != "XNYS" and session.date() == local_date and row.get("quote_session") != "premarket"
            and calendar.session_open(session).to_pydatetime() > now):
        session = calendar.previous_session(session)
    return session.date()


def _health_date(value: Any) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _positive_price(value: Any) -> bool:
    try:
        price = Decimal(str(value))
        return price.is_finite() and price > 0
    except (InvalidOperation, ValueError, TypeError):
        return False


def _quote_timestamp(value: Any) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value))
        return stamp if stamp.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def overseas_quote_health(rows: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    """Require coverage of every current account/security; keep facts untouched."""
    items = []
    due_dates: dict[tuple[str | None, bool], date | None] = {}
    for row in rows:
        if row.get("quantity") == 0 or _is_cash_like_holding(row) or resolveAssetType(row) == "现金":
            continue
        key = _overseas_calendar_name(row)
        due_key = (key, row.get("quote_session") == "premarket")
        if due_key not in due_dates:
            try:
                due_dates[due_key] = _overseas_due_date(row, now)
            except (ValueError, KeyError, OverflowError):
                due_dates[due_key] = None
        due = due_dates[due_key]
        snapshot_date = _health_date(row.get("as_of"))
        quote_date = _health_date(row.get("quote_date"))
        valuation_date = max(snapshot_date, quote_date) if row.get("projection_only") and snapshot_date and quote_date else snapshot_date
        return_status = normalize_return_status(
            row.get("return_status"), source=row.get("source"),
            quote_session=row.get("quote_session"), market=row.get("quote_market"),
        )
        issues = []
        if due is None:
            issues.append("unknown_market_calendar")
        if snapshot_date is None or quote_date is None:
            issues.append("missing_date")
        today = now.astimezone(ZoneInfo("Asia/Shanghai")).date()
        if any(day and day > today for day in (snapshot_date, quote_date)):
            issues.append("future_date")
        if due and (not valuation_date or valuation_date < due or not quote_date or quote_date < due):
            issues.append("stale")
        if due and quote_date and quote_date > due:
            issues.append("date_mismatch")
        if not _positive_price(row.get("current_price")) or not _positive_price(row.get("quote_price")):
            issues.append("missing_price")
        if return_status == "pending" or row.get("confidence") == "needs_review":
            issues.append("unconfirmed_quote")
        stamp = _quote_timestamp(row.get("quote_time"))
        if row.get("close_evidence") and return_status == "confirmed":
            if due and key and _quote_calendar(key).session_close(due.isoformat()).to_pydatetime() > now:
                issues.append("premature_close")
            if row.get("quote_time") is not None and (
                stamp is None or stamp > now
                or (key and stamp.astimezone(_quote_calendar(key).tz).date() != quote_date)
            ):
                issues.append("invalid_quote_time")
        else:
            if return_status == "confirmed":
                issues.append("unverified_close")
            if stamp is None:
                issues.append("missing_quote_time")
            elif key:
                stamp_date = (daily_pnl_window_date(now=stamp) if key == "XNYS"
                              else stamp.astimezone(_quote_calendar(key).tz).date())
                if stamp > now or stamp_date != quote_date:
                    issues.append("invalid_quote_time")
        items.append({
            "account_code": row.get("account_code"), "symbol": row.get("symbol"),
            "name": row.get("name"), "market": resolve_market(row), "calendar": key, "currency": row.get("currency"),
            "snapshot_date": snapshot_date.isoformat() if snapshot_date else None,
            "valuation_date": valuation_date.isoformat() if valuation_date else None,
            "quote_date": quote_date.isoformat() if quote_date else None,
            "expected_date": due.isoformat() if due else None,
            "return_status": return_status, "source": row.get("source"),
            "quote_time": stamp.isoformat() if stamp else None,
            "status": "review" if issues else "ok", "issues": issues,
        })
    covered = sum(item["status"] == "ok" for item in items)
    total = len(items)
    missing = sum(any(issue in item["issues"] for issue in ("missing_date", "missing_price")) for item in items)
    stale = sum("stale" in item["issues"] for item in items)
    details = f"当前账户/证券覆盖 {covered}/{total}；缺失 {missing} 项，过期 {stale} 项。"
    if not items:
        details += "无有效持仓行情可核验。"
    issue_labels = {
        "unknown_market_calendar": "市场日历待核验", "missing_date": "缺日期",
        "future_date": "日期异常", "stale": "过期", "missing_price": "缺有效价格",
        "unconfirmed_quote": "报价待核验",
        "missing_quote_time": "缺真实报价时间", "invalid_quote_time": "报价时间不符",
        "premature_close": "尚未收盘，不能确认收盘价",
        "date_mismatch": "报价日期不属于当前有效交易日", "unverified_close": "缺真实收盘证据",
    }
    for item in items:
        if item["issues"]:
            details += (
                f" {item['account_code']}/{item['symbol'] or item['name']}："
                f"快照 {item['snapshot_date'] or '缺失'}，报价 {item['quote_date'] or '缺失'}，"
                f"应有 {item['expected_date'] or '待核验'}（{'、'.join(issue_labels[issue] for issue in item['issues'])}）。"
            )
    dates = [day for item in items if (day := _health_date(item["snapshot_date"]))]
    times = [row["updated_at"] for row in rows if row.get("updated_at")]
    return {
        "as_of": max(dates, default=None), "updated_at": max(times, default=None), "rows": total,
        "status": "ok" if total and covered == total else "review",
        "label": "正常" if total and covered == total else "覆盖待核验",
        "details": details,
        "coverage": {"expected": total, "covered": covered, "missing": missing, "stale": stale, "items": items},
    }


def _flex_diagnostic(job: dict, current: datetime, *, key: str = "ibkr_flex",
                     label: str = "Flex 官方报告") -> tuple[dict, list[str]]:
    labels = {"success": "已确认", "waiting": "等待重试", "blocked": "需处理", "error": "读取失败", "running": "确认中"}
    reasons = {
        "official_report_confirmed": "官方报告已确认", "authorization_required": "请检查接口授权",
        "report_delayed": "官方报告尚未到达", "report_unconfirmed": "尚未取得有效发布确认",
        "rate_limited": "接口限流", "report_date_regression": "报告日期倒退已阻断",
        "source_unavailable": "状态读取或同步失败",
        "cash_events_confirmed": "已发布报表中的正常资金事件已处理",
        "production_account_or_base_mismatch": "生产与日结账户或本位币不一致，已阻断",
        "production_close_components_mismatch": "生产与日结组成不一致，已阻断",
        "production_report_changed": "读取期间生产原文变化，已阻断",
        "publication_historical_revision_requires_review": "官方历史资金流或收益修订已隔离，不自动改账",
        "publication_scope_or_date_regression": "日结范围或日期倒退，已阻断",
        "publication_same_date_source_conflict": "同日日结原文冲突，已阻断",
        "previous_source_integrity_failure": "上一有效日结原文完整性检查失败",
        "approved_close_evidence_unavailable": "已批准日结证据不可用，未自动替换",
    }
    issues = []
    status = job.get("status")
    checked = datetime.fromisoformat(job["last_checked_at"]) if job.get("last_checked_at") else None
    retry = datetime.fromisoformat(job["next_retry_at"]) if job.get("next_retry_at") else None
    if any(stamp.tzinfo is None for stamp in (checked, retry) if stamp):
        raise ValueError("confirmation timestamp must include timezone")
    if status != "success":
        issues.append(key + (":blocked" if status in {"blocked", "error"} else ":pending"))
    if not checked or current - checked > timedelta(hours=2) or (retry and current - retry > timedelta(hours=1)):
        issues.append(key + ":stale")
    detail = (
        f"{label}：{labels.get(str(status), '状态未知')}，"
        f"{reasons.get(str(job.get('reason')), '等待检查')}，"
        f"应有日期 {job.get('target_date') or '未知'}，"
        f"最近确认 {job.get('last_success_date') or '无'}，"
        f"下次重试 {job.get('next_retry_at') or '无需重试'}；不代表历史行情已补齐"
    )
    return {**job, "display_detail": detail}, issues


def scheduler_health(path: Path, now: datetime | None = None) -> dict:
    current = now or datetime.now(timezone.utc)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("version") != 1 or not isinstance(state.get("jobs"), dict):
            raise ValueError("invalid checkpoint")
        jobs = state["jobs"]
        if not jobs:
            return {"status": "missing", "label": "未运行", "jobs": {}}
        issues = []
        times = []
        for key in ("asia", "north_america"):
            job = jobs.get(key, {})
            if job.get("status") != "success":
                issues.append(key + ":blocked")
            if job.get("history_gap_since") or job.get("history_coverage") != "verified":
                issues.append(key + ":history_unverified")
            finished = job.get("finished_at")
            if not finished:
                issues.append(key + ":blocked")
            if finished:
                timestamp = datetime.fromisoformat(finished)
                if timestamp.tzinfo is None:
                    raise ValueError("checkpoint timestamp must include timezone")
                times.append(timestamp)
                if current - timestamp > timedelta(hours=72):
                    issues.append(key + ":stale")
        jobs = dict(jobs)
        for key, label in (("ibkr_flex", "Flex 官方报告"), ("ibkr_daily_close", "IBKR 日结发布"),
                           ("ibkr_cash_events", "IBKR 资金事实")):
            if key in jobs:
                jobs[key], job_issues = _flex_diagnostic(jobs[key], current, key=key, label=label)
                issues.extend(job_issues)
        # File existence and a recent failed attempt do not imply a successful refresh.
        blocked = any(item.endswith(":blocked") for item in issues)
        stale = any(item.endswith(":stale") for item in issues)
        status = "error" if blocked else "stale" if stale else "review" if issues else "ok"
        return {
            "status": status,
            "label": {"error": "发布已阻断", "stale": "同步过期", "review": "同步待核验" if any(item.endswith(":pending") for item in issues) else "历史待核验", "ok": "正常"}[status],
            "jobs": jobs,
            "issues": issues,
            "latest_time": max(times).isoformat() if times else None,
        }
    except FileNotFoundError:
        return {"status": "missing", "label": "未运行", "jobs": {}}
    except (OSError, ValueError, TypeError, AttributeError):
        return {"status": "error", "label": "同步记录不可读", "jobs": {}}
