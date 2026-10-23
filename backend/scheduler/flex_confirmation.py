"""Acquire late official reports only. Never refresh prices or historical NAV here."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from refresh_state import job_lock, read_state, store_job

KEY = "ibkr_flex"


def _timestamp(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("invalid checkpoint timestamp")
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("checkpoint timestamps must include timezone")
    return result


def _probe_info(payload: dict) -> tuple[date | None, date | None, bool]:
    quality = payload.get("report_quality")
    if not isinstance(quality, dict):
        return None, None, False
    expected = quality.get("expected_statement_as_of")
    observed = quality.get("statement_as_of")
    target = date.fromisoformat(expected) if expected else None
    statement = date.fromisoformat(observed) if observed else None
    verified = bool(
        target and statement and statement >= target and quality.get("status") == "ok"
        and quality.get("legacy_unverified") is False
        and type(quality.get("active_run_id")) is int and quality["active_run_id"] > 0
    )
    return target, statement, verified


def _safe_probe(callback: Callable[[], dict]) -> dict:
    try:
        payload = callback()
        if not isinstance(payload, dict):
            return {}
        _probe_info(payload)
        return payload
    except Exception:
        # Provider exception messages and response bodies can contain credentials.
        return {}


def _result(job: dict, *, deferred: bool = False) -> dict:
    return {
        "success": job.get("status") == "success" and not deferred,
        "status": "deferred" if deferred else job.get("status"),
        "reason": job.get("reason"), "next_retry_at": job.get("next_retry_at"),
        "statement_as_of": job.get("last_observed_statement_as_of"),
        "expected_statement_as_of": job.get("target_date"),
        "publication_verified": job.get("status") == "success" and not deferred,
    }


class FlexConfirmationJournal:
    def __init__(self, path: Path):
        self.path = path

    def run(self, probe: Callable[[], dict], sync: Callable[[date], Any], *, now: datetime | None = None) -> dict:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("confirmation clock must include timezone")
        try:
            with job_lock(self.path, KEY):
                return self._run(probe, sync, current, now is None)
        except BlockingIOError:
            return {"success": False, "status": "deferred", "reason": "job_busy"}

    def _run(self, probe, sync, current: datetime, real_clock: bool) -> dict:
        previous = read_state(self.path)["jobs"].get(KEY, {})
        attempts = previous.get("attempt_count", 0)
        if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
            raise ValueError("invalid confirmation attempt count")
        due = _timestamp(previous["next_retry_at"]) if previous.get("next_retry_at") else None
        payload = _safe_probe(probe)  # Local metadata GET, not a provider download.
        target, statement, verified = _probe_info(payload)
        run = {
            **previous, "scope": "official_report_only", "last_checked_at": current.isoformat(),
            "target_date": target.isoformat() if target else previous.get("target_date"),
            "last_observed_statement_as_of": statement.isoformat() if statement else None,
            "last_success_date": previous.get("last_success_date"),
            "recovered_interrupted": previous.get("status") == "running",
            "history_coverage": previous.get("history_coverage", "unverified"),
            "recent_runs": previous.get("recent_runs", [])[-19:],
        }
        last = date.fromisoformat(run["last_success_date"]) if run["last_success_date"] else None
        if verified and statement is not None and (last is None or statement >= last):
            return self._success(run, statement, current)
        if due and current < due:
            store_job(self.path, KEY, run)
            return _result(run, deferred=True)
        run.update(status="running", started_at=current.isoformat(), finished_at=None,
                   attempt_count=attempts + 1, recovered_interrupted=previous.get("status") == "running")
        store_job(self.path, KEY, run)  # A durable intent is required before any provider POST.
        response: dict = payload
        regression = bool(verified and last and statement is not None and statement < last)
        if target and payload.get("configured") is True and not regression:
            try:
                value = sync(target)
                response = value if isinstance(value, dict) else {}
            except Exception:
                response = {}
            after = _safe_probe(probe)
            after_target, observed, accepted = _probe_info(after)
            if after_target:
                run["target_date"] = after_target.isoformat()
            if observed:
                run["last_observed_statement_as_of"] = observed.isoformat()
            if (accepted and observed is not None and after_target is not None
                    and after_target >= target and observed >= target and (last is None or observed >= last)):
                return self._success(run, observed, datetime.now(timezone.utc) if real_clock else current)
        finished = datetime.now(timezone.utc) if real_clock else current
        code = response.get("http_status")
        if (payload.get("configured") is False or code in (400, 401, 403)
                or response.get("reason") == "ibkr_flex_not_configured"):
            state, reason = "blocked", "authorization_required"
        elif code == 429:
            state, reason = "waiting", "rate_limited"
        elif regression:
            state, reason = "blocked", "report_date_regression"
        elif response.get("reason") == "stale_statement_date":
            state, reason = "waiting", "report_delayed"
        elif response.get("status") in {"blocked", "success"} or response.get("success") is True:
            state, reason = "waiting", "report_unconfirmed"
        else:
            state, reason = "error", "source_unavailable"
        delay = 6 * 3600 if state == "blocked" else min(1800 * 2 ** min(attempts, 4), 6 * 3600)
        retry_at = finished + timedelta(seconds=delay)
        if response.get("retry_after"):
            try:
                retry_at = max(retry_at, _timestamp(response["retry_after"]))
            except (ValueError, TypeError):
                pass
        run.update(status=state, reason=reason, http_status=code if isinstance(code, int) else None,
                   finished_at=finished.isoformat(), next_retry_at=retry_at.isoformat(), issues=[reason])
        run["recent_runs"].append({key: run.get(key) for key in (
            "status", "target_date", "started_at", "finished_at", "reason", "attempt_count",
        )})
        store_job(self.path, KEY, run)
        return _result(run)

    def _success(self, run: dict, statement: date, current: datetime) -> dict:
        changed = run.get("status") != "success" or run.get("last_success_date") != statement.isoformat()
        attempt = run.get("attempt_count", 0)
        run.update(status="success", reason="official_report_confirmed", last_success_date=statement.isoformat(),
                   last_observed_statement_as_of=statement.isoformat(), finished_at=current.isoformat(),
                   next_retry_at=None, attempt_count=0, http_status=None, issues=[])
        if changed:
            run["recent_runs"].append({"status": "success", "target_date": run.get("target_date"),
                                       "finished_at": current.isoformat(), "attempt_count": attempt})
        store_job(self.path, KEY, run)
        return _result(run)
