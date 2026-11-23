"""Dated exchange rates used by account and portfolio valuation."""
from datetime import date as Date
from decimal import Decimal


def load_fx_timelines(
    cur,
    base_currency: str,
    *,
    identity_date: Date | None = None,
) -> dict[str, list[tuple[Date, Decimal]]]:
    """Load FX rates once so valuation back-casting can convert by sample date."""
    base_currency = base_currency.upper()
    cur.execute(
        """
        SELECT base_currency, quote_currency, as_of, rate
        FROM pams_fx_rates_daily
        WHERE base_currency = ANY(%s::text[])
          AND rate > 0 AND rate::text NOT IN ('NaN','Infinity','-Infinity')
          AND NULLIF(BTRIM(source), '') IS NOT NULL
        ORDER BY base_currency, quote_currency, as_of
        """,
        ([base_currency, 'CNY'],),
    )
    direct: dict[tuple[str, Date], Decimal] = {}
    cny: dict[tuple[str, Date], Decimal] = {}
    for row in cur.fetchall():
        currency = str(row["quote_currency"] or "").upper()
        day = row["as_of"]
        rate = Decimal(str(row["rate"]))
        if str(row.get("base_currency") or base_currency).upper() == base_currency:
            direct[(currency, day)] = rate
        if str(row.get("base_currency") or base_currency).upper() == 'CNY':
            cny[(currency, day)] = rate
    if base_currency != 'CNY':
        for (currency, day), rate in cny.items():
            denominator = cny.get((base_currency, day))
            if denominator is not None:
                direct.setdefault((currency, day), rate / denominator)
                direct.setdefault(('CNY', day), Decimal(1) / denominator)
    timelines: dict[str, list[tuple[Date, Decimal]]] = {}
    for (currency, day), rate in sorted(direct.items(), key=lambda item: (item[0][0], item[0][1])):
        timelines.setdefault(currency, []).append((day, rate))
    timelines.setdefault(base_currency, []).append((identity_date or Date(2026, 5, 31), Decimal("1")))
    return timelines


def fx_rate_at(
    timelines: dict[str, list[tuple[Date, Decimal]]],
    currency: str,
    as_of: Date,
    base_currency: str,
) -> Decimal | None:
    """Return a same-day rate, or unknown when the evidence is absent."""
    quote = str(currency or base_currency).upper()
    if quote == str(base_currency).upper():
        return Decimal("1")
    rows = timelines.get(quote) or []
    for row_date, rate in reversed(rows):
        if row_date == as_of:
            return rate if rate.is_finite() and rate > 0 else None
    return None
