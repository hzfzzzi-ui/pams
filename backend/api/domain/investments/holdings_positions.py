"""Holdings row helpers and closed-position queries.

These helpers are kept separate from the public holdings payload builders so
`holdings_view.py` stays focused on endpoint adapter orchestration.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
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
            out[cny_key] = None if out[cny_key] is None else _dec(out[cny_key]) * ratio
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
        # Missing FX must not make a known native position disappear.
        value = row.get("market_value_cny")
        market_value = _dec(row.get("market_value") if value is None else value)
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
        WITH tx AS (
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
                ABS(COALESCE(gross_amount, net_cash)) AS gross_amount,
                trade_date,
                review_status
            FROM pams_investment_trade_fact
        )
        SELECT
            tx.account_code,
            tx.major_category,
            tx.minor_category,
            COALESCE(tx.symbol, '') AS symbol,
            tx.currency,
            tx.trade_date,
            CASE WHEN COUNT(*) FILTER (WHERE tx.gross_amount IS NULL)>0 THEN NULL ELSE SUM(tx.gross_amount) END AS realized_income,
            CASE WHEN COUNT(*) FILTER (WHERE tx.gross_amount IS NULL OR
                (tx.currency IS DISTINCT FROM 'CNY' AND fx.rate IS NULL)) > 0 THEN NULL
                ELSE SUM(tx.gross_amount * CASE WHEN tx.currency='CNY' THEN 1::numeric ELSE fx.rate END) END AS realized_income_cny
        FROM tx
        LEFT JOIN LATERAL (SELECT rate FROM pams_fx_rates_daily f
            WHERE NULLIF(BTRIM(f.source),'') IS NOT NULL AND f.base_currency='CNY' AND f.quote_currency=tx.currency AND f.as_of=tx.trade_date
              AND f.rate > 0 AND f.rate NOT IN ('NaN'::numeric,'Infinity'::numeric)
            LIMIT 1) fx ON TRUE
        WHERE tx.major_category = 'ESOP'
          AND LOWER(COALESCE(NULLIF(tx.trade_type, ''), tx.side, '')) IN ('dividend', 'interest', 'income', 'distribution', 'coupon')
          AND COALESCE(LOWER(tx.review_status), 'confirmed') NOT IN ('cancelled', 'canceled', 'void')
        GROUP BY tx.account_code, tx.major_category, tx.minor_category, COALESCE(tx.symbol, ''), tx.currency, tx.trade_date
        """,
    )

    income_by_key: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for raw in cur.fetchall():
        item = dict(raw)
        key = (str(item.get("account_code") or ""), str(item.get("major_category") or ""),
               str(item.get("minor_category") or ""), str(item.get("symbol") or ""))
        income_by_key.setdefault(key, []).append(item)

    for row in rows:
        key = (str(row.get("account") or row.get("account_code") or ""),
               str(row.get("major_category") or ""), str(row.get("minor_category") or row.get("type") or ""),
               str(row.get("symbol") or ""))
        as_of = row.get("as_of") or row.get("snapshot_as_of")
        if as_of is None:
            continue
        observations = [item for item in income_by_key.get(key, []) if item.get("trade_date") is not None
                        and str(item["trade_date"])[:10] <= str(as_of)[:10]]
        if not observations:
            continue
        income: dict[str, Any] = {"realized_income_cny": Decimal("0"), "by_currency": {}}
        for item in observations:
            if item.get("realized_income_cny") is None:
                income["realized_income_cny"] = None
            elif income["realized_income_cny"] is not None:
                income["realized_income_cny"] += _dec(item["realized_income_cny"])
            currency = str(item.get("currency") or "")
            native = item.get("realized_income")
            previous = income["by_currency"].get(currency, Decimal("0"))
            income["by_currency"][currency] = (previous + _dec(native)
                if previous is not None and native is not None else None)

        row_currency = str(row.get("currency") or "")
        realized_native = (income["by_currency"].get(row_currency, Decimal("0"))
                           if row_currency and "" not in income["by_currency"] else None)
        realized_cny = income.get("realized_income_cny")
        if any(currency != row_currency for currency in income["by_currency"]):
            realized_native = None
            realized_cny = None

        row["holding_pnl_before_income"] = row.get("holding_pnl")
        row["holding_pnl_before_income_native"] = row.get("holding_pnl_native", row.get("holding_pnl"))
        row["holding_pnl_before_income_cny"] = row.get("holding_pnl_cny", row.get("holding_pnl"))
        row["realized_income"] = realized_native
        row["realized_income_native"] = realized_native
        row["realized_income_cny"] = realized_cny
        prior_native = row.get("holding_pnl_native", row.get("holding_pnl"))
        row["holding_pnl"] = (_dec(row["holding_pnl"]) + realized_native
                              if row.get("holding_pnl") is not None and realized_native is not None else None)
        row["holding_pnl_native"] = (_dec(prior_native) + realized_native
                                     if prior_native is not None and realized_native is not None else None)
        row["holding_pnl_cny"] = (_dec(row["holding_pnl_cny"]) + realized_cny
            if row.get("holding_pnl_cny") is not None and realized_cny is not None else None)

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
    net_cash = row.get("net_cash")
    if net_cash is not None:
        return -_dec(net_cash)
    return abs(_dec(row.get("gross_amount"))) + abs(_dec(row.get("fee_amount"))) + abs(_dec(row.get("tax_amount")))

def _trade_cash_proceeds(row: dict[str, Any]) -> Decimal:
    net_cash = row.get("net_cash")
    if net_cash is not None:
        return _dec(net_cash)
    return abs(_dec(row.get("gross_amount"))) - abs(_dec(row.get("fee_amount"))) - abs(_dec(row.get("tax_amount")))


def _trade_order_ambiguous(trades: list[dict[str, Any]]) -> bool:
    """Do not infer ordering of same-day buys and sells from row IDs."""
    by_day: dict[str, list[dict[str, Any]]] = {}
    for trade in trades:
        by_day.setdefault(str(trade.get('trade_date'))[:10], []).append(trade)
    for day_trades in by_day.values():
        def direction(row):
            value = str(row.get('trade_type') or row.get('side') or '').lower()
            return 'buy' if value in {'buy', 'fund_subscription'} else 'sell'
        if len({direction(row) for row in day_trades}) < 2:
            continue
        if any(not row.get('trade_time') for row in day_trades):
            return True
        time_sides: dict[str, set[str]] = {}
        for row in day_trades:
            time_sides.setdefault(str(row['trade_time']), set()).add(direction(row))
        if any(len(sides) > 1 for sides in time_sides.values()):
            return True
    return False


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

    for trade in sorted(trades, key=lambda item: (str(item.get("trade_date"))[:10],
                                                  str(item.get("trade_time") or ''), _dec(item.get("trade_id")))):
        trade_date_key = str(trade.get("trade_date"))[:10]
        if trade_date_key > as_of_key:
            continue

        trade_type = str(trade.get("trade_type") or "").lower()
        trade_quantity = abs(_dec(trade.get("quantity")))
        if trade_quantity <= 0:
            continue

        raw_rate = trade.get("effective_fx_rate")
        try:
            fx_rate = Decimal(str(raw_rate)) if raw_rate is not None else None
            if fx_rate is not None and (not fx_rate.is_finite() or fx_rate <= 0):
                fx_rate = None
        except (InvalidOperation, ValueError):
            fx_rate = None
        if trade_type in {"buy", "fund_subscription"}:
            outlay = _trade_cash_outlay(trade)
            quantity += trade_quantity
            cost += outlay
            cost_cny = cost_cny + outlay * fx_rate if cost_cny is not None and fx_rate is not None else None
            continue

        if trade_type not in {"sell", "fund_redemption"} or quantity <= 0:
            continue

        basis_quantity = min(trade_quantity, quantity)
        basis = cost / quantity * basis_quantity if quantity else Decimal("0")
        basis_cny = cost_cny / quantity * basis_quantity if quantity and cost_cny is not None else None
        proceeds = _trade_cash_proceeds(trade)
        trade_realized = proceeds - basis
        trade_realized_cny = proceeds * fx_rate - basis_cny if fx_rate is not None and basis_cny is not None else None
        realized += trade_realized
        realized_cny = realized_cny + trade_realized_cny if realized_cny is not None and trade_realized_cny is not None else None
        cost -= basis
        cost_cny = cost_cny - basis_cny if cost_cny is not None and basis_cny is not None else None
        quantity -= basis_quantity
        if trade_date_key == as_of_key:
            daily_realized += trade_realized
            daily_realized_cny = daily_realized_cny + trade_realized_cny if daily_realized_cny is not None and trade_realized_cny is not None else None

    return {
        "quantity": quantity,
        "cost": cost,
        "cost_cny": cost_cny,
        "realized_pnl": realized,
        "realized_pnl_cny": realized_cny,
        "daily_realized_pnl": daily_realized,
        "daily_realized_pnl_cny": daily_realized_cny,
    }

def _apply_inferred_realized_trade_pnl(row: dict[str, Any], state: dict[str, Any]) -> bool:
    realized_native = _dec(state.get("realized_pnl"))
    realized_cny = state.get("realized_pnl_cny")
    if realized_native == 0 and realized_cny == 0:
        return False
    if row.get("cost") is None:
        return False
    snapshot_cost = _dec(row["cost"])
    remaining_cost = _dec(state.get("cost"))
    snapshot_is_net_invested = _money_close(snapshot_cost, remaining_cost - realized_native)
    snapshot_is_remaining_cost = _money_close(snapshot_cost, remaining_cost)
    if not snapshot_is_net_invested and not snapshot_is_remaining_cost:
        return False
    row["realized_trade_pnl"] = _dec(row.get("realized_trade_pnl")) + realized_native
    row["realized_trade_pnl_native"] = _dec(row.get("realized_trade_pnl_native")) + realized_native
    row["realized_trade_pnl_cny"] = (_dec(row.get("realized_trade_pnl_cny")) + realized_cny
                                     if realized_cny is not None else None)
    row["realized_trade_pnl_source"] = ("weighted_average_trade_replay" if realized_cny is not None
                                         else "weighted_average_trade_replay_fx_unknown")
    remaining_cny = state.get("cost_cny")
    if snapshot_is_net_invested:
        row["reported_cost"] = snapshot_cost
        row["reported_cost_cny"] = row.get("cost_cny")
        row["cost_basis_mode"] = "net_invested_snapshot"
        row["cost"] = remaining_cost
        row["cost_cny"] = remaining_cny
        row["holding_pnl_before_realized_trade"] = (_dec(row["market_value"]) - remaining_cost
                                                       if row.get("market_value") is not None else None)
        row["holding_pnl_before_realized_trade_native"] = row["holding_pnl_before_realized_trade"]
        row["holding_pnl_before_realized_trade_cny"] = (_dec(row["market_value_cny"]) - remaining_cny
            if row.get("market_value_cny") is not None and remaining_cny is not None else None)
        quantity = _dec(row.get("quantity"))
        if quantity > 0:
            row["buy_price"] = remaining_cost / quantity
        if realized_cny is None:
            row["holding_pnl_cny"] = None
        return True
    row["cost_basis_mode"] = "remaining_cost_snapshot"
    for key in ("holding_pnl", "holding_pnl_native", "holding_pnl_cny"):
        row[key.replace("holding_pnl", "holding_pnl_before_realized_trade")] = row.get(key)
    for key in ("holding_pnl", "holding_pnl_native"):
        row[key] = _dec(row[key]) + realized_native if row.get(key) is not None else None
    row["holding_pnl_cny"] = (_dec(row["holding_pnl_cny"]) + realized_cny
        if row.get("holding_pnl_cny") is not None and realized_cny is not None else None)
    daily_native = _dec(state.get("daily_realized_pnl"))
    daily_cny = state.get("daily_realized_pnl_cny")
    if daily_native != 0 or daily_cny is None or daily_cny != 0:
        row["daily_realized_trade_pnl"] = _dec(row.get("daily_realized_trade_pnl")) + daily_native
        row["daily_realized_trade_pnl_native"] = _dec(row.get("daily_realized_trade_pnl_native")) + daily_native
        row["daily_realized_trade_pnl_cny"] = (_dec(row.get("daily_realized_trade_pnl_cny")) + daily_cny
                                               if daily_cny is not None else None)
        row.setdefault("daily_price_pnl", row.get("daily_pnl"))
        row.setdefault("daily_price_pnl_native", row.get("daily_pnl_native", row.get("daily_pnl")))
        row.setdefault("daily_price_pnl_cny", row.get("daily_pnl_cny"))
        row.setdefault("daily_total_pnl", row.get("daily_pnl"))
        row.setdefault("daily_total_pnl_cny", row.get("daily_pnl_cny"))
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
        and _dec(row.get("market_value_cny") if row.get("market_value_cny") is not None else row.get("market_value")) > Decimal("0.0001")
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
            currency,
            SUM(CASE WHEN realized_pnl IS NOT NULL THEN COALESCE(realized_pnl, 0) ELSE 0 END) AS realized_pnl,
            CASE WHEN COUNT(*) FILTER (WHERE realized_pnl IS NOT NULL AND
                currency IS DISTINCT FROM 'CNY' AND (SELECT f.rate FROM pams_fx_rates_daily f
                    WHERE NULLIF(BTRIM(f.source),'') IS NOT NULL AND f.base_currency='CNY' AND f.quote_currency=pams_investment_trade_fact.currency
                      AND f.as_of=pams_investment_trade_fact.trade_date AND f.rate>0
                      AND f.rate NOT IN ('NaN'::numeric,'Infinity'::numeric) LIMIT 1) IS NULL)>0 THEN NULL
                ELSE SUM(CASE WHEN realized_pnl IS NULL THEN 0 WHEN currency='CNY' THEN realized_pnl
                    ELSE realized_pnl * (SELECT f.rate FROM pams_fx_rates_daily f WHERE NULLIF(BTRIM(f.source),'') IS NOT NULL AND f.base_currency='CNY'
                        AND f.quote_currency=pams_investment_trade_fact.currency AND f.as_of=pams_investment_trade_fact.trade_date
                        AND f.rate>0 AND f.rate NOT IN ('NaN'::numeric,'Infinity'::numeric) LIMIT 1) END) END AS realized_pnl_cny,
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
        GROUP BY account_code, symbol, trade_date, currency
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
                trade_time,
                trade_type,
                side,
                quantity,
                gross_amount,
                fee_amount,
                fee_currency,
                tax_amount,
                net_cash,
                currency,
                source_table,
                raw_json,
                CASE WHEN currency='CNY' THEN 1::numeric ELSE (SELECT f.rate FROM pams_fx_rates_daily f
                    WHERE NULLIF(BTRIM(f.source),'') IS NOT NULL AND f.base_currency='CNY' AND f.quote_currency=pams_investment_trade_fact.currency
                      AND f.as_of=pams_investment_trade_fact.trade_date AND f.rate>0
                      AND f.rate NOT IN ('NaN'::numeric,'Infinity'::numeric) LIMIT 1) END AS effective_fx_rate
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

        relevant_facts = [fact for fact in facts_by_key.get((account, symbol), [])
                          if str(fact.get("trade_date"))[:10] <= str(as_of)[:10]]
        fact_currencies = {str(fact.get("currency") or "").upper() for fact in relevant_facts}
        if fact_currencies and (len(fact_currencies) != 1 or "" in fact_currencies or
                str(row.get("currency") or "").upper() not in fact_currencies):
            row["realized_trade_pnl"] = None
            row["realized_trade_pnl_native"] = None
            row["realized_trade_pnl_cny"] = None
            row["realized_trade_pnl_source"] = "unavailable_currency_identity"
            continue
        inferred_trades = inferred_trades_by_key.get((account, symbol))
        if inferred_trades:
            relevant = [trade for trade in inferred_trades if str(trade.get("trade_date"))[:10] <= str(as_of)[:10]]
            currencies = {str(trade.get("currency") or "").upper() for trade in relevant}
            if len(currencies) != 1 or "" in currencies or str(row.get("currency") or "").upper() not in currencies:
                row["realized_trade_pnl"] = None
                row["realized_trade_pnl_native"] = None
                row["realized_trade_pnl_cny"] = None
                row["realized_trade_pnl_source"] = "unavailable_currency_identity"
                continue
            from domain.investments.trade_cost_evidence import known_trade_charges, unconverted_ibkr_fee
            if any((unconverted_ibkr_fee(trade) or (known_trade_charges(trade) is None and trade.get("net_cash") is None))
                   and str(trade.get("trade_date"))[:10] <= str(as_of)[:10]
                   for trade in inferred_trades):
                row["realized_trade_pnl"] = None
                row["realized_trade_pnl_native"] = None
                row["realized_trade_pnl_cny"] = None
                row["realized_trade_pnl_source"] = "unavailable_missing_tax"
                continue
            if _trade_order_ambiguous(relevant):
                row["realized_trade_pnl"] = None
                row["realized_trade_pnl_native"] = None
                row["realized_trade_pnl_cny"] = None
                row["realized_trade_pnl_source"] = "unavailable_trade_order"
                continue
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
            realized_cny = fact.get("realized_pnl_cny")
            total_native += realized_native
            total_cny = total_cny + _dec(realized_cny) if total_cny is not None and realized_cny is not None else None
            if trade_date_key == as_of_key:
                daily_native += realized_native
                daily_cny = daily_cny + _dec(realized_cny) if daily_cny is not None and realized_cny is not None else None

        if total_native == 0 and total_cny == 0:
            continue

        base_holding = row.get("holding_pnl")
        base_holding_native = row.get("holding_pnl_native", base_holding)
        base_holding_cny = row.get("holding_pnl_cny")
        row["holding_pnl_before_realized_trade"] = base_holding
        row["holding_pnl_before_realized_trade_native"] = base_holding_native
        row["holding_pnl_before_realized_trade_cny"] = base_holding_cny
        row["realized_trade_pnl"] = _dec(row.get("realized_trade_pnl")) + total_native
        row["realized_trade_pnl_native"] = _dec(row.get("realized_trade_pnl_native")) + total_native
        row["realized_trade_pnl_cny"] = (_dec(row.get("realized_trade_pnl_cny")) + total_cny if total_cny is not None else None)
        row["holding_pnl"] = _dec(base_holding) + total_native if base_holding is not None else None
        row["holding_pnl_native"] = _dec(base_holding_native) + total_native if base_holding_native is not None else None
        row["holding_pnl_cny"] = _dec(base_holding_cny) + total_cny if total_cny is not None and base_holding_cny is not None else None

        if daily_native != 0 or daily_cny != 0:
            base_daily = _dec(row.get("daily_pnl"))
            base_daily_native = _dec(row.get("daily_pnl_native", row.get("daily_pnl")))
            base_daily_cny = row.get("daily_pnl_cny")
            row["daily_realized_trade_pnl"] = _dec(row.get("daily_realized_trade_pnl")) + daily_native
            row["daily_realized_trade_pnl_native"] = (
                _dec(row.get("daily_realized_trade_pnl_native")) + daily_native
            )
            row["daily_realized_trade_pnl_cny"] = (_dec(row.get("daily_realized_trade_pnl_cny")) + daily_cny if daily_cny is not None else None)
            row.setdefault("daily_price_pnl", base_daily)
            row.setdefault("daily_price_pnl_native", base_daily_native)
            row.setdefault("daily_price_pnl_cny", base_daily_cny)
            row.setdefault("daily_total_pnl", base_daily)
            row.setdefault("daily_total_pnl_cny", base_daily_cny)
            row["daily_realized_trade_pnl_only"] = True

    return rows

def _closed_position_rows(cur, *, category=None, minor_category=None, current_keys=None, limit=500):
    """Transactions alone cannot establish a completed position exit."""
    return []


def _ibkr_closed_position_rows(cur, *, current_keys=None, limit=500):
    from domain.investments.ibkr_closed_evidence import verified_ibkr_closed_positions
    return verified_ibkr_closed_positions(cur, current_keys=current_keys, limit=limit)
