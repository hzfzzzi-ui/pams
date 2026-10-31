"""Read the existing published valuation state without recalculating it.

The caller owns the table-availability check.  This adapter deliberately returns
the complete stored row, including its source evidence and completeness status.
"""

from typing import Any


def latest_published(cur: Any, *, base_currency: str = "CNY") -> dict[str, Any] | None:
    cur.execute(
        """
        SELECT *
        FROM pams_portfolio_valuation_state
        WHERE base_currency = %s
          AND status = 'published'
        ORDER BY valuation_date DESC, published_at DESC NULLS LAST, updated_at DESC
        LIMIT 1
        """,
        (str(base_currency or "CNY").upper(),),
    )
    return cur.fetchone()
