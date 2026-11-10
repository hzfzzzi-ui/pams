"""Daily FX storage and read adapters."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
import os
from typing import Any
from zoneinfo import ZoneInfo


OPEN_EXCHANGE_RATES_SOURCE = 'openexchangerates'
REFRESH_CURRENCIES = ('CNY', 'USD', 'MXN', 'HKD', 'KRW', 'SEK', 'JPY', 'CHF')


def store_daily_fx_rates(cur, *, day: date, usd_rates: dict[str, Decimal]) -> dict[tuple[str, str], Decimal]:
    """Persist rates in PAMS units: one unit of quote currency in base currency."""
    rates = {
        (base, quote): (base_rate / quote_rate).quantize(Decimal('0.00000001'), rounding=ROUND_HALF_UP)
        for base, base_rate in usd_rates.items()
        for quote, quote_rate in usd_rates.items()
    }
    if any(rate <= 0 or rate >= Decimal('1000000000000') for rate in rates.values()):
        raise ValueError('FX rate is outside the storage precision')
    for (base, quote), rate in rates.items():
        cur.execute("""
            INSERT INTO pams_fx_rates_daily (as_of, base_currency, quote_currency, rate, source)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (as_of, base_currency, quote_currency) DO UPDATE
            SET rate = EXCLUDED.rate, source = EXCLUDED.source, fetched_at = EXCLUDED.fetched_at
            WHERE (pams_fx_rates_daily.rate, pams_fx_rates_daily.source)
                  IS DISTINCT FROM (EXCLUDED.rate, EXCLUDED.source)
        """, (day, base, quote, rate, OPEN_EXCHANGE_RATES_SOURCE))
    return rates


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
