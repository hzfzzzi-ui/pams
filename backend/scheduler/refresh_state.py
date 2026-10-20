"""Durable refresh checkpoints. Historical gaps are never silently cleared."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


def latest_due_date(local_time: str, zone: str, now: datetime | None = None) -> date:
    current = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(zone))
    hour, minute = map(int, local_time.split(":"))
    due = current.date()
    if (current.hour, current.minute) < (hour, minute):
        due -= timedelta(days=1)
    while due.weekday() >= 5:
        due -= timedelta(days=1)
    return due


def read_state(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "jobs": {}}
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or state.get("version") != 1 or not isinstance(state.get("jobs"), dict):
        raise ValueError("invalid refresh checkpoint")
    for job in state["jobs"].values():
        if not isinstance(job, dict) or not isinstance(job.get("recent_runs", []), list):
            raise ValueError("invalid job checkpoint")
        for field in ("last_success_date", "history_gap_since", "target_date"):
            if job.get(field) is not None:
                date.fromisoformat(str(job[field]))
    return state


def write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="refresh-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def result_issues(result, *, target: date | None = None, allow_skip: bool = False) -> list[str]:
    if not isinstance(result, dict) or not result:
        return ["missing_result"]
    if result.get("skipped"):
        return [] if allow_skip else ["required_job_skipped"]
    issues = []
    if result.get("success") is False or result.get("error") or result.get("errors"):
        issues.append("refresh_errors")
    status = str(result.get("status", "ok")).lower()
    if status not in {"ok", "success", "confirmed", "estimated"}:
        issues.append("status_" + status)
    if result.get("manual_review_items"):
        issues.append("manual_review_required")
    if "found" in result and int(result.get("updated") or 0) < int(result["found"] or 0):
        issues.append("partial_update")
    if result.get("skipped_estimates"):
        issues.append("missing_estimates")
    if target:
        for key in ("as_of", "data_as_of", "statement_as_of", "statement_date"):
            value = result.get(key)
            if value and date.fromisoformat(str(value)[:10]) < target:
                issues.append("stale_" + key)
        for row in result.get("rows", []):
            if not isinstance(row, dict):
                issues.append("invalid_row")
                continue
            row_status = str(row.get("status", "")).lower()
            dates = [row.get(k) for k in ("estimate_date", "estimate_time", "estimated_nav_date", "confirmed_date", "confirmed_nav_date", "nav_date", "as_of") if row.get(k)]
            latest = max((date.fromisoformat(str(v)[:10]) for v in dates), default=None)
            if any(word in row_status for word in ("missing", "review", "error", "failed")):
                issues.append("row_" + row_status)
            if "stale" in row_status and (latest is None or latest < target):
                issues.append("row_" + row_status)
            if latest is not None and latest < target:
                issues.append("stale_row")
    return sorted(set(issues))


class RefreshJournal:
    def __init__(self, path: Path):
        self.path = path

    def run(self, key: str, target: date, callback) -> dict:
        state = read_state(self.path)
        previous = state["jobs"].get(key, {})
        if previous.get("status") == "success" and previous.get("last_success_date") == target.isoformat():
            return {"status": "skipped", "reason": "already_completed"}
        gap = previous.get("history_gap_since")
        last = previous.get("last_success_date")
        if last and date.fromisoformat(last) > target:
            raise ValueError("checkpoint is ahead of target date")
        if last:
            candidate = date.fromisoformat(last) + timedelta(days=1)
            while candidate.weekday() >= 5:
                candidate += timedelta(days=1)
            if candidate < target and not gap:
                gap = candidate.isoformat()
        run = {
            "status": "running", "target_date": target.isoformat(),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "last_success_date": last, "history_gap_since": gap,
            "history_coverage": "gap" if gap else previous.get("history_coverage", "unverified"),
            "recent_runs": previous.get("recent_runs", [])[-19:],
        }
        state["jobs"][key] = run
        write_state(self.path, state)
        try:
            result = callback(target)
            issues = result_issues(result)
            run.update(status="blocked" if issues else "success", issues=issues)
            run["steps"] = result.get("steps", []) if isinstance(result, dict) else []
            if not issues:
                run["last_success_date"] = target.isoformat()
        except Exception as exc:
            # Store only the exception class: provider exceptions may contain credentials.
            run.update(status="error", issues=[type(exc).__name__])
        run["finished_at"] = datetime.now(timezone.utc).isoformat()
        run["recent_runs"].append({k: run[k] for k in ("target_date", "status", "started_at", "finished_at", "issues")})
        write_state(self.path, state)
        return run
