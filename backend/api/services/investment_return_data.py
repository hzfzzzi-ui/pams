"""Data-access helpers for investment return calculations.

This module owns SQL reads and raw source normalization used by
``services.investment_returns``.  It intentionally does not own return formulas
or endpoint payload assembly.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date as Date, timedelta
from decimal import Decimal
from typing import Any

from services.investment_return_math import dec, ibkr_return_effective_date
from services.holdings_view import COMPLETE_ACCOUNT_SNAPSHOT_CTE
from services.valuation_nav import fx_rate_at


SECTION_RETURN_CONFIG: dict[str, dict[str, Any]] = {
    "a_stock": {
        "label": "A股个股",
        "major_category": "A股",
        "minor_category": "A股",
    },
    "a_fund": {
        "label": "A股基金",
        "major_category": "A股",
        "minor_category": "A股基金",
    },
    "us_stock": {
        "label": "美股",
        "major_category": "美股",
        "minor_category": None,
    },
    "precious_metal": {
        "label": "贵金属",
        "major_category": "贵金属",
        "minor_category": None,
    },
}

INVESTMENT_RETURN_CATEGORIES = ("A股", "美股", "贵金属", "ESOP")
INVESTMENT_RETURN_SNAPSHOT_EXCLUDE = {"tonghuashun", "huawei_stock", "ibkr", "ccb_cny"}
GOLD_PROXY_ASSET_IDS = ("BENCH_GOLD_SPOT_AU9999", "BENCH_GOLD_ETF_518880")
GOLD_PROXY_SYMBOLS = ("Au99.99.SGE", "Au99.99", "518880.SH", "sh518880")
GOLD_PROXY_SOURCE_PATTERNS = ("%Au99.99%", "%518880%")
GOLD_HOLDING_ASSET_IDS = ("ASSET_CCB_GOLD_ACCUM", "ASSET_CCB_GOLD_ACCUMULATION")
GOLD_HOLDING_SYMBOLS = ("CCB_GOLD_ACCUM", "GOLD", "XAU")
IBKR_EQUITY_MARKETS = (
    "STK",
    "\u7f8e\u80a1",
    "\u7f8e\u80a1\u57fa\u91d1",
    "\u6e2f\u80a1",
    "\u97e9\u80a1",
    "\u6b27\u80a1",
    "\u6d77\u5916\u4e2a\u80a1",
    "\u6d77\u5916ETF",
)


TRADE_FACT_MAJOR_EXPR = """
    COALESCE(
        NULLIF(asset_major_category, ''),
        NULLIF(raw_json #>> '{source_row,major_category}', ''),
        NULLIF(market, ''),
        trade_type
    )
"""

TRADE_FACT_MINOR_EXPR = """
    COALESCE(
        NULLIF(asset_minor_category, ''),
        NULLIF(raw_json #>> '{source_row,minor_category}', ''),
        NULLIF(market, ''),
        trade_type
    )
"""


def _reported_account_return_adjustments(
    cur,
    dates: list[Date],
    base_currency: str,
    *,
    reported_returns_cfg: dict[str, Any],
    owner_ratio_case_sql: Callable[[str], tuple[str, list[Any]]],
) -> dict[Date, Decimal]:
    """Return per-date PnL adjustments needed to match confirmed platform return anchors."""
    adjustments = {d: Decimal("0") for d in dates}
    account_perf = reported_returns_cfg.get("account_performance") or {}
    if not dates or not isinstance(account_perf, dict):
        return adjustments

    owner_sql, owner_params = owner_ratio_case_sql("h")
    categories = list(INVESTMENT_RETURN_CATEGORIES)
    for code, item in account_perf.items():
        if not isinstance(item, dict) or item.get("total_ytd_pnl") is None:
            continue
        as_of_raw = item.get("as_of") or reported_returns_cfg.get("as_of")
        try:
            anchor_date = Date.fromisoformat(str(as_of_raw))
        except (TypeError, ValueError):
            continue
        target_pnl = Decimal(str(item["total_ytd_pnl"]))
        cur.execute(
            f"""
            WITH {COMPLETE_ACCOUNT_SNAPSHOT_CTE}, latest AS (
                SELECT MAX(hs.as_of) AS as_of
                FROM pams_holdings_snapshot hs
                JOIN complete_account_snapshot cas
                  ON cas.account_code = hs.account_code
                 AND cas.as_of = hs.as_of
                WHERE hs.account_code = %s
                  AND hs.as_of <= %s
                  AND asset_liability = '资产'
                  AND major_category = ANY(%s::text[])
            )
            SELECT COALESCE(SUM(
                (h.market_value - COALESCE(h.cost, 0))
                * COALESCE(fx_hist.rate, fx_latest.rate, 1)
                * {owner_sql}
            ), 0) AS computed_pnl
            FROM pams_holdings_snapshot h
            JOIN latest l ON h.as_of = l.as_of
            LEFT JOIN LATERAL (
                SELECT rate
                FROM pams_fx_rates_daily fx
                WHERE fx.base_currency = %s
                  AND fx.quote_currency = h.currency
                  AND fx.as_of <= h.as_of
                ORDER BY fx.as_of DESC
                LIMIT 1
            ) fx_hist ON TRUE
            LEFT JOIN LATERAL (
                SELECT rate
                FROM pams_fx_rates_daily fx
                WHERE fx.base_currency = %s
                  AND fx.quote_currency = h.currency
                ORDER BY fx.as_of DESC
                LIMIT 1
            ) fx_latest ON TRUE
            WHERE h.account_code = %s
              AND h.asset_liability = '资产'
              AND h.major_category = ANY(%s::text[])
            """,
            (
                anchor_date,
                str(item.get("account_code") or code),
                anchor_date,
                categories,
                *owner_params,
                base_currency,
                base_currency,
                str(item.get("account_code") or code),
                categories,
            ),
        )
        row = cur.fetchone()
        computed_pnl = dec(row["computed_pnl"] if row else 0)
        delta = target_pnl - computed_pnl
        if abs(delta) < Decimal("0.005"):
            continue
        for sample_date in dates:
            if sample_date >= anchor_date:
                adjustments[sample_date] += delta
    return adjustments


def _tonghuashun_cost_series(
    cur,
    *,
    dates: list[Date],
    account_owner_ratio: Callable[[str], Decimal],
    account_initial_capital_cny: Callable[[str], Decimal | None],
) -> dict[Date, Decimal]:
    """Return the personal cumulative capital deposited into the A-share account."""
    values = {d: Decimal("0") for d in dates}
    owner_ratio = account_owner_ratio("tonghuashun")

    cur.execute(
        """
        WITH series_dates AS (
            SELECT unnest(%s::date[]) AS date
        ), deposits AS (
            SELECT
                trade_date AS event_date,
                COALESCE(
                    NULLIF(ABS(net_cash_cny), 0),
                    NULLIF(ABS(gross_amount) * fx_rate_to_cny, 0),
                    NULLIF(ABS(net_cash) * fx_rate_to_cny, 0),
                    0
                ) AS amount
            FROM pams_investment_trade_fact
            WHERE account_code = 'tonghuashun'
              AND LOWER(COALESCE(NULLIF(side, ''), trade_type)) IN ('deposit', 'cash_deposit', 'transfer_in')
              AND COALESCE(LOWER(review_status), 'confirmed') NOT IN ('cancelled', 'canceled', 'void')
        )
        SELECT d.date, COALESCE(SUM(dep.amount), 0) AS gross_cost_cny, COUNT(dep.event_date) AS event_count
        FROM series_dates d
        LEFT JOIN deposits dep ON dep.event_date <= d.date
        GROUP BY d.date
        ORDER BY d.date
        """,
        ([d.isoformat() for d in dates],),
    )
    has_deposits = False
    for row in cur.fetchall():
        values[row["date"]] = dec(row["gross_cost_cny"]) * owner_ratio
        has_deposits = has_deposits or int(row.get("event_count") or 0) > 0
    if not has_deposits:
        initial_capital = account_initial_capital_cny("tonghuashun")
        if initial_capital is None:
            return values
        return {d: initial_capital * owner_ratio for d in dates}
    return values


def _section_first_activity_date(cur, section: str) -> Date | None:
    """Return the first transaction or snapshot date available for an investment section."""
    cfg = SECTION_RETURN_CONFIG[section]
    if section == "a_stock":
        cur.execute(
            """
            SELECT MIN(trade_date) AS start_date
            FROM pams_investment_trade_fact
            WHERE account_code = 'tonghuashun'
              AND (
                trade_type IN ('buy', 'sell', 'dividend')
                OR LOWER(COALESCE(side, '')) IN ('deposit', 'withdraw', 'buy', 'sell', 'dividend')
              )
            """
        )
    elif section == "us_stock":
        cur.execute(
            """
            SELECT MIN(trade_date) AS start_date
            FROM pams_investment_trade_fact
            WHERE account_code = 'ibkr'
              AND COALESCE(NULLIF(market, ''), NULLIF(asset_minor_category, ''), NULLIF(asset_major_category, '')) = ANY(%s::text[])
              AND quantity IS NOT NULL
            """,
            (list(IBKR_EQUITY_MARKETS),),
        )
    else:
        minor = cfg.get("minor_category")
        minor_filter = f"AND {TRADE_FACT_MINOR_EXPR} = %s" if minor else ""
        params: list[Any] = [cfg["major_category"]]
        if minor:
            params.append(minor)
        cur.execute(
            f"""
            SELECT MIN(trade_date) AS start_date
            FROM pams_investment_trade_fact
            WHERE {TRADE_FACT_MAJOR_EXPR} = %s
              {minor_filter}
              AND gross_amount IS NOT NULL
            """,
            params,
        )
    row = cur.fetchone()
    return row["start_date"] if row and row.get("start_date") else None


def _snapshot_category_value_series(
    cur,
    *,
    dates: list[Date],
    base_currency: str,
    major_category: str,
    owner_ratio_case_sql: Callable[[str], tuple[str, list[Any]]],
    excluded_holding_accounts: set[str],
    minor_category: str | None = None,
    account_codes: list[str] | None = None,
) -> dict[Date, Decimal]:
    """Return latest-known holding value for a category on each sample date."""
    values = {d: Decimal("0") for d in dates}
    if not dates:
        return values

    owner_ratio_sql, owner_ratio_params = owner_ratio_case_sql("h")
    latest_filters = [
        "hs.asset_liability = %s",
        "hs.major_category = %s",
        "NOT (hs.account_code = ANY(%s::text[]))",
    ]
    latest_params: list[Any] = ["资产", major_category, sorted(excluded_holding_accounts)]
    h_filters = [
        "h.asset_liability = %s",
        "h.major_category = %s",
        "NOT (h.account_code = ANY(%s::text[]))",
    ]
    h_params: list[Any] = ["资产", major_category, sorted(excluded_holding_accounts)]

    if minor_category:
        latest_filters.append("hs.minor_category = %s")
        latest_params.append(minor_category)
        h_filters.append("h.minor_category = %s")
        h_params.append(minor_category)
    if account_codes:
        latest_filters.append("hs.account_code = ANY(%s::text[])")
        latest_params.append(account_codes)
        h_filters.append("h.account_code = ANY(%s::text[])")
        h_params.append(account_codes)

    cur.execute(
        f"""
        WITH series_dates AS (
            SELECT unnest(%s::date[]) AS date
        ), {COMPLETE_ACCOUNT_SNAPSHOT_CTE}, complete_holding_snapshot AS (
            SELECT hs.*
            FROM pams_holdings_snapshot hs
            JOIN complete_account_snapshot cas
              ON cas.account_code = hs.account_code
             AND cas.as_of = hs.as_of
        ), account_latest AS (
            SELECT d.date, hs.account_code, MAX(hs.as_of) AS as_of
            FROM series_dates d
            JOIN complete_holding_snapshot hs ON hs.as_of <= d.date
            WHERE {' AND '.join(latest_filters)}
            GROUP BY d.date, hs.account_code
        ), h AS (
            SELECT al.date, hs.*
            FROM account_latest al
            JOIN complete_holding_snapshot hs
              ON hs.account_code = al.account_code
             AND hs.as_of = al.as_of
        ), marked AS (
            SELECT
                h.date,
                h.market_value * COALESCE(fx_hist.rate, fx_latest.rate, 1) * {owner_ratio_sql} AS value_cny
            FROM h
            LEFT JOIN LATERAL (
                SELECT rate
                FROM pams_fx_rates_daily fx
                WHERE fx.base_currency = %s
                  AND fx.quote_currency = h.currency
                  AND fx.as_of <= h.date
                ORDER BY fx.as_of DESC
                LIMIT 1
            ) fx_hist ON TRUE
            LEFT JOIN LATERAL (
                SELECT rate
                FROM pams_fx_rates_daily fx
                WHERE fx.base_currency = %s
                  AND fx.quote_currency = h.currency
                ORDER BY fx.as_of DESC
                LIMIT 1
            ) fx_latest ON TRUE
            WHERE {' AND '.join(h_filters)}
        )
        SELECT date, COALESCE(SUM(value_cny), 0) AS value_cny
        FROM marked
        GROUP BY date
        ORDER BY date
        """,
        (
            [d.isoformat() for d in dates],
            dates[-1],
            *latest_params,
            *owner_ratio_params,
            base_currency,
            base_currency,
            *h_params,
        ),
    )
    for row in cur.fetchall():
        values[row["date"]] = dec(row["value_cny"])
    return values


def _apply_gold_proxy_to_precious_values(
    cur,
    values: dict[Date, Decimal],
    *,
    dates: list[Date],
    base_currency: str,
    owner_ratio_case_sql: Callable[[str], tuple[str, list[Any]]],
    excluded_holding_accounts: set[str],
    account_codes: list[str] | None = None,
) -> dict[Date, Decimal]:
    """Revalue local gold holdings with stored SGE spot gold, with ETF as fallback."""
    adjusted = dict(values)
    if not dates:
        return adjusted

    owner_ratio_sql, owner_ratio_params = owner_ratio_case_sql("h")
    latest_filters = [
        "hs.asset_liability = %s",
        "hs.major_category = %s",
        "NOT (hs.account_code = ANY(%s::text[]))",
        """
        (
            UPPER(COALESCE(hs.symbol, '')) = ANY(%s::text[])
            OR UPPER(COALESCE(hs.symbol, '')) LIKE %s
            OR hs.name ILIKE %s
            OR hs.asset_id = ANY(%s::text[])
        )
        """,
        "hs.quantity IS NOT NULL",
        "hs.quantity <> 0",
    ]
    latest_params: list[Any] = [
        "资产",
        "贵金属",
        sorted(excluded_holding_accounts),
        list(GOLD_HOLDING_SYMBOLS),
        "%GOLD%",
        "%黄金%",
        list(GOLD_HOLDING_ASSET_IDS),
    ]
    if account_codes:
        latest_filters.append("hs.account_code = ANY(%s::text[])")
        latest_params.append(account_codes)

    cur.execute(
        f"""
        WITH series_dates AS (
            SELECT unnest(%s::date[]) AS date
        ), {COMPLETE_ACCOUNT_SNAPSHOT_CTE}, complete_holding_snapshot AS (
            SELECT hs.*
            FROM pams_holdings_snapshot hs
            JOIN complete_account_snapshot cas
              ON cas.account_code = hs.account_code
             AND cas.as_of = hs.as_of
        ), gold_px AS (
            SELECT d.date, px.close_price
            FROM series_dates d
            LEFT JOIN LATERAL (
                SELECT COALESCE(close_price, unit_nav, estimated_unit_nav) AS close_price
                FROM pams_asset_market_daily px
                WHERE (
                    px.asset_id = ANY(%s::text[])
                    OR px.symbol = ANY(%s::text[])
                    OR px.source_key ILIKE ANY(%s::text[])
                )
                  AND px.as_of <= d.date
                  AND COALESCE(close_price, unit_nav, estimated_unit_nav) > 0
                ORDER BY
                    px.as_of DESC,
                    CASE WHEN px.asset_id = 'BENCH_GOLD_SPOT_AU9999' THEN 0 ELSE 1 END
                LIMIT 1
            ) px ON TRUE
        ), account_latest AS (
            SELECT
                d.date,
                hs.account_code,
                COALESCE(NULLIF(hs.symbol, ''), hs.name) AS holding_key,
                MAX(hs.as_of) AS as_of
            FROM series_dates d
            JOIN complete_holding_snapshot hs ON hs.as_of <= d.date
            WHERE {' AND '.join(latest_filters)}
            GROUP BY d.date, hs.account_code, COALESCE(NULLIF(hs.symbol, ''), hs.name)
        ), h AS (
            SELECT al.date, hs.*
            FROM account_latest al
            JOIN complete_holding_snapshot hs
              ON hs.account_code = al.account_code
             AND hs.as_of = al.as_of
             AND COALESCE(NULLIF(hs.symbol, ''), hs.name) = al.holding_key
        ), owned AS (
            SELECT
                h.date,
                h.currency,
                h.market_value,
                h.quantity,
                gp.close_price,
                {owner_ratio_sql} AS owner_ratio
            FROM h
            JOIN gold_px gp
              ON gp.date = h.date
             AND gp.close_price IS NOT NULL
        ), marked AS (
            SELECT
                owned.date,
                owned.market_value * COALESCE(fx_hist.rate, fx_latest.rate, 1) * owned.owner_ratio AS snapshot_value_cny,
                owned.quantity * owned.close_price * COALESCE(fx_hist.rate, fx_latest.rate, 1) * owned.owner_ratio AS proxy_value_cny
            FROM owned
            LEFT JOIN LATERAL (
                SELECT rate
                FROM pams_fx_rates_daily fx
                WHERE fx.base_currency = %s
                  AND fx.quote_currency = owned.currency
                  AND fx.as_of <= owned.date
                ORDER BY fx.as_of DESC
                LIMIT 1
            ) fx_hist ON TRUE
            LEFT JOIN LATERAL (
                SELECT rate
                FROM pams_fx_rates_daily fx
                WHERE fx.base_currency = %s
                  AND fx.quote_currency = owned.currency
                ORDER BY fx.as_of DESC
                LIMIT 1
            ) fx_latest ON TRUE
        )
        SELECT
            date,
            COALESCE(SUM(snapshot_value_cny), 0) AS snapshot_value_cny,
            COALESCE(SUM(proxy_value_cny), 0) AS proxy_value_cny,
            COUNT(*) AS priced_count
        FROM marked
        GROUP BY date
        ORDER BY date
        """,
        (
            [d.isoformat() for d in dates],
            dates[-1],
            list(GOLD_PROXY_ASSET_IDS),
            list(GOLD_PROXY_SYMBOLS),
            list(GOLD_PROXY_SOURCE_PATTERNS),
            *latest_params,
            *owner_ratio_params,
            base_currency,
            base_currency,
        ),
    )
    for row in cur.fetchall():
        if int(row.get("priced_count") or 0) <= 0:
            continue
        sample_date = row["date"]
        snapshot_value = dec(row.get("snapshot_value_cny"))
        proxy_value = dec(row.get("proxy_value_cny"))
        adjusted[sample_date] = dec(adjusted.get(sample_date)) - snapshot_value + proxy_value
    return adjusted


def _exact_snapshot_category_value(
    cur,
    *,
    as_of: Date,
    base_currency: str,
    major_category: str,
    owner_ratio_case_sql: Callable[[str], tuple[str, list[Any]]],
    excluded_holding_accounts: set[str],
    minor_category: str | None = None,
    account_codes: list[str] | None = None,
) -> Decimal | None:
    """Return same-date holding value for a category, without carrying prior snapshots forward."""
    filters = [
        "h.as_of = %s",
        "h.asset_liability = %s",
        "h.major_category = %s",
        "NOT (h.account_code = ANY(%s::text[]))",
    ]
    params: list[Any] = [as_of, "资产", major_category, sorted(excluded_holding_accounts)]
    if minor_category:
        filters.append("h.minor_category = %s")
        params.append(minor_category)
    if account_codes:
        filters.append("h.account_code = ANY(%s::text[])")
        params.append(account_codes)

    owner_ratio_sql, owner_ratio_params = owner_ratio_case_sql("h")
    cur.execute(
        f"""
        WITH {COMPLETE_ACCOUNT_SNAPSHOT_CTE}
        SELECT COALESCE(SUM(
            h.market_value * COALESCE(fx_hist.rate, fx_latest.rate, 1) * {owner_ratio_sql}
        ), 0) AS value_cny,
        COUNT(*) AS row_count
        FROM pams_holdings_snapshot h
        JOIN complete_account_snapshot cas
          ON cas.account_code = h.account_code
         AND cas.as_of = h.as_of
        LEFT JOIN LATERAL (
            SELECT rate
            FROM pams_fx_rates_daily fx
            WHERE fx.base_currency = %s
              AND fx.quote_currency = h.currency
              AND fx.as_of <= h.as_of
            ORDER BY fx.as_of DESC
            LIMIT 1
        ) fx_hist ON TRUE
        LEFT JOIN LATERAL (
            SELECT rate
            FROM pams_fx_rates_daily fx
            WHERE fx.base_currency = %s
              AND fx.quote_currency = h.currency
            ORDER BY fx.as_of DESC
            LIMIT 1
        ) fx_latest ON TRUE
        WHERE {' AND '.join(filters)}
        """,
        (
            as_of,
            *owner_ratio_params,
            base_currency,
            base_currency,
            *params,
        ),
    )
    row = cur.fetchone()
    if not row or int(row.get("row_count") or 0) <= 0:
        return None
    return dec(row.get("value_cny"))



def _investment_transaction_flow_events(
    cur,
    *,
    start_date: Date,
    end_date: Date,
    base_currency: str,
    fx_timelines: dict[str, list[tuple[Date, Decimal]]],
    account_owner_ratio: Callable[[str], Decimal],
    major_category: str,
    minor_category: str | None = None,
    account_codes: list[str] | None = None,
) -> list[tuple[Date, Decimal]]:
    """Return section-level investment cash flows that should be excluded from returns."""
    filters = [
        "trade_date >= %s",
        "trade_date <= %s",
        f"{TRADE_FACT_MAJOR_EXPR} = %s",
        "gross_amount IS NOT NULL",
        "COALESCE(LOWER(review_status), 'confirmed') NOT IN ('cancelled', 'canceled', 'void', 'pending', 'pending_settlement', 'in_transit')",
    ]
    params: list[Any] = [start_date, end_date, major_category]
    if minor_category:
        filters.append(f"{TRADE_FACT_MINOR_EXPR} = %s")
        params.append(minor_category)
    if account_codes:
        filters.append("account_code = ANY(%s::text[])")
        params.append(account_codes)

    coverage_filters = [
        "hs.account_code = pams_investment_trade_fact.account_code",
        "hs.as_of BETWEEN pams_investment_trade_fact.trade_date - INTERVAL '10 days' AND pams_investment_trade_fact.trade_date",
        "hs.asset_liability = %s",
        "hs.major_category = %s",
        "COALESCE(hs.market_value, 0) <> 0",
    ]
    coverage_params: list[Any] = ["资产", major_category]
    if minor_category:
        coverage_filters.append("hs.minor_category = %s")
        coverage_params.append(minor_category)
    filters.append(
        f"""
        EXISTS (
            SELECT 1
            FROM pams_holdings_snapshot hs
            WHERE {' AND '.join(coverage_filters)}
        )
        """
    )
    params.extend(coverage_params)

    cur.execute(
        f"""
        SELECT
            account_code,
            trade_date,
            COALESCE(NULLIF(side, ''), trade_type) AS side,
            gross_amount,
            COALESCE(fee_amount, 0) + COALESCE(tax_amount, 0) AS fees,
            currency
        FROM pams_investment_trade_fact
        WHERE {' AND '.join(filters)}
        ORDER BY trade_date, trade_id
        """,
        params,
    )

    positive_sides = {"buy", "subscribe", "purchase", "deposit", "transfer_in", "in"}
    negative_sides = {"sell", "redeem", "redemption", "withdraw", "withdrawal", "cancel", "transfer_out", "out"}
    events: list[tuple[Date, Decimal]] = []
    for row in cur.fetchall():
        side = str(row.get("side") or "").strip().lower()
        amount = abs(dec(row.get("gross_amount")))
        fees = abs(dec(row.get("fees")))
        if side in positive_sides:
            signed_native = amount + fees
        elif side in negative_sides:
            signed_native = -(amount - fees)
        else:
            continue
        trade_date = row["trade_date"]
        currency = str(row.get("currency") or base_currency).upper()
        signed = (
            signed_native
            * fx_rate_at(fx_timelines, currency, trade_date, base_currency)
            * account_owner_ratio(str(row.get("account_code") or ""))
        )
        events.append((trade_date, signed))
    return events


def _tonghuashun_external_flow_events(
    cur,
    *,
    start_date: Date,
    end_date: Date,
    base_currency: str,
    fx_timelines: dict[str, list[tuple[Date, Decimal]]],
    account_owner_ratio: Callable[[str], Decimal],
) -> list[tuple[Date, Decimal]]:
    """Return external capital flows for the Tonghuashun account."""
    cur.execute(
        """
        SELECT trade_date, COALESCE(NULLIF(side, ''), trade_type) AS side, gross_amount, net_cash, currency
        FROM pams_investment_trade_fact
        WHERE account_code = 'tonghuashun'
          AND trade_date >= %s
          AND trade_date <= %s
          AND LOWER(COALESCE(NULLIF(side, ''), trade_type)) IN ('deposit', 'withdraw', 'cash_deposit', 'cash_withdrawal', 'transfer_in', 'transfer_out')
          AND COALESCE(LOWER(review_status), 'confirmed') NOT IN ('cancelled', 'canceled', 'void')
        ORDER BY trade_date, trade_id
        """,
        (start_date, end_date),
    )
    events: list[tuple[Date, Decimal]] = []
    owner_ratio = account_owner_ratio("tonghuashun")
    for row in cur.fetchall():
        side = str(row.get("side") or "").strip().lower()
        amount = abs(dec(row.get("gross_amount")) or dec(row.get("net_cash")))
        signed_native = -amount if side in {"withdraw", "cash_withdrawal", "transfer_out"} else amount
        trade_date = row["trade_date"]
        currency = str(row.get("currency") or base_currency).upper()
        signed = signed_native * fx_rate_at(fx_timelines, currency, trade_date, base_currency) * owner_ratio
        events.append((trade_date, signed))
    return events


def _ibkr_trade_flow_events(
    cur,
    *,
    start_date: Date,
    end_date: Date,
    base_currency: str,
    fx_timelines: dict[str, list[tuple[Date, Decimal]]],
) -> list[tuple[Date, Decimal]]:
    """Return stock/ETF trade cash flows for the IBKR section."""
    cur.execute(
        """
        SELECT
            trade_date,
            quantity,
            gross_amount,
            COALESCE(fee_amount, 0) + COALESCE(tax_amount, 0) AS commission,
            net_cash,
            currency
        FROM pams_investment_trade_fact
        WHERE account_code = 'ibkr'
          AND COALESCE(NULLIF(market, ''), NULLIF(asset_minor_category, ''), NULLIF(asset_major_category, '')) = ANY(%s::text[])
          AND quantity IS NOT NULL
          AND trade_date >= %s
          AND trade_date <= %s
          AND COALESCE(LOWER(review_status), 'confirmed') NOT IN ('cancelled', 'canceled', 'void')
        ORDER BY trade_date, trade_id
        """,
        (list(IBKR_EQUITY_MARKETS), start_date - timedelta(days=1), end_date),
    )
    events: list[tuple[Date, Decimal]] = []
    for row in cur.fetchall():
        qty = dec(row.get("quantity"))
        gross = abs(dec(row.get("gross_amount")))
        commission = abs(dec(row.get("commission")))
        net_cash = dec(row.get("net_cash"))
        if qty > 0:
            signed_native = abs(net_cash) if net_cash != 0 else gross + commission
        elif qty < 0:
            signed_native = -(net_cash if net_cash > 0 else max(gross - commission, Decimal("0")))
        else:
            continue
        currency = str(row.get("currency") or "USD").upper()
        trade_date = ibkr_return_effective_date(row["trade_date"], currency)
        if trade_date < start_date or trade_date > end_date:
            continue
        signed = signed_native * fx_rate_at(fx_timelines, currency, trade_date, base_currency)
        events.append((trade_date, signed))
    return events
