"""Current quote marks may change valuation, never account membership or cost."""
from __future__ import annotations


GOLD_CLOSE_SOURCE = "akshare_sge_daily"
GOLD_BENCHMARK_ASSET = "BENCH_GOLD_SPOT_AU9999"


def uses_gold_close(row: str) -> str:
    """Identify gram-denominated gold, separately from gold funds and ETFs."""
    return f"""({row}.currency = 'CNY' AND {row}.major_category = '贵金属'
        AND {row}.asset_liability = '资产'
        AND (UPPER(COALESCE({row}.symbol, '')) IN ('CCB_GOLD_ACCUM', 'GOLD', 'XAU')
             OR COALESCE({row}.asset_id, '') IN ('ASSET_CCB_GOLD_ACCUM', 'ASSET_CCB_GOLD_ACCUMULATION')))"""


def gold_close_join(row: str, valuation_date: str, *, alias: str = "gold_close") -> str:
    """Resolve a published, dated Au99.99 close for a position."""
    return f"""LEFT JOIN LATERAL (
        SELECT close_price AS price, as_of AS quote_date, updated_at
        FROM market_observations
        WHERE {uses_gold_close(row)}
          AND observation_type = 'daily' AND source = '{GOLD_CLOSE_SOURCE}'
          AND asset_id = '{GOLD_BENCHMARK_ASSET}'
          AND as_of <= {valuation_date}
          AND close_price > 0 AND close_price::text NOT IN ('NaN','Infinity','-Infinity')
        ORDER BY as_of DESC, updated_at DESC LIMIT 1
    ) {alias} ON TRUE"""


def gold_market_value(row: str, alias: str = "gold_close") -> str:
    return f"""CASE WHEN {uses_gold_close(row)} THEN
        CASE WHEN {row}.quantity = 0 THEN 0 ELSE ROUND({row}.quantity * {alias}.price, 2) END
        ELSE {row}.market_value END"""


def snapshot_tables(cur) -> dict:
    cur.execute("SELECT to_regclass(%s) AS table_name, to_regclass('market_observations') IS NOT NULL AS quote_available",
                ("pams_holdings_snapshot",))
    return dict(cur.fetchone() or {})


def quote_price_as_of(enabled: bool) -> str:
    """Keep price evidence dates separate from the complete account's date."""
    if not enabled:
        return "h.as_of"
    return """(SELECT COALESCE(q.quote_date, s.as_of) FROM source_holdings s
               LEFT JOIN quote_marks q ON q.id = s.id WHERE s.id = h.id)"""


def quote_valuation_cte(enabled: bool, *, position_prices: bool = False) -> str:
    """Consume source_holdings plus the existing bounded account snapshot CTE."""
    if not enabled:
        return "h AS (SELECT * FROM source_holdings)"
    domestic = """
        UNION ALL
        SELECT s.id, q.price, q.as_of, q.updated_at, q.source
        FROM source_holdings s
        JOIN LATERAL (
            SELECT as_of, updated_at, source,
                   CASE WHEN s.minor_category='A股基金' THEN
                        COALESCE(estimated_unit_nav, confirmed_unit_nav, unit_nav) ELSE price END AS price
            FROM market_observations
            WHERE s.major_category='A股' AND s.asset_liability='资产' AND s.quantity>0
              AND as_of BETWEEN s.as_of AND (SELECT as_of FROM snapshot_view_cutoff)
              AND currency=s.currency
              AND (symbol=s.symbol OR asset_id=s.asset_id)
              AND (
                (s.minor_category='A股基金' AND (
                    (observation_type='fund' AND account_code=s.account_code
                     AND status IN ('estimated','confirmed','confirmed_corrected')
                     AND COALESCE(estimated_unit_nav,confirmed_unit_nav)>0)
                    OR (observation_type='daily' AND market='基金'
                        AND source IN ('tushare_fund_nav','eastmoney_f10') AND unit_nav>0)))
                OR (s.minor_category='A股' AND observation_type='quote'
                    AND market='CN_A' AND quote_session='close' AND return_status='confirmed' AND price>0)
              )
            ORDER BY as_of DESC, (estimated_unit_nav IS NULL) DESC, updated_at DESC, quote_id DESC LIMIT 1
        ) q ON TRUE
    """ if position_prices else ""
    holding_date = "s.as_of" if position_prices else """
        CASE WHEN {gold} THEN s.as_of ELSE GREATEST(s.as_of, d.as_of) END
    """.format(gold=uses_gold_close("s"))
    return f"""
    quote_marks AS (
        SELECT s.id, q.price, q.quote_date, q.updated_at, NULL::text AS valuation_price_source
        FROM source_holdings s
        JOIN LATERAL (
            SELECT q.price, q.quote_date, q.updated_at
            FROM (SELECT * FROM market_observations WHERE observation_type = 'quote') q
            WHERE q.currency = s.currency AND q.symbol = s.symbol
              AND (q.market, q.currency) IN (
                  ('US', 'USD'), ('HK', 'HKD'), ('KR', 'KRW'), ('SE', 'SEK'), ('CH', 'CHF')
              )
              AND q.market = CASE
                  WHEN UPPER(s.symbol) LIKE '%%.ST' THEN 'SE'
                  WHEN UPPER(s.symbol) LIKE '%%.SW' THEN 'CH'
                  WHEN UPPER(s.symbol) LIKE '%%.KS' OR UPPER(s.symbol) LIKE '%%.KQ' THEN 'KR'
                  WHEN UPPER(s.symbol) LIKE '%%.HK' THEN 'HK'
                  WHEN s.symbol ~ '^[0-9]{{1,5}}$' THEN 'HK'
                  WHEN s.currency = 'KRW' THEN 'KR'
                  WHEN s.currency = 'HKD' THEN 'HK'
                  WHEN s.currency = 'SEK' THEN 'SE'
                  WHEN s.currency = 'CHF' THEN 'CH'
                  WHEN s.currency = 'USD' THEN 'US'
              END
              AND q.quote_date > s.as_of
              AND q.quote_date <= (SELECT as_of FROM snapshot_view_cutoff)
              AND q.price > 0 AND q.price::text NOT IN ('NaN', 'Infinity', '-Infinity')
              AND q.return_status IN ('estimated', 'confirmed')
              AND q.raw_json @> '{{"valuation_projection_only":true}}'::jsonb
              AND s.quantity > 0 AND s.asset_liability = '资产' AND s.major_category = '美股'
            ORDER BY q.quote_date DESC, q.updated_at DESC, q.quote_id DESC LIMIT 1
        ) q ON TRUE
        UNION ALL
        SELECT s.id, gold_close.price, gold_close.quote_date, gold_close.updated_at,
               '{GOLD_CLOSE_SOURCE}' AS valuation_price_source
        FROM source_holdings s
        {gold_close_join("s", "(SELECT as_of FROM snapshot_view_cutoff)")}
        WHERE gold_close.quote_date IS NOT NULL
        {domestic}
    ), quote_account_dates AS (
        SELECT s.account_code, MAX(q.quote_date) AS as_of
        FROM source_holdings s JOIN quote_marks q ON q.id = s.id
        WHERE NOT {uses_gold_close("s")} GROUP BY s.account_code
    ), marked_holdings AS (
        SELECT s.*, q.price AS marked_price,
               CASE WHEN {uses_gold_close("s")} THEN {gold_market_value("s", "q")}
                    WHEN q.price IS NOT NULL THEN ROUND(s.quantity * q.price, 2)
                    ELSE s.market_value END AS marked_value,
               {holding_date} AS valuation_as_of,
               COALESCE(q.updated_at, s.updated_at) AS mark_updated_at
        FROM source_holdings s LEFT JOIN quote_marks q ON q.id = s.id
        LEFT JOIN quote_account_dates d ON d.account_code = s.account_code
    ), h AS (
        SELECT id, symbol, name, asset_liability, currency, account_code,
               major_category, minor_category, marked_value AS market_value, cost,
               CASE WHEN marked_price IS NOT NULL OR {uses_gold_close("m")}
                    THEN marked_value - cost ELSE holding_pnl END AS holding_pnl,
               valuation_as_of AS as_of, created_at, mark_updated_at AS updated_at,
               quantity, buy_price,
               CASE WHEN {uses_gold_close("m")} THEN marked_price ELSE COALESCE(marked_price, current_price) END AS current_price,
               asset_id
        FROM marked_holdings m
    )
    """
