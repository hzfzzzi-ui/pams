"""FX rate read adapters."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import os
from typing import Any
from zoneinfo import ZoneInfo


def _norm(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _view_date() -> date:
    tz_name = os.environ.get("PAMS_TIMEZONE", os.environ.get("TZ", "Asia/Shanghai"))
    try:
        zone = ZoneInfo(tz_name)
    except Exception:
        zone = ZoneInfo("Asia/Shanghai")
    return datetime.now(zone).date()


def build_latest_fx_rates_payload(cur, *, base_currency: str, as_of: date | None = None) -> dict[str, Any]:
    """Return latest sourced, positive, nonfuture rates and their actual dates."""
    view_date = as_of or _view_date()
    cur.execute(
        """
        SELECT DISTINCT ON (quote_currency)
               as_of, base_currency, quote_currency, rate, source
        FROM pams_fx_rates_daily
        WHERE base_currency = %s AND as_of <= %s
          AND rate > 0 AND rate::text NOT IN ('NaN', 'Infinity', '-Infinity')
          AND source ~ '[^[:space:]]'
        ORDER BY quote_currency, as_of DESC
        """,
        (base_currency, view_date),
    )
    rows = cur.fetchall()

    if not rows:
        return {"as_of": None, "rates": []}

    latest_as_of = max(row["as_of"] for row in rows)
    return {
        "as_of": latest_as_of.isoformat(),
        "base_currency": base_currency,
        "rates": [{key: _norm(value) for key, value in dict(row).items()} for row in rows],
    }
