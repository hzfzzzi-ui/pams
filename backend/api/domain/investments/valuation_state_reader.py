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


def valuation_series_rows(cur: Any, *, base_currency: str, limit: int) -> list[dict[str, Any]]:
    """Read series rows using the existing per-day published-preferred policy.

    Unlike the published snapshot, the series can fall back to draft/failed rows.
    The policy is intentionally retained as-is until separately redesigned.
    """
    cur.execute(
        """
        WITH ranked AS (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY valuation_date
                       ORDER BY
                           CASE WHEN status = 'published' THEN 0 ELSE 1 END,
                           published_at DESC NULLS LAST,
                           updated_at DESC NULLS LAST
                   ) AS rn
            FROM pams_portfolio_valuation_state
            WHERE base_currency = %s
        )
        SELECT valuation_date,
               assets_cny,
               liabilities_cny,
               net_liquidation_value,
               investment_value_cny,
               total_pnl_cny
        FROM ranked
        WHERE rn = 1
        ORDER BY valuation_date DESC
        LIMIT %s
        """,
        (base_currency, limit),
    )
    return list(reversed([dict(row) for row in cur.fetchall()]))
