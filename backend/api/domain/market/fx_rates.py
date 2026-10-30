"""FX rate read adapters."""

from __future__ import annotations

from decimal import Decimal
from typing import Any


def _norm(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def build_latest_fx_rates_payload(cur, *, base_currency: str) -> dict[str, Any]:
    """Return the legacy /api/fx-rates/latest payload."""
    cur.execute(
        """
        SELECT DISTINCT ON (quote_currency)
               as_of, base_currency, quote_currency, rate, source
        FROM pams_fx_rates_daily
        WHERE base_currency = %s
        ORDER BY quote_currency, as_of DESC
        """,
        (base_currency,),
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
