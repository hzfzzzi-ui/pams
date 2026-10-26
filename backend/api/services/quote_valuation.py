"""Current quote marks may change valuation, never account membership or cost."""
from __future__ import annotations


def snapshot_tables(cur) -> dict:
    cur.execute("SELECT to_regclass(%s) AS table_name, to_regclass('pams_market_quote_snapshot') IS NOT NULL AS quote_available",
                ("pams_holdings_snapshot",))
    return dict(cur.fetchone() or {})


def quote_valuation_cte(enabled: bool) -> str:
    """Consume source_holdings plus the existing bounded account snapshot CTE."""
    if not enabled:
        return "h AS (SELECT * FROM source_holdings)"
    return """
    quote_marks AS (
        SELECT s.id, q.price, q.quote_date, q.updated_at
        FROM source_holdings s
        JOIN LATERAL (
            SELECT q.price, q.quote_date, q.updated_at
            FROM pams_market_quote_snapshot q
            WHERE q.currency = s.currency AND q.symbol = s.symbol
              AND q.market = 'US' AND q.currency = 'USD'
              AND q.quote_date > s.as_of
              AND q.quote_date <= (SELECT MAX(as_of) FROM account_snapshot_stats)
              AND q.price > 0 AND q.price::text NOT IN ('NaN', 'Infinity', '-Infinity')
              AND q.return_status IN ('estimated', 'confirmed')
              AND q.raw_json @> '{"valuation_projection_only":true}'::jsonb
              AND s.quantity > 0 AND s.asset_liability = '资产' AND s.major_category = '美股'
            ORDER BY q.quote_date DESC, q.updated_at DESC, q.quote_id DESC LIMIT 1
        ) q ON TRUE
    ), quote_account_dates AS (
        SELECT s.account_code, MAX(q.quote_date) AS as_of
        FROM source_holdings s JOIN quote_marks q ON q.id = s.id GROUP BY s.account_code
    ), marked_holdings AS (
        SELECT s.*, q.price AS marked_price,
               CASE WHEN q.price IS NOT NULL THEN ROUND(s.quantity * q.price, 2) ELSE s.market_value END AS marked_value,
               GREATEST(s.as_of, d.as_of) AS valuation_as_of,
               COALESCE(q.updated_at, s.updated_at) AS mark_updated_at
        FROM source_holdings s LEFT JOIN quote_marks q ON q.id = s.id
        LEFT JOIN quote_account_dates d ON d.account_code = s.account_code
    ), h AS (
        SELECT id, symbol, name, asset_liability, currency, account_code,
               major_category, minor_category, marked_value AS market_value, cost,
               CASE WHEN marked_price IS NOT NULL THEN marked_value - cost ELSE holding_pnl END AS holding_pnl,
               valuation_as_of AS as_of, created_at, mark_updated_at AS updated_at,
               quantity, buy_price, COALESCE(marked_price, current_price) AS current_price, asset_id
        FROM marked_holdings
    )
    """
