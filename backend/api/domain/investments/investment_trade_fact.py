"""Backfill service for the canonical investment trade fact table."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
import logging
from typing import Any, Mapping, Sequence

import psycopg2.extras

from domain.ledger.cashflow_fact import make_source_hash
from domain.brokers.ibkr.ibkr_fee_currency import explicit_ibkr_fee_currency

logger = logging.getLogger(__name__)


_STRICT_FACT_COLUMNS = (
    "trade_date", "trade_time", "settle_date", "account_code", "broker", "portfolio_code",
    "asset_id", "symbol", "name", "market", "currency", "trade_type", "side", "quantity",
    "price", "gross_amount", "fee_amount", "tax_amount", "net_cash", "fx_rate_to_cny",
    "net_cash_cny", "affects_position", "affects_cost_basis", "affects_realized_pnl",
    "affects_cash", "realized_pnl", "realized_pnl_cny", "cashflow_id", "source_table",
    "source_id", "source_key", "source_hash", "raw_json", "review_status",
)

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


def _cny_amount(amount: Decimal | None, rate: Decimal | None) -> Decimal | None:
    """Match PostgreSQL NUMERIC(28,10) rounding for deterministic replay."""
    if amount is None or rate is None:
        return None
    return (amount * rate).quantize(Decimal("0.0000000001"), rounding=ROUND_HALF_UP)


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
    tax_amount: Decimal | None,
) -> Decimal | None:
    source_net_cash = _optional_dec(row.get("net_cash"))
    if source_net_cash is not None:
        return source_net_cash
    if gross_amount is None and trade_type not in {"fee", "tax"}:
        return None
    gross = gross_amount if gross_amount is not None else Decimal("0")
    if trade_type in {"buy", "fund_subscription", "sell", "fund_redemption", "dividend", "interest", "coupon"}:
        if tax_amount is None and row.get("fee_scope") != "total_charges":
            return None
        # Only a known total charge permits an unknown tax component here.
        tax_for_total = tax_amount if tax_amount is not None else Decimal("0")
        if trade_type in {"buy", "fund_subscription"}:
            return -(gross + fee_amount + tax_for_total)
        return gross - fee_amount - tax_for_total
    if trade_type == "fee":
        return -(gross or fee_amount)
    if trade_type == "tax":
        return -gross if gross else (-tax_amount if tax_amount is not None else None)
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

    if str(base_currency).upper() != "CNY":
        raise ValueError("trade fact CNY fields require CNY base currency")

    source_table = str(row["source_table"])
    source_id = str(row["source_id"])
    source_key = str(row.get("source_key") or source_id)
    source_hash = make_source_hash(source_table, {"source_key": source_key, "source_id": source_id})
    raw_side = normalize_side(row)
    trade_type = classify_trade_type(row)
    side = "fx_conversion" if trade_type == "fx_conversion" else raw_side
    fee_amount = abs(_dec(row.get("fee_amount")))
    raw_tax = _optional_dec(row.get("tax_amount"))
    tax_amount = abs(raw_tax) if raw_tax is not None else None
    gross = _gross_amount(row, side)
    net_cash = _net_cash(row, trade_type=trade_type, side=side, gross_amount=gross, fee_amount=fee_amount, tax_amount=tax_amount)
    currency = str(row.get("currency") or "").strip().upper()
    if not currency:
        raise ValueError("trade currency is required")
    fx_rate = _optional_dec(row.get("fx_rate_to_cny"))
    if currency == str(base_currency).upper():
        if fx_rate is not None and fx_rate != Decimal("1"):
            raise ValueError("base currency exchange rate must be one")
        fx_rate = Decimal("1")
    elif fx_rate is not None and (not fx_rate.is_finite() or fx_rate <= 0):
        raise ValueError("trade exchange rate must be finite and positive")
    if (side == "deposit" and net_cash is not None and net_cash < 0) or (
            side == "withdraw" and net_cash is not None and net_cash > 0):
        raise ValueError("trade cash direction conflicts with net cash")
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
        "fee_currency": explicit_ibkr_fee_currency(row, fee_amount),
        "tax_amount": tax_amount,
        "net_cash": net_cash,
        "fx_rate_to_cny": fx_rate,
        "net_cash_cny": _cny_amount(net_cash, fx_rate),
        **flags,
        "realized_pnl": realized_pnl,
        "realized_pnl_cny": _cny_amount(realized_pnl, fx_rate),
        "cashflow_id": row.get("cashflow_id"),
        "source_table": source_table,
        "source_id": source_id,
        "source_key": source_key,
        "source_hash": source_hash,
        "raw_json": {"source_row": _json_ready(dict(row))},
        "review_status": "auto",
    }


def insert_investment_trade_facts(
    cur, facts: Sequence[Mapping[str, Any]], *, strict_source: bool = False,
) -> int:
    """Insert normalized trade facts, skipping duplicates by source keys."""

    has_explicit_fee = any(fact.get('fee_currency') is not None for fact in facts)
    include_fee_currency = False
    if has_explicit_fee:
        cur.execute("""SELECT EXISTS (
            SELECT 1 FROM pg_catalog.pg_attribute
            WHERE attrelid=to_regclass('public.pams_investment_trade_fact')
              AND attname='fee_currency' AND NOT attisdropped
        ) AS exists""")
        row = cur.fetchone()
        include_fee_currency = bool(row.get('exists') if isinstance(row, Mapping) else row and row[0])
        if not include_fee_currency:
            logger.warning('Trade fact fee_currency column unavailable; explicit unit remains in raw evidence only')
    # An unmigrated database keeps the original insert contract; no runtime DDL or backfill.
    fee_column = ', fee_currency' if include_fee_currency else ''
    fee_value = ', %(fee_currency)s' if include_fee_currency else ''
    inserted = 0
    for fact in facts:
        params = dict(fact)
        currency = str(params.get("currency") or "").strip().upper()
        if not currency:
            raise ValueError("trade currency is required")
        rate = _optional_dec(params.get("fx_rate_to_cny"))
        if rate is not None and (not rate.is_finite() or rate <= 0):
            raise ValueError("trade exchange rate must be finite and positive")
        if currency == "CNY":
            if rate != Decimal("1"):
                raise ValueError("base currency exchange rate must be one")
        elif rate is not None:
            cur.execute("""SELECT rate,source FROM pams_fx_rates_daily WHERE base_currency='CNY'
                AND quote_currency=%s AND as_of=%s AND rate > 0
                AND rate::text NOT IN ('NaN', 'Infinity', '-Infinity') FOR SHARE""", (currency, params.get("trade_date")))
            fx_row = cur.fetchone()
            evidence_rate = (fx_row.get("rate") if isinstance(fx_row, Mapping) else fx_row[0]) if fx_row else None
            source = (fx_row.get("source") if isinstance(fx_row, Mapping) else fx_row[1]) if fx_row else None
            if evidence_rate is None or not isinstance(source, str) or not source.strip():
                # Preserve the original-currency fact but never publish an
                # unproven conversion as an exact CNY amount.
                rate = None
                params['fx_rate_to_cny'] = None
                params['net_cash_cny'] = None
                params['realized_pnl_cny'] = None
            elif Decimal(str(evidence_rate)) != rate:
                raise ValueError("trade exchange rate conflicts with business-day evidence")
        for raw_key, converted_key in (("net_cash", "net_cash_cny"), ("realized_pnl", "realized_pnl_cny")):
            raw, converted = _optional_dec(params.get(raw_key)), _optional_dec(params.get(converted_key))
            expected = _cny_amount(raw, rate)
            if converted != expected:
                raise ValueError("converted trade amount conflicts with source and exchange rate")
        side = params.get("trade_type")
        net_cash = _optional_dec(params.get("net_cash"))
        if (side == "deposit" and net_cash is not None and net_cash < 0) or (
                side == "withdraw" and net_cash is not None and net_cash > 0):
            raise ValueError("trade cash direction conflicts with net cash")
        params.setdefault('fee_currency', None)
        params["raw_json"] = psycopg2.extras.Json(_json_ready(params.get("raw_json") or {}))
        if strict_source:
            # Only opt-in immutable sources use this guard; legacy backfills retain their contract.
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (f"{params['source_table']}:{params['source_key']}",))
            columns = _STRICT_FACT_COLUMNS + (("fee_currency",) if include_fee_currency else ())
            comparisons = " AND ".join(
                f"{key} IS NOT DISTINCT FROM %({key})s" + ("::jsonb" if key == "raw_json" else "")
                for key in columns
            )
            cur.execute(
                f"""SELECT ({comparisons}) AS same FROM pams_investment_trade_fact
                    WHERE source_hash = %(source_hash)s OR
                        (source_table = %(source_table)s AND
                            (source_id = %(source_id)s OR source_key = %(source_key)s))
                    FOR UPDATE""", params,
            )
            existing = cur.fetchall()
            if existing:
                if len(existing) != 1 or not all(
                    item.get("same") if isinstance(item, Mapping) else item[0] for item in existing
                ):
                    raise ValueError(f"Conflicting immutable trade source: {params['source_key']}")
                continue
        cur.execute(
            f"""
            INSERT INTO pams_investment_trade_fact (
                trade_date, trade_time, settle_date, account_code, broker, portfolio_code,
                asset_id, symbol, name, market, currency,
                trade_type, side, quantity, price, gross_amount, fee_amount, tax_amount,
                net_cash, fx_rate_to_cny, net_cash_cny,
                affects_position, affects_cost_basis, affects_realized_pnl, affects_cash,
                realized_pnl, realized_pnl_cny, cashflow_id,
                source_table, source_id, source_key, source_hash, raw_json, review_status{fee_column}
            ) VALUES (
                %(trade_date)s, %(trade_time)s, %(settle_date)s, %(account_code)s, %(broker)s, %(portfolio_code)s,
                %(asset_id)s, %(symbol)s, %(name)s, %(market)s, %(currency)s,
                %(trade_type)s, %(side)s, %(quantity)s, %(price)s, %(gross_amount)s, %(fee_amount)s, %(tax_amount)s,
                %(net_cash)s, %(fx_rate_to_cny)s, %(net_cash_cny)s,
                %(affects_position)s, %(affects_cost_basis)s, %(affects_realized_pnl)s, %(affects_cash)s,
                %(realized_pnl)s, %(realized_pnl_cny)s, %(cashflow_id)s,
                %(source_table)s, %(source_id)s, %(source_key)s, %(source_hash)s, %(raw_json)s, %(review_status)s{fee_value}
            )
            ON CONFLICT DO NOTHING
            """,
            params,
        )
        inserted += max(int(getattr(cur, "rowcount", 0) or 0), 0)
        if strict_source and cur.rowcount != 1:
            raise ValueError(f"Concurrent immutable trade source conflict: {params['source_key']}")
    return inserted
