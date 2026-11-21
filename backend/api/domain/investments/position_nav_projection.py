"""Dated NAV estimates from observed holdings and market valuations."""

from collections import defaultdict
from datetime import date
from decimal import Decimal

from domain.accounts.balance_reference import historical_account_balance
from domain.accounts.position_reference import historical_position_reference, position_reference_config
from domain.investments.fund_return_history import NAV_SOURCES
from domain.investments.snapshot_coverage import load_account_snapshot_coverage
from domain.market.quote_valuation import gold_close_join, gold_market_value, uses_gold_close


def load_position_nav_estimates(cur, *, accounts, dates, owner_ratio_case_sql, account_owner_ratio, rate_at):
    """Read the latest holding for each member, independently of the quote date."""
    if not accounts or not dates:
        return {}
    # Partial observations update individual positions. A verified complete
    # snapshot replaces the prior set, so an omitted position stays closed.
    cur.execute("""
        SELECT s.account_code, s.as_of,
               ARRAY_AGG(s.id) FILTER (WHERE s.major_category='现金') AS cash_ids
        FROM pams_holdings_snapshot s
        WHERE s.account_code=ANY(%s) AND s.as_of<=%s
        GROUP BY s.account_code,s.as_of
    """, (sorted(accounts), max(dates)))
    observed_days = {(row["account_code"], row["as_of"]): row for row in cur.fetchall()}
    coverage = load_account_snapshot_coverage(cur, set(observed_days))
    cash_ids = []
    complete_days = []
    point_in_time_ids = []
    invalid_point_ids = []
    for pair, proof in coverage.items():
        cash_ids.extend(set(observed_days[pair]["cash_ids"] or ()) & set(proof.get("member_ids", ())))
        if proof.get("status") == "complete":
            complete_days.append(pair)
        point_ids = set(proof.get("point_in_time_member_ids", ()))
        point_in_time_ids.extend(point_ids)
        invalid_point_ids.extend(point_ids - set(proof.get("member_ids", ())))
    ratio_sql, ratio_params = owner_ratio_case_sql("h")
    cur.execute(f"""
        WITH samples AS (SELECT unnest(%s::date[]) AS date),
        complete_days AS (
            SELECT * FROM unnest(%s::text[], %s::date[]) AS scope(account_code, as_of)
        ),
        scope_cutoffs AS (
            SELECT d.date, scope.account_code, MAX(scope.as_of) AS as_of
            FROM samples d JOIN complete_days scope ON scope.as_of <= d.date
            GROUP BY d.date, scope.account_code
        ),
        members AS (
            SELECT DISTINCT ON (d.date, s.account_code, s.currency, s.asset_liability,
                       CASE WHEN s.major_category='现金' THEN NULL ELSE COALESCE(NULLIF(s.symbol, ''), s.name) END)
                   d.date, s.*
            FROM samples d JOIN pams_holdings_snapshot s ON s.as_of <= d.date
            LEFT JOIN scope_cutoffs scope ON scope.date=d.date AND scope.account_code=s.account_code
            WHERE s.account_code = ANY(%s) AND (scope.as_of IS NULL OR s.as_of>=scope.as_of)
              AND (s.major_category <> '现金' OR s.id=ANY(%s::bigint[]))
              AND (s.id<>ALL(%s::bigint[]) OR s.as_of=d.date)
              AND s.id<>ALL(%s::bigint[])
            ORDER BY d.date, s.account_code, s.currency, s.asset_liability,
                     CASE WHEN s.major_category='现金' THEN NULL ELSE COALESCE(NULLIF(s.symbol, ''), s.name) END,
                     s.as_of DESC, s.id DESC
        )
        SELECT h.date,h.account_code,h.as_of,h.currency,
               (CASE
                    WHEN h.asset_liability='负债' THEN -ABS(h.market_value)
                    WHEN h.major_category='现金' THEN h.market_value
                    WHEN h.quantity=0 THEN 0
                    WHEN {uses_gold_close("h")} THEN {gold_market_value("h")}
                    WHEN h.quantity IS NOT NULL AND fund.price IS NOT NULL
                        THEN ROUND(h.quantity * fund.price, 2)
                    WHEN h.quantity IS NOT NULL AND equity.close IS NOT NULL
                        THEN ROUND(h.quantity * equity.close, 2)
                    WHEN h.quantity IS NOT NULL AND overseas.close IS NOT NULL
                        THEN ROUND(h.quantity * overseas.close, 2)
                    ELSE h.market_value
                END) * {ratio_sql} AS amount
        FROM members h
        {gold_close_join("h", "h.date")}
        LEFT JOIN LATERAL (
            SELECT MIN(unit_nav) AS price
            FROM market_observations
            WHERE observation_type='daily' AND market='基金' AND currency='CNY'
              AND h.currency='CNY' AND h.major_category='A股' AND h.minor_category='A股基金'
              AND symbol=h.symbol AND source=ANY(%s)
              AND as_of BETWEEN h.as_of AND h.date
              AND unit_nav>0 AND unit_nav::text NOT IN ('NaN','Infinity','-Infinity')
            GROUP BY as_of
            HAVING COUNT(DISTINCT unit_nav)=1
            ORDER BY as_of DESC LIMIT 1
        ) fund ON TRUE
        LEFT JOIN LATERAL (
            SELECT close FROM pams_ohlcv_daily
            WHERE market='CN_A' AND h.currency='CNY' AND h.major_category='A股' AND h.minor_category='A股'
              AND symbol=CASE WHEN h.symbol ~ '^[0-9]{{6}}$'
                THEN (CASE WHEN LEFT(h.symbol,1)='6' THEN 'sh' ELSE 'sz' END) || h.symbol
                ELSE h.symbol END
              AND trade_date BETWEEN h.as_of AND h.date
              AND close>0 AND close::text NOT IN ('NaN','Infinity','-Infinity')
            ORDER BY trade_date DESC LIMIT 1
        ) equity ON TRUE
        LEFT JOIN LATERAL (
            SELECT close FROM pams_ohlcv_daily
            WHERE market='YF' AND h.major_category='美股' AND symbol=h.symbol
              AND trade_date BETWEEN h.as_of AND h.date
              AND close>0 AND close::text NOT IN ('NaN','Infinity','-Infinity')
            ORDER BY trade_date DESC LIMIT 1
        ) overseas ON TRUE
    """, (dates, [code for code, _ in complete_days], [day for _, day in complete_days],
           sorted(accounts), cash_ids, point_in_time_ids, invalid_point_ids, *ratio_params, list(NAV_SOURCES)))
    observed = set()
    amounts = defaultdict(list)
    rates = {}

    def dated_rate(currency, day):
        key = (currency, day)
        if key not in rates:
            rates[key] = rate_at(currency, day)
        return rates[key]

    for row in cur.fetchall():
        day, code, source_day = row["date"], row["account_code"], row["as_of"]
        observed.add((day, code))
        if row["amount"] is None or not row["amount"].is_finite():
            continue
        rate = dated_rate(row["currency"], day)
        if row["amount"] == 0:
            amounts[day, code].append((Decimal(0), source_day))
        elif rate is not None:
            amounts[day, code].append((row["amount"] * rate, source_day))

    for code, cfg in accounts.items():
        missing_days = [day for day in dates if (day, code) not in observed]
        if not missing_days:
            continue
        owner_ratio = account_owner_ratio(code)
        for day in missing_days:
            reference = historical_account_balance(cfg, day, owner_ratio)
            if not reference and position_reference_config(cfg) and (cfg.get("manual_balance") or {}).get("source"):
                reference = historical_position_reference(cfg, day, owner_ratio)
            if reference:
                amount = Decimal(reference["balance_amount"])
                rate = dated_rate(reference["balance_currency"], day)
                if amount == 0 or rate is not None:
                    amounts[day, code].append((amount if amount == 0 else amount * rate, reference["balance_as_of"]))

    estimates: dict[date, dict[str, tuple[Decimal, date, date]]] = {}
    for (day, code), members in amounts.items():
        source_days = [source_day for _, source_day in members]
        estimates.setdefault(day, {})[code] = (
            sum((amount for amount, _ in members), Decimal(0)),
            min(source_days),
            max(source_days),
        )
    return estimates
