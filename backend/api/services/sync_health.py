"""Read scheduler diagnostics without writing portfolio state."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


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
        # File existence and a recent failed attempt do not imply a successful refresh.
        blocked = any(item.endswith(":blocked") for item in issues)
        stale = any(item.endswith(":stale") for item in issues)
        status = "error" if blocked else "stale" if stale else "review" if issues else "ok"
        return {
            "status": status,
            "label": {"error": "发布已阻断", "stale": "同步过期", "review": "历史待核验", "ok": "正常"}[status],
            "jobs": jobs,
            "issues": issues,
            "latest_time": max(times).isoformat() if times else None,
        }
    except FileNotFoundError:
        return {"status": "missing", "label": "未运行", "jobs": {}}
    except (OSError, ValueError, TypeError, AttributeError):
        return {"status": "error", "label": "同步记录不可读", "jobs": {}}
