"""Read scheduler diagnostics without writing portfolio state."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _flex_diagnostic(job: dict, current: datetime, *, key: str = "ibkr_flex",
                     label: str = "Flex 官方报告") -> tuple[dict, list[str]]:
    labels = {"success": "已确认", "waiting": "等待重试", "blocked": "需处理", "error": "读取失败", "running": "确认中"}
    reasons = {
        "official_report_confirmed": "官方报告已确认", "authorization_required": "请检查接口授权",
        "report_delayed": "官方报告尚未到达", "report_unconfirmed": "尚未取得有效发布确认",
        "rate_limited": "接口限流", "report_date_regression": "报告日期倒退已阻断",
        "source_unavailable": "状态读取或同步失败",
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
