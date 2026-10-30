"""Listing-market sessions for overseas quotes; not a portfolio accounting date."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from typing import Any

import exchange_calendars as xcals

from domain.investments.investment_view_date import daily_pnl_window_date

MARKET_CALENDARS = {"us": "XNYS", "hk": "XHKG", "kr": "XKRX", "se": "XSTO", "ch": "XSWX"}
MARKET_CURRENCIES = {"us": "USD", "hk": "HKD", "kr": "KRW", "se": "SEK", "ch": "CHF"}
MARKET_LABELS = {"us": "美股", "hk": "港股", "kr": "韩股", "se": "瑞典", "ch": "瑞士"}
_LISTINGS = {"美国": "us", "united states": "us", "香港": "hk", "hong kong": "hk",
             "韩国": "kr", "korea": "kr", "瑞典": "se", "sweden": "se",
             "瑞士": "ch", "switzerland": "ch"}


def market_bucket(row: dict[str, Any]) -> str | None:
    """Listing evidence wins over settlement currency; unknown is not US."""
    symbol = str(row.get("symbol") or row.get("primary_symbol") or "").strip().upper()
    if not symbol or symbol.startswith("CASH_"):
        return None
    for suffix, bucket in ((".HK", "hk"), (".KS", "kr"), (".KQ", "kr"), (".ST", "se"), (".SW", "ch")):
        if symbol.endswith(suffix):
            return bucket
    listing = _LISTINGS.get(str(row.get("listing_region") or "").strip().lower())
    if listing:
        return listing
    if symbol.isdigit() and 1 <= len(symbol) <= 5:
        return "hk"
    # Legacy IBKR symbols often omit the exchange suffix. These bindings are
    # limited to the existing overseas category, never applied to domestic funds.
    if row.get("major_category") != "美股":
        return None
    currency = str(row.get("currency") or "").upper()
    return next((bucket for bucket, ccy in MARKET_CURRENCIES.items() if ccy == currency), None)


@lru_cache(maxsize=5)
def market_calendar(bucket: str):
    return xcals.get_calendar(MARKET_CALENDARS[bucket])


def _now(now: datetime | None) -> datetime:
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("An aware market timestamp is required")
    return current


def latest_completed_session(bucket: str, now: datetime | None = None) -> date | None:
    """Wait for the exchange close plus publication/auction safety margin."""
    current = _now(now)
    try:
        calendar = market_calendar(bucket)
        day = current.astimezone(calendar.tz).date()
        session = calendar.date_to_session(day.isoformat(), direction="previous")
        # HKEX closing auction can finish after the continuous-session close.
        if calendar.session_close(session).to_pydatetime() + timedelta(minutes=15) > current:
            session = calendar.previous_session(session)
        return session.date()
    except (KeyError, ValueError, OverflowError):
        return None


def return_session_date(bucket: str, now: datetime | None = None) -> date | None:
    """Use the listing's opened trading session, preserving the US 07:00 window."""
    current = _now(now)
    try:
        calendar = market_calendar(bucket)
        day = daily_pnl_window_date(now=current) if bucket == "us" else current.astimezone(calendar.tz).date()
        session = calendar.date_to_session(day.isoformat(), direction="previous")
        if bucket != "us" and calendar.session_open(session).to_pydatetime() > current:
            session = calendar.previous_session(session)
        return session.date()
    except (KeyError, ValueError, OverflowError):
        return None
