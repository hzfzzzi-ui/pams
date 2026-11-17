"""Dated, read-only estimates for accounts updated by manual import."""

from collections import defaultdict
from datetime import date
from decimal import Decimal

from domain.accounts.balance_reference import historical_account_balance
from domain.accounts.position_reference import historical_position_reference, position_reference_config
from domain.investments.fund_return_history import NAV_SOURCES
from domain.investments.snapshot_coverage import load_account_snapshot_coverage
from domain.market.quote_valuation import gold_close_join, gold_market_value, uses_gold_close


def manual_nav_accounts(config):
    """Select accounts by how their positions are updated."""
    return {code: account for code, account in (config or {}).get("accounts", {}).items()
            if account.get("nav_position_policy") == "carry_last_observed"}


def load_manual_nav_estimates(cur, *, accounts, dates, owner_ratio_case_sql, account_owner_ratio):
    """Read the newest observed member at or before each day and mark it with dated prices."""
    if not accounts or not dates:
        return {}
    # Cash amounts and gold quantities require recorded snapshot members.
    cur.execute(f"""
        SELECT s.account_code, s.as_of, s.id FROM pams_holdings_snapshot s
        WHERE s.account_code=ANY(%s) AND s.as_of<=%s
          AND (s.major_category='现金' OR {uses_gold_close("s")})
    """, (sorted(accounts), max(dates)))
    checked_rows = cur.fetchall()
    coverage = load_account_snapshot_coverage(
        cur, {(row["account_code"], row["as_of"]) for row in checked_rows},
    )
    recorded_ids = [
        row["id"] for row in checked_rows
        if row["id"] in coverage.get((row["account_code"], row["as_of"]), {}).get("member_ids", ())
    ]
    ratio_sql, ratio_params = owner_ratio_case_sql("h")
    cur.execute(f"""
        WITH samples AS (SELECT unnest(%s::date[]) AS date),
        members AS (
            SELECT DISTINCT ON (d.date, s.account_code, s.currency,
                       CASE WHEN s.major_category='现金' THEN NULL ELSE COALESCE(NULLIF(s.symbol, ''), s.name) END)
                   d.date, s.*
            FROM samples d JOIN pams_holdings_snapshot s ON s.as_of <= d.date
            WHERE s.account_code = ANY(%s)
              AND ((s.major_category <> '现金' AND NOT {uses_gold_close("s")})
                   OR s.id=ANY(%s::bigint[]))
            ORDER BY d.date, s.account_code, s.currency,
                     CASE WHEN s.major_category='现金' THEN NULL ELSE COALESCE(NULLIF(s.symbol, ''), s.name) END,
                     s.as_of DESC, s.id DESC
        )
        SELECT h.date,h.account_code,h.as_of,
               (CASE
                    WHEN h.currency <> 'CNY' THEN NULL
                    WHEN h.asset_liability='负债' THEN -ABS(h.market_value)
                    WHEN h.major_category='现金' THEN h.market_value
                    WHEN {uses_gold_close("h")} THEN {gold_market_value("h")}
                    WHEN h.quantity IS NOT NULL AND fund.price IS NOT NULL
                        THEN ROUND(h.quantity * fund.price, 2)
                    WHEN h.quantity IS NOT NULL AND equity.close IS NOT NULL
                        THEN ROUND(h.quantity * equity.close, 2)
                    ELSE h.market_value
                END) * {ratio_sql} AS amount
        FROM members h
        {gold_close_join("h", "h.date")}
        LEFT JOIN LATERAL (
            SELECT MIN(unit_nav) AS price
            FROM market_observations
            WHERE observation_type='daily' AND market='基金' AND currency='CNY'
              AND h.major_category='A股' AND h.minor_category='A股基金'
              AND symbol=h.symbol AND source=ANY(%s)
              AND as_of BETWEEN h.as_of AND h.date
              AND unit_nav>0 AND unit_nav::text NOT IN ('NaN','Infinity','-Infinity')
            GROUP BY as_of
            HAVING COUNT(DISTINCT unit_nav)=1
            ORDER BY as_of DESC LIMIT 1
        ) fund ON TRUE
        LEFT JOIN LATERAL (
            SELECT close FROM pams_ohlcv_daily
            WHERE market='CN_A' AND h.major_category='A股' AND h.minor_category='A股'
              AND symbol=CASE WHEN h.symbol ~ '^[0-9]{{6}}$'
                THEN (CASE WHEN LEFT(h.symbol,1)='6' THEN 'sh' ELSE 'sz' END) || h.symbol
                ELSE h.symbol END
              AND trade_date BETWEEN h.as_of AND h.date
              AND close>0 AND close::text NOT IN ('NaN','Infinity','-Infinity')
            ORDER BY trade_date DESC LIMIT 1
        ) equity ON TRUE
    """, (dates, sorted(accounts), recorded_ids, *ratio_params, list(NAV_SOURCES)))
    observed = set()
    amounts = defaultdict(list)
    for row in cur.fetchall():
        day, code, source_day = row["date"], row["account_code"], row["as_of"]
        observed.add((day, code))
        if row["amount"] is None or not row["amount"].is_finite():
            continue
        amounts[day, code].append((row["amount"], source_day))

    for code, cfg in accounts.items():
        for day in dates:
            if (day, code) in observed:
                continue
            owner_ratio = account_owner_ratio(code)
            reference = historical_account_balance(cfg, day, owner_ratio)
            if not reference and position_reference_config(cfg) and (cfg.get("manual_balance") or {}).get("source"):
                reference = historical_position_reference(cfg, day, owner_ratio)
            if reference:
                amounts[day, code].append((Decimal(reference["balance_amount"]), reference["balance_as_of"]))

    estimates: dict[date, dict[str, tuple[Decimal, date, date]]] = {}
    for (day, code), members in amounts.items():
        source_days = [source_day for _, source_day in members]
        estimates.setdefault(day, {})[code] = (
            sum((amount for amount, _ in members), Decimal(0)),
            min(source_days),
            max(source_days),
        )
    return estimates
