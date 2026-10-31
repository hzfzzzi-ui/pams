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
               total_pnl_cny,
               cycle,
               status,
               data_completeness,
               source_run_id,
               source_hash,
               raw_json,
               published_at
        FROM ranked
        WHERE rn = 1
        ORDER BY valuation_date DESC
        LIMIT %s
        """,
        (base_currency, limit),
    )
    return list(reversed([dict(row) for row in cur.fetchall()]))


def valuation_series_evidence(raw: dict[str, Any], *, source_table: str) -> dict[str, Any]:
    """Expose stored evidence without inferring a confirmed daily valuation.

    A publication, complete inputs, or an IBKR close alone does not prove that
    every component is an exact daily close for the same business date.
    """
    is_state = source_table == "pams_portfolio_valuation_state"
    status = raw.get("status") if is_state else None
    status = status if status in {"draft", "published", "failed"} else "unknown"
    raw_json = raw.get("raw_json") if is_state else None
    source = raw_json.get("source") if isinstance(raw_json, dict) else None
    if not isinstance(source, str):
        source = None
    return {
        "source_table": source_table,
        "source": source,
        "business_date": raw.get("valuation_date") if is_state else raw.get("date", raw.get("as_of")),
        "publication_status": status,
        "data_completeness": raw.get("data_completeness") if is_state else None,
        "valuation_cycle": raw.get("cycle") if is_state else None,
        "valuation_period": "unknown",
        "source_run_id": raw.get("source_run_id") if is_state else None,
        "source_hash": raw.get("source_hash") if is_state else None,
        "published_at": raw.get("published_at") if is_state else None,
        "daily_accurate_eligible": False if status in {"draft", "failed"} else None,
        "input_lineage_status": "unknown",
    }
