from __future__ import annotations

from datetime import date as Date, datetime, time, timedelta
from zoneinfo import ZoneInfo


MARKET_DATA_CUTOFF = time(15, 5)
DAILY_PNL_RESET_CUTOFF = time(7, 0)


def market_data_candidate_date(
    *,
    now: datetime | None = None,
    tz_name: str = "Asia/Shanghai",
    cutoff: time = MARKET_DATA_CUTOFF,
) -> Date:
    """Return the latest market-data date that should be visible to users.

    A-share and fund valuation data should not roll to the current calendar
    date before the 15:05 Beijing refresh window, otherwise the UI compares a
    synthetic empty row with the prior real row and shows zero daily return.
    """
    zone = _zone(tz_name)
    current = now or datetime.now(zone)
    if current.tzinfo is not None:
        current = current.astimezone(zone)

    if current.time() < cutoff:
        return current.date() - timedelta(days=1)
    return current.date()


def daily_pnl_window_date(
    *,
    now: datetime | None = None,
    tz_name: str = "Asia/Shanghai",
    cutoff: time = DAILY_PNL_RESET_CUTOFF,
) -> Date:
    """Return the active daily PnL label date.

    Daily PnL should survive Beijing midnight because US-market refreshes can
    finish after the calendar day changes. Roll the visible "today" bucket at
    07:00 Beijing time, after the regular US close and refresh window.
    """
    zone = _zone(tz_name)
    current = now or datetime.now(zone)
    if current.tzinfo is not None:
        current = current.astimezone(zone)

    if current.time() < cutoff:
        return current.date() - timedelta(days=1)
    return current.date()


def clamp_to_latest_data_date(candidate: Date, latest_data_date: Date | None) -> Date:
    """Clamp the candidate view date to the newest persisted data date."""
    if latest_data_date is None:
        return candidate
    return latest_data_date if latest_data_date < candidate else candidate


def _zone(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("Asia/Shanghai")
