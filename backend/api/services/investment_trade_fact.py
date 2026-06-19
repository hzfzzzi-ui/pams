"""Backfill service for the canonical investment trade fact table."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Mapping, Sequence

import psycopg2.extras

from services.cashflow_fact import make_source_hash


MANUAL_TABLE = "pams_investment_transactions"
TRADINGVIEW_TABLE = "pams_tradingview_transactions"
IBKR_TABLE = "pams_ibkr_trades"

BUY_TERMS = {"buy", "bought", "purchase", "\u4e70\u5165", "\u5b9a\u6295"}
SUBSCRIBE_TERMS = {"subscribe", "subscription", "fund_subscription", "\u7533\u8d2d"}
SELL_TERMS = {"sell", "sold", "\u5356\u51fa"}
REDEEM_TERMS = {"redeem", "redemption", "fund_redemption", "\u8d4e\u56de"}
DIVIDEND_TERMS = {"dividend", "distribution", "\u5206\u7ea2", "\u80a1\u606f"}
INTEREST_TERMS = {"interest", "\u5229\u606f"}
COUPON_TERMS = {"coupon", "\u7968\u606f"}
FEE_TERMS = {"fee", "commission", "\u624b\u7eed\u8d39"}
TAX_TERMS = {"tax", "\u7a0e"}
DEPOSIT_TERMS = {"deposit", "cash_deposit", "transfer_in"}
WITHDRAW_TERMS = {"withdraw", "withdrawal", "cash_withdrawal", "transfer_out"}


def _is_fx_pair_symbol(value: Any) -> bool:
    raw = str(value or "").strip().upper().replace("/", ".")
    parts = raw.split(".")
    return len(parts) == 2 and all(len(part) == 3 and part.isalpha() for part in parts)


def _json_ready(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _json_ready(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _dec(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _optional_dec(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    return _dec(value)


def _contains(term: str, values: set[str]) -> bool:
    term_l = term.lower()
    return any(value.lower() == term_l for value in values)


def normalize_side(row: Mapping[str, Any]) -> str:
    raw_side = str(row.get("side") or "").strip().lower()
    quantity = _dec(row.get("quantity"))
    if _contains(raw_side, SUBSCRIBE_TERMS):
        return "subscribe"
    if _contains(raw_side, BUY_TERMS):
        return "buy"
    if _contains(raw_side, REDEEM_TERMS):
        return "redeem"
    if _contains(raw_side, SELL_TERMS):
        return "sell"
    if _contains(raw_side, DIVIDEND_TERMS):
        return "dividend"
    if _contains(raw_side, INTEREST_TERMS):
        return "interest"
    if _contains(raw_side, COUPON_TERMS):
        return "coupon"
    if _contains(raw_side, FEE_TERMS):
        return "fee"
    if _contains(raw_side, TAX_TERMS):
        return "tax"
    if _contains(raw_side, DEPOSIT_TERMS):
        return "deposit"
    if _contains(raw_side, WITHDRAW_TERMS):
        return "withdraw"
    if not raw_side and quantity > 0:
        return "buy"
    if not raw_side and quantity < 0:
        return "sell"
    return raw_side or "unknown"


def classify_trade_type(row: Mapping[str, Any]) -> str:
    side = normalize_side(row)
    if side in {"buy", "sell"} and _is_fx_pair_symbol(row.get("symbol") or row.get("name")):
        return "fx_conversion"
    if side == "subscribe":
        return "fund_subscription"
    if side == "redeem":
        return "fund_redemption"
    if side in {
        "buy",
        "sell",
        "deposit",
        "withdraw",
        "dividend",
        "interest",
        "coupon",
        "fee",
        "tax",
        "fx_conversion",
    }:
        return side
    if side == "split":
        return "split"
    if side in {"transfer_in_kind", "transfer_in"}:
        return "transfer_in_kind"
    if side in {"transfer_out_kind", "transfer_out"}:
        return "transfer_out_kind"
    if side in {"corporate_action", "corp_action"}:
        return "corporate_action"
    if side in {"position_adjustment", "adjustment"}:
        return "position_adjustment"
    return "unknown"


def _gross_amount(row: Mapping[str, Any], side: str) -> Decimal | None:
    gross = _optional_dec(row.get("gross_amount"))
    if gross is not None:
        return abs(gross)
    quantity = _optional_dec(row.get("quantity"))
    price = _optional_dec(row.get("price"))
    if quantity is not None and price is not None:
        return abs(quantity * price)
    if side in {"deposit", "withdraw", "dividend", "interest", "coupon"} and quantity is not None:
        return abs(quantity)
    return None


def _net_cash(
    row: Mapping[str, Any],
    *,
    trade_type: str,
    side: str,
    gross_amount: Decimal | None,
    fee_amount: Decimal,
    tax_amount: Decimal,
) -> Decimal | None:
    source_net_cash = _optional_dec(row.get("net_cash"))
    if source_net_cash is not None:
        return source_net_cash
    gross = gross_amount or Decimal("0")
    if trade_type in {"buy", "fund_subscription"}:
        return -(gross + fee_amount + tax_amount)
    if trade_type in {"sell", "fund_redemption"}:
        return gross - fee_amount - tax_amount
    if trade_type in {"dividend", "interest", "coupon"}:
        return gross - fee_amount - tax_amount
    if trade_type == "fee":
        return -(gross or fee_amount)
    if trade_type == "tax":
        return -(gross or tax_amount)
    if side == "deposit":
        return gross
    if side == "withdraw":
        return -gross
    return None


def _trade_flags(trade_type: str, side: str) -> dict[str, bool]:
    affects_position = trade_type in {
        "buy",
        "sell",
        "fund_subscription",
        "fund_redemption",
        "split",
        "transfer_in_kind",
        "transfer_out_kind",
        "position_adjustment",
    }
    affects_cost_basis = trade_type in {
        "buy",
        "sell",
        "fund_subscription",
        "fund_redemption",
        "position_adjustment",
    }
    affects_realized_pnl = trade_type in {
        "sell",
        "fund_redemption",
        "dividend",
        "interest",
        "coupon",
        "fee",
        "tax",
    }
    affects_cash = trade_type in {
        "buy",
        "sell",
        "fund_subscription",
        "fund_redemption",
        "dividend",
        "interest",
        "coupon",
        "fee",
        "tax",
        "deposit",
        "withdraw",
        "fx_conversion",
    } or side in {"deposit", "withdraw"}
    return {
        "affects_position": affects_position,
        "affects_cost_basis": affects_cost_basis,
        "affects_realized_pnl": affects_realized_pnl,
        "affects_cash": affects_cash,
    }


def build_investment_trade_fact(row: Mapping[str, Any], *, base_currency: str = "CNY") -> dict[str, Any]:
    """Normalize one raw investment transaction row into a trade fact."""

    source_table = str(row["source_table"])
    source_id = str(row["source_id"])
    source_key = str(row.get("source_key") or source_id)
    source_hash = make_source_hash(source_table, {"source_key": source_key, "source_id": source_id})
    raw_side = normalize_side(row)
    trade_type = classify_trade_type(row)
    side = "fx_conversion" if trade_type == "fx_conversion" else raw_side
    fee_amount = abs(_dec(row.get("fee_amount")))
    tax_amount = abs(_dec(row.get("tax_amount")))
    gross = _gross_amount(row, side)
    net_cash = _net_cash(row, trade_type=trade_type, side=side, gross_amount=gross, fee_amount=fee_amount, tax_amount=tax_amount)
    currency = str(row.get("currency") or base_currency)
    fx_rate = _dec(row.get("fx_rate_to_cny", 1)) or Decimal("1")
    realized_pnl = _optional_dec(row.get("realized_pnl"))
    if realized_pnl is None and trade_type in {"dividend", "interest", "coupon", "fee", "tax"}:
        realized_pnl = net_cash
    flags = _trade_flags(trade_type, side)

    return {
        "trade_date": row.get("trade_date"),
        "trade_time": row.get("trade_time"),
        "settle_date": row.get("settle_date"),
        "account_code": str(row.get("account_code") or ""),
        "broker": row.get("broker"),
        "portfolio_code": row.get("portfolio_code"),
        "asset_id": row.get("asset_id"),
        "symbol": row.get("symbol"),
        "name": row.get("name"),
        "market": row.get("market"),
        "currency": currency,
        "trade_type": trade_type,
        "side": side,
        "quantity": _optional_dec(row.get("quantity")),
        "price": _optional_dec(row.get("price")),
        "gross_amount": gross,
        "fee_amount": fee_amount,
        "tax_amount": tax_amount,
        "net_cash": net_cash,
        "fx_rate_to_cny": fx_rate,
        "net_cash_cny": net_cash * fx_rate if net_cash is not None else None,
        **flags,
        "realized_pnl": realized_pnl,
        "realized_pnl_cny": realized_pnl * fx_rate if realized_pnl is not None else None,
        "cashflow_id": row.get("cashflow_id"),
        "source_table": source_table,
        "source_id": source_id,
        "source_key": source_key,
        "source_hash": source_hash,
        "raw_json": {"source_row": _json_ready(dict(row))},
        "review_status": "auto",
    }


def insert_investment_trade_facts(cur, facts: Sequence[Mapping[str, Any]]) -> int:
    """Insert normalized trade facts, skipping duplicates by source keys."""

    inserted = 0
    for fact in facts:
        params = dict(fact)
        params["raw_json"] = psycopg2.extras.Json(_json_ready(params.get("raw_json") or {}))
        cur.execute(
            """
            INSERT INTO pams_investment_trade_fact (
                trade_date, trade_time, settle_date, account_code, broker, portfolio_code,
                asset_id, symbol, name, market, currency,
                trade_type, side, quantity, price, gross_amount, fee_amount, tax_amount,
                net_cash, fx_rate_to_cny, net_cash_cny,
                affects_position, affects_cost_basis, affects_realized_pnl, affects_cash,
                realized_pnl, realized_pnl_cny, cashflow_id,
                source_table, source_id, source_key, source_hash, raw_json, review_status
            ) VALUES (
                %(trade_date)s, %(trade_time)s, %(settle_date)s, %(account_code)s, %(broker)s, %(portfolio_code)s,
                %(asset_id)s, %(symbol)s, %(name)s, %(market)s, %(currency)s,
                %(trade_type)s, %(side)s, %(quantity)s, %(price)s, %(gross_amount)s, %(fee_amount)s, %(tax_amount)s,
                %(net_cash)s, %(fx_rate_to_cny)s, %(net_cash_cny)s,
                %(affects_position)s, %(affects_cost_basis)s, %(affects_realized_pnl)s, %(affects_cash)s,
                %(realized_pnl)s, %(realized_pnl_cny)s, %(cashflow_id)s,
                %(source_table)s, %(source_id)s, %(source_key)s, %(source_hash)s, %(raw_json)s, %(review_status)s
            )
            ON CONFLICT DO NOTHING
            """,
            params,
        )
        inserted += max(int(getattr(cur, "rowcount", 0) or 0), 0)
    return inserted


def _table_exists(cur, table_name: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL AS exists", (f"public.{table_name}",))
    row = cur.fetchone()
    if isinstance(row, Mapping):
        return bool(row.get("exists"))
    return bool(row and row[0])


def _fetch_manual_rows(cur, *, limit: int | None, base_currency: str) -> list[dict[str, Any]]:
    limit_sql = "LIMIT %(limit)s" if limit else ""
    params: dict[str, Any] = {"source_table": MANUAL_TABLE, "base": base_currency}
    if limit:
        params["limit"] = int(limit)
    cur.execute(
        f"""
        SELECT
            %(source_table)s AS source_table,
            tx.id::text AS source_id,
            tx.source || ':' || tx.source_key AS source_key,
            tx.trade_date,
            tx.trade_time,
            NULL::date AS settle_date,
            tx.account_code,
            tx.source AS broker,
            NULL::text AS portfolio_code,
            NULL::text AS asset_id,
            tx.symbol,
            tx.name,
            tx.minor_category AS market,
            tx.currency,
            tx.side,
            tx.quantity,
            tx.price,
            tx.gross_amount,
            tx.fees AS fee_amount,
            0::numeric AS tax_amount,
            NULL::numeric AS net_cash,
            CASE
                WHEN COALESCE(NULLIF(tx.currency, ''), %(base)s) = %(base)s THEN 1
                ELSE COALESCE(fx_latest.rate, 1)
            END AS fx_rate_to_cny,
            NULL::numeric AS realized_pnl,
            tx.raw_json
        FROM pams_investment_transactions tx
        LEFT JOIN LATERAL (
            SELECT rate
            FROM pams_fx_rates_daily fx
            WHERE fx.base_currency = %(base)s
              AND fx.quote_currency = COALESCE(NULLIF(tx.currency, ''), %(base)s)
            ORDER BY fx.as_of DESC
            LIMIT 1
        ) fx_latest ON TRUE
        WHERE NOT EXISTS (
            SELECT 1
            FROM pams_investment_trade_fact f
            WHERE f.source_table = %(source_table)s
              AND f.source_id = tx.id::text
        )
        ORDER BY tx.trade_date, tx.id
        {limit_sql}
        """,
        params,
    )
    return [dict(row) for row in cur.fetchall()]


def _fetch_tradingview_rows(cur, *, limit: int | None, base_currency: str) -> list[dict[str, Any]]:
    limit_sql = "LIMIT %(limit)s" if limit else ""
    params: dict[str, Any] = {"source_table": TRADINGVIEW_TABLE, "base": base_currency}
    if limit:
        params["limit"] = int(limit)
    cur.execute(
        f"""
        SELECT
            %(source_table)s AS source_table,
            tv.id::text AS source_id,
            tv.row_hash AS source_key,
            tv.closing_time::date AS trade_date,
            tv.closing_time AS trade_time,
            NULL::date AS settle_date,
            tv.account_code,
            'tradingview'::text AS broker,
            tv.portfolio_name AS portfolio_code,
            NULL::text AS asset_id,
            COALESCE(tv.normalized_symbol, tv.symbol) AS symbol,
            tv.symbol AS name,
            'CN_A'::text AS market,
            tv.currency,
            tv.side,
            tv.quantity,
            tv.fill_price AS price,
            CASE
                WHEN LOWER(tv.side) IN ('deposit', 'withdraw', 'dividend') THEN ABS(COALESCE(tv.quantity, 0))
                ELSE ABS(COALESCE(tv.quantity, 0) * COALESCE(tv.fill_price, 0))
            END AS gross_amount,
            ABS(COALESCE(tv.commission, 0)) AS fee_amount,
            0::numeric AS tax_amount,
            NULL::numeric AS net_cash,
            CASE
                WHEN COALESCE(NULLIF(tv.currency, ''), %(base)s) = %(base)s THEN 1
                ELSE COALESCE(fx_latest.rate, 1)
            END AS fx_rate_to_cny,
            NULL::numeric AS realized_pnl,
            tv.raw AS raw_json
        FROM pams_tradingview_transactions tv
        LEFT JOIN LATERAL (
            SELECT rate
            FROM pams_fx_rates_daily fx
            WHERE fx.base_currency = %(base)s
              AND fx.quote_currency = COALESCE(NULLIF(tv.currency, ''), %(base)s)
            ORDER BY fx.as_of DESC
            LIMIT 1
        ) fx_latest ON TRUE
        WHERE NOT EXISTS (
            SELECT 1
            FROM pams_investment_trade_fact f
            WHERE f.source_table = %(source_table)s
              AND f.source_id = tv.id::text
        )
        ORDER BY tv.closing_time, tv.id
        {limit_sql}
        """,
        params,
    )
    return [dict(row) for row in cur.fetchall()]


def _fetch_ibkr_rows(cur, *, limit: int | None, base_currency: str) -> list[dict[str, Any]]:
    limit_sql = "LIMIT %(limit)s" if limit else ""
    params: dict[str, Any] = {"source_table": IBKR_TABLE, "base": base_currency}
    if limit:
        params["limit"] = int(limit)
    cur.execute(
        f"""
        SELECT
            %(source_table)s AS source_table,
            ib.id::text AS source_id,
            ib.account_code || ':' || ib.source_trade_key AS source_key,
            ib.trade_date,
            ib.trade_date::timestamp AS trade_time,
            NULL::date AS settle_date,
            ib.account_code,
            'ibkr'::text AS broker,
            NULL::text AS portfolio_code,
            NULL::text AS asset_id,
            ib.symbol,
            ib.name,
            ib.asset_category AS market,
            ib.currency,
            COALESCE(ib.side, CASE WHEN ib.quantity > 0 THEN 'buy' WHEN ib.quantity < 0 THEN 'sell' ELSE NULL END) AS side,
            ib.quantity,
            ib.price,
            ABS(COALESCE(ib.gross_amount, COALESCE(ib.quantity, 0) * COALESCE(ib.price, 0))) AS gross_amount,
            ABS(COALESCE(ib.commission, 0)) AS fee_amount,
            0::numeric AS tax_amount,
            ib.net_cash,
            CASE
                WHEN COALESCE(NULLIF(ib.currency, ''), %(base)s) = %(base)s THEN 1
                ELSE COALESCE(fx_latest.rate, 1)
            END AS fx_rate_to_cny,
            ib.realized_pnl,
            ib.raw_json
        FROM pams_ibkr_trades ib
        LEFT JOIN LATERAL (
            SELECT rate
            FROM pams_fx_rates_daily fx
            WHERE fx.base_currency = %(base)s
              AND fx.quote_currency = COALESCE(NULLIF(ib.currency, ''), %(base)s)
            ORDER BY fx.as_of DESC
            LIMIT 1
        ) fx_latest ON TRUE
        WHERE NOT EXISTS (
            SELECT 1
            FROM pams_investment_trade_fact f
            WHERE f.source_table = %(source_table)s
              AND f.source_id = ib.id::text
        )
          AND NOT (
            COALESCE(ib.source_trade_key, '') LIKE 'manual_%%'
            AND EXISTS (
                SELECT 1
                FROM pams_ibkr_trades official
                WHERE official.id <> ib.id
                  AND official.account_code = ib.account_code
                  AND COALESCE(official.source_trade_key, '') NOT LIKE 'manual_%%'
                  AND official.trade_date = ib.trade_date
                  AND COALESCE(official.symbol, '') = COALESCE(ib.symbol, '')
                  AND COALESCE(UPPER(official.side), '') = COALESCE(UPPER(ib.side), '')
                  AND ROUND(COALESCE(official.quantity, 0)::numeric, 6) = ROUND(COALESCE(ib.quantity, 0)::numeric, 6)
                  AND ROUND(COALESCE(official.price, 0)::numeric, 6) = ROUND(COALESCE(ib.price, 0)::numeric, 6)
            )
          )
        ORDER BY ib.trade_date, ib.id
        {limit_sql}
        """,
        params,
    )
    return [dict(row) for row in cur.fetchall()]


def fetch_investment_trade_source_rows(cur, *, limit: int | None = None, base_currency: str = "CNY") -> list[dict[str, Any]]:
    """Load raw investment transaction rows not yet materialized by source id."""

    rows: list[dict[str, Any]] = []
    if _table_exists(cur, MANUAL_TABLE):
        rows.extend(_fetch_manual_rows(cur, limit=limit, base_currency=base_currency))
    if _table_exists(cur, TRADINGVIEW_TABLE):
        rows.extend(_fetch_tradingview_rows(cur, limit=limit, base_currency=base_currency))
    if _table_exists(cur, IBKR_TABLE):
        rows.extend(_fetch_ibkr_rows(cur, limit=limit, base_currency=base_currency))
    rows.sort(key=lambda r: (r.get("trade_date") or date.min, str(r.get("source_table") or ""), str(r.get("source_id") or "")))
    return rows[:limit] if limit else rows


def backfill_investment_trade_facts(cur, *, limit: int | None = None, base_currency: str = "CNY") -> dict[str, int]:
    """Backfill pams_investment_trade_fact from investment raw ledgers."""

    source_rows = fetch_investment_trade_source_rows(cur, limit=limit, base_currency=base_currency)
    facts = [build_investment_trade_fact(row, base_currency=base_currency) for row in source_rows]
    inserted = insert_investment_trade_facts(cur, facts)
    return {
        "source_count": len(source_rows),
        "inserted_count": inserted,
        "skipped_count": len(source_rows) - inserted,
    }
