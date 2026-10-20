"""Holdings row helpers and closed-position queries.

These helpers are kept separate from the public holdings payload builders so
`holdings_view.py` stays focused on endpoint adapter orchestration.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

def _norm(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value

def _dec(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    return Decimal(str(value))

def _owner_ratio(
    account_code: str,
    *,
    owner_ratios: dict[str, Decimal],
    default_owner_ratio: Decimal,
) -> Decimal:
    return owner_ratios.get(str(account_code), default_owner_ratio)

def _apply_owner_ratio(
    row: dict[str, Any],
    *,
    owner_ratios: dict[str, Decimal],
    default_owner_ratio: Decimal,
) -> dict[str, Any]:
    out = dict(row)
    ratio = _owner_ratio(
        str(out.get("account") or out.get("account_code") or ""),
        owner_ratios=owner_ratios,
        default_owner_ratio=default_owner_ratio,
    )
    out["owner_ratio"] = ratio
    for key in (
        "market_value",
        "cost",
        "holding_pnl",
        "daily_pnl",
        "daily_fx_pnl",
        "daily_total_pnl",
        "daily_price_pnl",
        "realized_trade_pnl",
        "daily_realized_trade_pnl",
    ):
        cny_key = f"{key}_cny"
        if cny_key in out:
            out[f"{key}_gross_cny"] = out[cny_key]
            out[cny_key] = _dec(out[cny_key]) * ratio
    return out

def _holding_key(row: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        str(row.get("account") or row.get("account_code") or ""),
        str(row.get("symbol") or ""),
        str(row.get("name") or ""),
        str(row.get("major_category") or ""),
        str(row.get("minor_category") or row.get("type") or ""),
    )

def _filter_current_holding_rows(
    rows: list[dict[str, Any]],
    *,
    excluded_accounts: set[str],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        account = str(row.get("account") or row.get("account_code") or "")
        if account in excluded_accounts:
            continue
        market_value = _dec(row.get("market_value_cny", row.get("market_value")))
        asset_liability = str(row.get("asset_liability") or "").lower()
        is_liability = asset_liability in {"负债", "liability", "debt"}
        if is_liability:
            if abs(market_value) <= Decimal("0.0001"):
                continue
        elif market_value <= Decimal("0.0001"):
            continue
        row["is_closed"] = False
        out.append(row)
    return out

def _enrich_esop_realized_income(cur, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add confirmed ESOP dividends to current holding PnL without changing position cost."""
    if not rows:
        return rows

    cur.execute(
        """
        WITH fx AS (
            SELECT DISTINCT ON (quote_currency) quote_currency, rate
            FROM pams_fx_rates_daily
            WHERE base_currency = 'CNY'
            ORDER BY quote_currency, as_of DESC
        ), tx AS (
            SELECT
                account_code,
                COALESCE(
                    NULLIF(asset_major_category, ''),
                    NULLIF(raw_json #>> '{source_row,major_category}', ''),
                    NULLIF(market, ''),
                    trade_type
                ) AS major_category,
                COALESCE(
                    NULLIF(asset_minor_category, ''),
                    NULLIF(raw_json #>> '{source_row,minor_category}', ''),
                    NULLIF(market, ''),
                    trade_type
                ) AS minor_category,
                COALESCE(symbol, '') AS symbol,
                currency,
                trade_type,
                side,
                ABS(COALESCE(gross_amount, net_cash, 0)) AS gross_amount,
                COALESCE(fx_rate_to_cny, 1) AS fx_rate_to_cny,
                review_status
            FROM pams_investment_trade_fact
        )
        SELECT
            tx.account_code,
            tx.major_category,
            tx.minor_category,
            COALESCE(tx.symbol, '') AS symbol,
            tx.currency,
            SUM(tx.gross_amount) AS realized_income,
            SUM(tx.gross_amount * COALESCE(tx.fx_rate_to_cny, fx.rate, 1)) AS realized_income_cny
        FROM tx
        LEFT JOIN fx ON fx.quote_currency = tx.currency
        WHERE tx.major_category = 'ESOP'
          AND LOWER(COALESCE(NULLIF(tx.trade_type, ''), tx.side, '')) IN ('dividend', 'interest', 'income', 'distribution', 'coupon')
          AND tx.gross_amount IS NOT NULL
          AND COALESCE(LOWER(tx.review_status), 'confirmed') NOT IN ('cancelled', 'canceled', 'void')
        GROUP BY tx.account_code, tx.major_category, tx.minor_category, COALESCE(tx.symbol, ''), tx.currency
        """,
    )

    income_by_key: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for raw in cur.fetchall():
        item = dict(raw)
        key = (
            str(item.get("account_code") or ""),
            str(item.get("major_category") or ""),
            str(item.get("minor_category") or ""),
            str(item.get("symbol") or ""),
        )
        bucket = income_by_key.setdefault(
            key,
            {"realized_income_cny": Decimal("0"), "by_currency": {}},
        )
        bucket["realized_income_cny"] += _dec(item.get("realized_income_cny"))
        bucket["by_currency"][str(item.get("currency") or "")] = (
            bucket["by_currency"].get(str(item.get("currency") or ""), Decimal("0"))
            + _dec(item.get("realized_income"))
        )

    for row in rows:
        key = (
            str(row.get("account") or row.get("account_code") or ""),
            str(row.get("major_category") or ""),
            str(row.get("minor_category") or row.get("type") or ""),
            str(row.get("symbol") or ""),
        )
        income = income_by_key.get(key)
        if not income:
            continue

        row_currency = str(row.get("currency") or "")
        realized_native = income["by_currency"].get(row_currency, Decimal("0"))
        realized_cny = _dec(income.get("realized_income_cny"))

        row["holding_pnl_before_income"] = row.get("holding_pnl")
        row["holding_pnl_before_income_native"] = row.get("holding_pnl_native", row.get("holding_pnl"))
        row["holding_pnl_before_income_cny"] = row.get("holding_pnl_cny", row.get("holding_pnl"))
        row["realized_income"] = realized_native
        row["realized_income_native"] = realized_native
        row["realized_income_cny"] = realized_cny
        row["holding_pnl"] = _dec(row.get("holding_pnl")) + realized_native
        row["holding_pnl_native"] = _dec(row.get("holding_pnl_native", row.get("holding_pnl"))) + realized_native
        row["holding_pnl_cny"] = _dec(row.get("holding_pnl_cny")) + realized_cny

    return rows

def _table_exists(cur, table_name: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL AS exists", (f"public.{table_name}",))
    row = cur.fetchone() or {}
    if isinstance(row, dict):
        return bool(row.get("exists"))
    if isinstance(row, (list, tuple)) and row:
        return bool(row[0])
    return False

def _money_close(left: Any, right: Any, tolerance: Decimal = Decimal("0.05")) -> bool:
    return abs(_dec(left) - _dec(right)) <= tolerance

def _trade_cash_outlay(row: dict[str, Any]) -> Decimal:
    net_cash = _dec(row.get("net_cash"))
    if net_cash < 0:
        return -net_cash
    return abs(_dec(row.get("gross_amount"))) + abs(_dec(row.get("fee_amount"))) + abs(_dec(row.get("tax_amount")))

def _trade_cash_proceeds(row: dict[str, Any]) -> Decimal:
    net_cash = _dec(row.get("net_cash"))
    if net_cash > 0:
        return net_cash
    return abs(_dec(row.get("gross_amount"))) - abs(_dec(row.get("fee_amount"))) - abs(_dec(row.get("tax_amount")))

def _replay_weighted_average_trade_pnl(
    trades: list[dict[str, Any]],
    *,
    as_of_key: str,
) -> dict[str, Decimal]:
    quantity = Decimal("0")
    cost = Decimal("0")
    cost_cny = Decimal("0")
    realized = Decimal("0")
    realized_cny = Decimal("0")
    daily_realized = Decimal("0")
    daily_realized_cny = Decimal("0")

    for trade in sorted(trades, key=lambda item: (str(item.get("trade_date"))[:10], _dec(item.get("trade_id")))):
        trade_date_key = str(trade.get("trade_date"))[:10]
        if trade_date_key > as_of_key:
            continue

        trade_type = str(trade.get("trade_type") or "").lower()
        trade_quantity = abs(_dec(trade.get("quantity")))
        if trade_quantity <= 0:
            continue

        fx_rate = _dec(trade.get("fx_rate_to_cny"),) or Decimal("1")
        if trade_type in {"buy", "fund_subscription"}:
            outlay = _trade_cash_outlay(trade)
            quantity += trade_quantity
            cost += outlay
            cost_cny += outlay * fx_rate
            continue

        if trade_type not in {"sell", "fund_redemption"} or quantity <= 0:
            continue

        basis_quantity = min(trade_quantity, quantity)
        basis = cost / quantity * basis_quantity if quantity else Decimal("0")
        basis_cny = cost_cny / quantity * basis_quantity if quantity else Decimal("0")
        proceeds = _trade_cash_proceeds(trade)
        trade_realized = proceeds - basis
        trade_realized_cny = proceeds * fx_rate - basis_cny
        realized += trade_realized
        realized_cny += trade_realized_cny
        cost -= basis
        cost_cny -= basis_cny
        quantity -= basis_quantity
        if trade_date_key == as_of_key:
            daily_realized += trade_realized
            daily_realized_cny += trade_realized_cny

    return {
        "quantity": quantity,
        "cost": cost,
        "cost_cny": cost_cny,
        "realized_pnl": realized,
        "realized_pnl_cny": realized_cny,
        "daily_realized_pnl": daily_realized,
        "daily_realized_pnl_cny": daily_realized_cny,
    }

def _apply_inferred_realized_trade_pnl(row: dict[str, Any], state: dict[str, Decimal]) -> bool:
    realized_native = _dec(state.get("realized_pnl"))
    realized_cny = _dec(state.get("realized_pnl_cny"))
    if realized_native == 0 and realized_cny == 0:
        return False

    snapshot_cost = _dec(row.get("cost"))
    snapshot_cost_cny = _dec(row.get("cost_cny", snapshot_cost))
    remaining_cost = _dec(state.get("cost"))
    remaining_cost_cny = _dec(state.get("cost_cny", remaining_cost))
    net_invested = remaining_cost - realized_native
    net_invested_cny = remaining_cost_cny - realized_cny

    base_holding = _dec(row.get("holding_pnl"))
    base_holding_native = _dec(row.get("holding_pnl_native", row.get("holding_pnl")))
    base_holding_cny = _dec(row.get("holding_pnl_cny", row.get("holding_pnl")))

    snapshot_is_net_invested = _money_close(snapshot_cost_cny, net_invested_cny)
    snapshot_is_remaining_cost = _money_close(snapshot_cost_cny, remaining_cost_cny)
    if not snapshot_is_net_invested and not snapshot_is_remaining_cost:
        return False

    row["realized_trade_pnl"] = _dec(row.get("realized_trade_pnl")) + realized_native
    row["realized_trade_pnl_native"] = _dec(row.get("realized_trade_pnl_native")) + realized_native
    row["realized_trade_pnl_cny"] = _dec(row.get("realized_trade_pnl_cny")) + realized_cny
    row["realized_trade_pnl_source"] = "weighted_average_trade_replay"

    if snapshot_is_net_invested:
        row["reported_cost"] = snapshot_cost
        row["reported_cost_cny"] = snapshot_cost_cny
        row["cost_basis_mode"] = "net_invested_snapshot"
        row["cost"] = remaining_cost
        row["cost_cny"] = remaining_cost_cny
        row["holding_pnl_before_realized_trade"] = _dec(row.get("market_value")) - remaining_cost
        row["holding_pnl_before_realized_trade_native"] = (
            _dec(row.get("market_value_native", row.get("market_value"))) - remaining_cost
        )
        row["holding_pnl_before_realized_trade_cny"] = (
            _dec(row.get("market_value_cny", row.get("market_value"))) - remaining_cost_cny
        )
        quantity = _dec(row.get("quantity"))
        if quantity > 0:
            row["buy_price"] = remaining_cost / quantity
        return True

    row["cost_basis_mode"] = "remaining_cost_snapshot"
    row["holding_pnl_before_realized_trade"] = base_holding
    row["holding_pnl_before_realized_trade_native"] = base_holding_native
    row["holding_pnl_before_realized_trade_cny"] = base_holding_cny
    row["holding_pnl"] = base_holding + realized_native
    row["holding_pnl_native"] = base_holding_native + realized_native
    row["holding_pnl_cny"] = base_holding_cny + realized_cny

    daily_native = _dec(state.get("daily_realized_pnl"))
    daily_cny = _dec(state.get("daily_realized_pnl_cny"))
    if daily_native != 0 or daily_cny != 0:
        base_daily = _dec(row.get("daily_pnl"))
        base_daily_native = _dec(row.get("daily_pnl_native", row.get("daily_pnl")))
        base_daily_cny = _dec(row.get("daily_pnl_cny", row.get("daily_pnl")))
        row["daily_realized_trade_pnl"] = _dec(row.get("daily_realized_trade_pnl")) + daily_native
        row["daily_realized_trade_pnl_native"] = (
            _dec(row.get("daily_realized_trade_pnl_native")) + daily_native
        )
        row["daily_realized_trade_pnl_cny"] = _dec(row.get("daily_realized_trade_pnl_cny")) + daily_cny
        row.setdefault("daily_price_pnl", base_daily)
        row.setdefault("daily_price_pnl_native", base_daily_native)
        row.setdefault("daily_price_pnl_cny", base_daily_cny)
        row.setdefault("daily_total_pnl", base_daily)
        row.setdefault("daily_total_pnl_cny", base_daily_cny)
        row["daily_realized_trade_pnl_only"] = True

    return True

def _enrich_realized_trade_pnl(cur, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge realized trade-fact PnL into open holding display rows."""
    keys = {
        (
            str(row.get("account") or row.get("account_code") or ""),
            str(row.get("symbol") or ""),
            row.get("as_of") or row.get("snapshot_as_of"),
        )
        for row in rows
        if row.get("symbol")
        and (row.get("as_of") is not None or row.get("snapshot_as_of") is not None)
        and _dec(row.get("market_value_cny", row.get("market_value"))) > Decimal("0.0001")
    }
    if not keys or not _table_exists(cur, "pams_investment_trade_fact"):
        return rows

    accounts = sorted({account for account, _, _ in keys})
    symbols = sorted({symbol for _, symbol, _ in keys})
    max_as_of = max(as_of for _, _, as_of in keys)
    cur.execute(
        """
        SELECT
            account_code,
            symbol,
            trade_date,
            SUM(CASE WHEN realized_pnl IS NOT NULL THEN COALESCE(realized_pnl, 0) ELSE 0 END) AS realized_pnl,
            SUM(CASE WHEN realized_pnl IS NOT NULL THEN COALESCE(realized_pnl_cny, realized_pnl, 0) ELSE 0 END) AS realized_pnl_cny,
            COUNT(*) FILTER (
                WHERE trade_type IN ('sell', 'fund_redemption')
                  AND realized_pnl IS NULL
            ) AS missing_realized_count
        FROM pams_investment_trade_fact
        WHERE account_code = ANY(%s::text[])
          AND symbol = ANY(%s::text[])
          AND trade_date <= %s::date
          AND (
              realized_pnl IS NOT NULL
              OR trade_type IN ('sell', 'fund_redemption')
          )
          AND COALESCE(LOWER(review_status), 'auto') NOT IN ('cancelled', 'canceled', 'void')
        GROUP BY account_code, symbol, trade_date
        """,
        (accounts, symbols, max_as_of),
    )

    facts_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for raw in cur.fetchall():
        item = dict(raw)
        key = (str(item.get("account_code") or ""), str(item.get("symbol") or ""))
        facts_by_key.setdefault(key, []).append(item)
    explicit_realized_keys = set(facts_by_key)
    missing_realized_keys = {
        key
        for key, facts in facts_by_key.items()
        if any(_dec(fact.get("missing_realized_count")) > 0 for fact in facts)
    }

    inferred_trades_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    missing_realized_keys.update(
        (account, symbol)
        for account, symbol, _ in keys
        if (account, symbol) not in explicit_realized_keys
    )
    if missing_realized_keys:
        cur.execute(
            """
            SELECT
                trade_id,
                account_code,
                symbol,
                trade_date,
                trade_type,
                side,
                quantity,
                gross_amount,
                fee_amount,
                tax_amount,
                net_cash,
                fx_rate_to_cny
            FROM pams_investment_trade_fact
            WHERE account_code = ANY(%s::text[])
              AND symbol = ANY(%s::text[])
              AND trade_date <= %s::date
              AND trade_type IN ('buy', 'sell', 'fund_subscription', 'fund_redemption')
              AND quantity IS NOT NULL
              AND COALESCE(LOWER(review_status), 'auto') NOT IN ('cancelled', 'canceled', 'void')
            ORDER BY account_code, symbol, trade_date, trade_id
            """,
            (accounts, symbols, max_as_of),
        )
        for raw in cur.fetchall():
            item = dict(raw)
            key = (str(item.get("account_code") or ""), str(item.get("symbol") or ""))
            if key not in missing_realized_keys:
                continue
            inferred_trades_by_key.setdefault(key, []).append(item)

    for row in rows:
        account = str(row.get("account") or row.get("account_code") or "")
        symbol = str(row.get("symbol") or "")
        as_of = row.get("as_of") or row.get("snapshot_as_of")
        if not account or not symbol or as_of is None:
            continue

        inferred_trades = inferred_trades_by_key.get((account, symbol))
        if inferred_trades:
            state = _replay_weighted_average_trade_pnl(inferred_trades, as_of_key=str(as_of)[:10])
            if _apply_inferred_realized_trade_pnl(row, state):
                continue

        total_native = Decimal("0")
        total_cny = Decimal("0")
        daily_native = Decimal("0")
        daily_cny = Decimal("0")
        as_of_key = str(as_of)[:10]
        for fact in facts_by_key.get((account, symbol), []):
            trade_date_key = str(fact.get("trade_date"))[:10]
            if trade_date_key > as_of_key:
                continue
            realized_native = _dec(fact.get("realized_pnl"))
            realized_cny = _dec(fact.get("realized_pnl_cny"))
            total_native += realized_native
            total_cny += realized_cny
            if trade_date_key == as_of_key:
                daily_native += realized_native
                daily_cny += realized_cny

        if total_native == 0 and total_cny == 0:
            continue

        base_holding = _dec(row.get("holding_pnl"))
        base_holding_native = _dec(row.get("holding_pnl_native", row.get("holding_pnl")))
        base_holding_cny = _dec(row.get("holding_pnl_cny", row.get("holding_pnl")))
        row["holding_pnl_before_realized_trade"] = base_holding
        row["holding_pnl_before_realized_trade_native"] = base_holding_native
        row["holding_pnl_before_realized_trade_cny"] = base_holding_cny
        row["realized_trade_pnl"] = _dec(row.get("realized_trade_pnl")) + total_native
        row["realized_trade_pnl_native"] = _dec(row.get("realized_trade_pnl_native")) + total_native
        row["realized_trade_pnl_cny"] = _dec(row.get("realized_trade_pnl_cny")) + total_cny
        row["holding_pnl"] = base_holding + total_native
        row["holding_pnl_native"] = base_holding_native + total_native
        row["holding_pnl_cny"] = base_holding_cny + total_cny

        if daily_native != 0 or daily_cny != 0:
            base_daily = _dec(row.get("daily_pnl"))
            base_daily_native = _dec(row.get("daily_pnl_native", row.get("daily_pnl")))
            base_daily_cny = _dec(row.get("daily_pnl_cny", row.get("daily_pnl")))
            row["daily_realized_trade_pnl"] = _dec(row.get("daily_realized_trade_pnl")) + daily_native
            row["daily_realized_trade_pnl_native"] = (
                _dec(row.get("daily_realized_trade_pnl_native")) + daily_native
            )
            row["daily_realized_trade_pnl_cny"] = _dec(row.get("daily_realized_trade_pnl_cny")) + daily_cny
            row.setdefault("daily_price_pnl", base_daily)
            row.setdefault("daily_price_pnl_native", base_daily_native)
            row.setdefault("daily_price_pnl_cny", base_daily_cny)
            row.setdefault("daily_total_pnl", base_daily)
            row.setdefault("daily_total_pnl_cny", base_daily_cny)
            row["daily_realized_trade_pnl_only"] = True

    return rows

def _closed_position_rows(
    cur,
    *,
    category: str | None = None,
    minor_category: str | None = None,
    current_keys: set[tuple[str, str, str, str, str]] | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    major_expr = """
        COALESCE(
            NULLIF(asset_major_category, ''),
            NULLIF(raw_json #>> '{source_row,major_category}', ''),
            NULLIF(market, ''),
            trade_type
        )
    """
    minor_expr = """
        COALESCE(
            NULLIF(asset_minor_category, ''),
            NULLIF(raw_json #>> '{source_row,minor_category}', ''),
            NULLIF(market, ''),
            trade_type
        )
    """
    where = [
        "gross_amount IS NOT NULL",
        "COALESCE(LOWER(review_status), 'confirmed') NOT IN ('cancelled', 'canceled', 'void', 'pending', 'pending_settlement', 'in_transit')",
    ]
    params: list[Any] = []
    if category:
        where.append(f"{major_expr} = %s")
        params.append(category)
    if minor_category:
        where.append(f"{minor_expr} = %s")
        params.append(minor_category)

    cur.execute(
        f"""
        WITH tx AS (
            SELECT
                account_code,
                {major_expr} AS major_category,
                {minor_expr} AS minor_category,
                COALESCE(symbol, '') AS symbol,
                name,
                currency,
                MIN(trade_date) AS first_trade_date,
                MAX(trade_date) AS closed_date,
                SUM(CASE WHEN trade_type IN ('buy', 'fund_subscription') OR lower(COALESCE(side, '')) IN ('buy', 'subscribe') THEN ABS(gross_amount) ELSE 0 END) AS buy_amount,
                0::numeric AS cancel_amount,
                SUM(CASE WHEN trade_type IN ('sell', 'fund_redemption') OR lower(COALESCE(side, '')) IN ('sell', 'redeem') THEN ABS(gross_amount) ELSE 0 END) AS sell_amount
            FROM pams_investment_trade_fact
            WHERE {" AND ".join(where)}
            GROUP BY account_code, {major_expr}, {minor_expr}, COALESCE(symbol, ''), name, currency
        )
        SELECT
            account_code AS account,
            minor_category AS type,
            symbol,
            name,
            currency,
            0::numeric AS market_value,
            0::numeric AS market_value_native,
            0::numeric AS market_value_cny,
            GREATEST(buy_amount - cancel_amount, 0)::numeric AS cost,
            GREATEST(buy_amount - cancel_amount, 0)::numeric AS cost_cny,
            (sell_amount - GREATEST(buy_amount - cancel_amount, 0))::numeric AS holding_pnl,
            (sell_amount - GREATEST(buy_amount - cancel_amount, 0))::numeric AS holding_pnl_native,
            (sell_amount - GREATEST(buy_amount - cancel_amount, 0))::numeric AS holding_pnl_cny,
            0::numeric AS daily_pnl,
            0::numeric AS daily_pnl_cny,
            major_category,
            minor_category,
            closed_date AS as_of,
            closed_date AS snapshot_as_of,
            NULL::numeric AS quantity,
            NULL::numeric AS buy_price,
            NULL::numeric AS current_price,
            TRUE AS is_closed,
            first_trade_date,
            closed_date,
            buy_amount,
            cancel_amount,
            sell_amount
        FROM tx
        WHERE buy_amount > 0
          AND sell_amount > 0
        ORDER BY closed_date DESC, ABS(sell_amount - GREATEST(buy_amount - cancel_amount, 0)) DESC
        LIMIT %s
        """,
        [*params, limit],
    )
    keys = current_keys or set()
    rows: list[dict[str, Any]] = []
    for raw in cur.fetchall():
        row = dict(raw)
        if _holding_key(row) in keys:
            continue
        rows.append(row)
    return rows

def _tradingview_closed_position_rows(
    cur,
    *,
    current_keys: set[tuple[str, str, str, str, str]] | None = None,
    daily_pnl_as_of=None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Return closed Tonghuashun A-share positions from TradingView trades."""
    cur.execute(
        """
        WITH tx AS (
            SELECT
                account_code,
                normalized_symbol,
                MIN(closing_time::date) AS first_trade_date,
                MAX(closing_time::date) AS closed_date,
                SUM(CASE WHEN side = 'Buy' THEN quantity ELSE -quantity END) AS net_quantity,
                SUM(CASE WHEN side = 'Buy' THEN quantity ELSE 0 END) AS buy_qty,
                SUM(CASE WHEN side = 'Sell' THEN quantity ELSE 0 END) AS sell_qty,
                SUM(CASE WHEN side = 'Buy' THEN quantity * fill_price + COALESCE(commission, 0) ELSE 0 END) AS buy_cash,
                SUM(CASE WHEN side = 'Sell' THEN quantity * fill_price - COALESCE(commission, 0) ELSE 0 END) AS sell_cash,
                MAX(fill_price) FILTER (WHERE side = 'Sell') AS exit_price
            FROM pams_tradingview_transactions
            WHERE account_code = 'tonghuashun'
              AND side IN ('Buy', 'Sell')
              AND normalized_symbol IS NOT NULL
            GROUP BY account_code, normalized_symbol
        )
        SELECT
            tx.account_code AS account,
            %s AS type,
            tx.normalized_symbol AS symbol,
            COALESCE(name_snap.name, tx.normalized_symbol) AS name,
            'CNY' AS currency,
            0::numeric AS market_value,
            0::numeric AS market_value_native,
            0::numeric AS market_value_cny,
            buy_cash::numeric AS cost,
            buy_cash::numeric AS cost_cny,
            (sell_cash - buy_cash)::numeric AS holding_pnl,
            (sell_cash - buy_cash)::numeric AS holding_pnl_native,
            (sell_cash - buy_cash)::numeric AS holding_pnl_cny,
            CASE
                WHEN tx.closed_date = %s::date AND prev_px.close IS NOT NULL
                THEN (sell_cash - sell_qty * prev_px.close)::numeric
                ELSE 0::numeric
            END AS daily_pnl,
            CASE
                WHEN tx.closed_date = %s::date AND prev_px.close IS NOT NULL
                THEN (sell_cash - sell_qty * prev_px.close)::numeric
                ELSE 0::numeric
            END AS daily_pnl_cny,
            %s AS major_category,
            %s AS minor_category,
            tx.closed_date AS as_of,
            tx.closed_date AS snapshot_as_of,
            NULL::numeric AS quantity,
            CASE WHEN buy_qty > 0 THEN (buy_cash / buy_qty)::numeric ELSE NULL::numeric END AS buy_price,
            exit_price::numeric AS current_price,
            TRUE AS is_closed,
            first_trade_date,
            closed_date,
            buy_cash,
            sell_cash
        FROM tx
        LEFT JOIN LATERAL (
            SELECT hs.name
            FROM pams_holdings_snapshot hs
            WHERE hs.account_code = tx.account_code
              AND hs.symbol = tx.normalized_symbol
              AND hs.name IS NOT NULL
            ORDER BY hs.as_of DESC
            LIMIT 1
        ) name_snap ON TRUE
        LEFT JOIN LATERAL (
            SELECT close
            FROM pams_ohlcv_daily o
            WHERE o.market = 'CN_A'
              AND o.symbol = CASE
                  WHEN LEFT(tx.normalized_symbol, 1) = '6' THEN 'sh' || tx.normalized_symbol
                  ELSE 'sz' || tx.normalized_symbol
              END
              AND o.trade_date < tx.closed_date
            ORDER BY o.trade_date DESC
            LIMIT 1
        ) prev_px ON TRUE
        WHERE ABS(net_quantity) <= 0.0000001
          AND buy_cash > 0
          AND sell_cash > 0
        ORDER BY closed_date DESC, ABS(sell_cash - buy_cash) DESC
        LIMIT %s
        """,
        (
            "A\u80a1",
            daily_pnl_as_of,
            daily_pnl_as_of,
            "A\u80a1",
            "A\u80a1",
            limit,
        ),
    )
    keys = current_keys or set()
    rows: list[dict[str, Any]] = []
    for raw in cur.fetchall():
        row = dict(raw)
        if _holding_key(row) in keys:
            continue
        rows.append(row)
    return rows

def _ibkr_closed_position_rows(
    cur,
    *,
    current_keys: set[tuple[str, str, str, str, str]] | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Return closed IBKR stock/ETF trades as display rows."""
    cur.execute(
        """
        WITH fx AS (
            SELECT DISTINCT ON (quote_currency) quote_currency, rate
            FROM pams_fx_rates_daily
            WHERE base_currency = 'CNY'
            ORDER BY quote_currency, as_of DESC
        ), tx AS (
            SELECT
                account_code,
                symbol,
                MAX(name) AS name,
                MAX(currency) AS currency,
                MIN(trade_date) AS first_trade_date,
                MAX(trade_date) AS closed_date,
                SUM(quantity) AS net_quantity,
                SUM(
                    CASE
                        WHEN quantity > 0 THEN ABS(COALESCE(NULLIF(net_cash, 0), gross_amount))
                        ELSE 0
                    END
                ) AS buy_cash,
                SUM(COALESCE(realized_pnl, 0)) AS realized_pnl,
                MAX(price) FILTER (WHERE quantity < 0) AS exit_price
            FROM pams_ibkr_trades
            WHERE account_code = 'ibkr'
              AND COALESCE(asset_category, '') = 'STK'
              AND quantity IS NOT NULL
            GROUP BY account_code, symbol
        )
        SELECT
            account_code AS account,
            '缇庤偂鍩洪噾' AS type,
            symbol,
            name,
            currency,
            0::numeric AS market_value,
            0::numeric AS market_value_native,
            0::numeric AS market_value_cny,
            buy_cash::numeric AS cost,
            (buy_cash * COALESCE(fx.rate, 1))::numeric AS cost_cny,
            realized_pnl::numeric AS holding_pnl,
            realized_pnl::numeric AS holding_pnl_native,
            (realized_pnl * COALESCE(fx.rate, 1))::numeric AS holding_pnl_cny,
            0::numeric AS daily_pnl,
            0::numeric AS daily_pnl_cny,
            '缇庤偂' AS major_category,
            '缇庤偂鍩洪噾' AS minor_category,
            closed_date AS as_of,
            closed_date AS snapshot_as_of,
            NULL::numeric AS quantity,
            NULL::numeric AS buy_price,
            exit_price::numeric AS current_price,
            TRUE AS is_closed,
            first_trade_date,
            closed_date,
            buy_cash,
            realized_pnl
        FROM tx
        LEFT JOIN fx ON fx.quote_currency = tx.currency
        WHERE ABS(net_quantity) <= 0.0000001
          AND buy_cash > 0
        ORDER BY closed_date DESC, ABS(realized_pnl) DESC
        LIMIT %s
        """,
        (limit,),
    )
    keys = current_keys or set()
    rows: list[dict[str, Any]] = []
    for raw in cur.fetchall():
        row = dict(raw)
        if _holding_key(row) in keys:
            continue
        rows.append(row)
    return rows
