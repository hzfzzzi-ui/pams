"""Daily FX storage and read adapters."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
import os
from typing import Any
from zoneinfo import ZoneInfo


OPEN_EXCHANGE_RATES_SOURCE = 'openexchangerates'
REFRESH_CURRENCIES = ('CNY', 'USD', 'MXN', 'HKD', 'KRW', 'SEK', 'JPY', 'CHF')


def store_daily_fx_rates(cur, *, day: date, usd_rates: dict[str, Decimal],
                         historical: bool = False) -> dict[tuple[str, str], Decimal]:
    """Persist rates in PAMS units: one unit of quote currency in base currency."""
    rates = {
        (base, quote): (base_rate / quote_rate).quantize(Decimal('0.00000001'), rounding=ROUND_HALF_UP)
        for base, base_rate in usd_rates.items()
        for quote, quote_rate in usd_rates.items()
    }
    if any(rate <= 0 or rate >= Decimal('1000000000000') for rate in rates.values()):
        raise ValueError('FX rate is outside the storage precision')
    for (base, quote), rate in rates.items():
        store_official_fx_rate(cur, day=day, base=base, quote=quote, rate=rate, historical=historical)
    return rates


def store_official_fx_rate(cur, *, day, base, quote, rate, historical=False):
    rate = rate.quantize(Decimal('0.00000001'), rounding=ROUND_HALF_UP)
    if not rate.is_finite() or not 0 < rate < Decimal('1000000000000'):
        raise ValueError('FX rate is outside the storage precision')
    cur.execute("""
            INSERT INTO pams_fx_rates_daily (as_of, base_currency, quote_currency, rate, source)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (as_of, base_currency, quote_currency) DO UPDATE
            SET rate = EXCLUDED.rate, source = EXCLUDED.source, fetched_at = EXCLUDED.fetched_at
            WHERE (pams_fx_rates_daily.rate, pams_fx_rates_daily.source)
                  IS DISTINCT FROM (EXCLUDED.rate, EXCLUDED.source)
               OR (%s AND pams_fx_rates_daily.fetched_at <
                   ((EXCLUDED.as_of + 1)::timestamp AT TIME ZONE 'UTC'))
        """, (day, base, quote, rate, OPEN_EXCHANGE_RATES_SOURCE, historical))


def has_official_usd_cny(cur, day: date) -> bool:
    cur.execute("""SELECT EXISTS (
        SELECT 1 FROM pams_fx_rates_daily
        WHERE as_of=%s AND base_currency='CNY' AND quote_currency='USD'
          AND source=%s AND rate>0 AND rate<1000000000000
          AND fetched_at >= ((as_of + 1)::timestamp AT TIME ZONE 'UTC')
    )""", (day, OPEN_EXCHANGE_RATES_SOURCE))
    return cur.fetchone()[0]


def has_finalized_daily_fx(cur, day: date) -> bool:
    cur.execute("""SELECT count(*) FROM pams_fx_rates_daily
        WHERE as_of=%s AND source=%s AND base_currency=ANY(%s) AND quote_currency=ANY(%s)
          AND rate>0 AND rate<1000000000000
          AND fetched_at >= ((as_of + 1)::timestamp AT TIME ZONE 'UTC')
    """, (day, OPEN_EXCHANGE_RATES_SOURCE, list(REFRESH_CURRENCIES), list(REFRESH_CURRENCIES)))
    return cur.fetchone()[0] == len(REFRESH_CURRENCIES) ** 2


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
