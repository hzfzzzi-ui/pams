"""Backfill service for the canonical cashflow fact table."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping, Sequence

import psycopg2.extras


SOURCE_TABLE = "source_items"

TRANSFER_MATCH_TYPES = {
    "account_transfer",
    "payment_proxy",
    "internal_transfer",
    "investment_funding",
    "credit_card_payment",
}

INVESTMENT_KEYWORDS = (
    "broker",
    "securities",
    "fund",
    "ibkr",
    "\u8bc1\u5238",
    "\u57fa\u91d1",
    "\u7406\u8d22",
)
INVESTMENT_RETURN_KEYWORDS = (
    "dividend",
    "interest",
    "coupon",
    "\u5206\u7ea2",
    "\u5229\u606f",
    "\u80a1\u606f",
    "\u7968\u606f",
)
CREDIT_REPAYMENT_KEYWORDS = (
    "credit card repayment",
    "repayment",
    "\u4fe1\u7528\u5361\u8fd8\u6b3e",
    "\u8fd8\u6b3e",
)
FEE_KEYWORDS = ("fee", "commission", "\u624b\u7eed\u8d39")
TAX_KEYWORDS = ("tax", "\u7a0e")


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


def _first_text(row: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _row_text(row: Mapping[str, Any]) -> str:
    fields = (
        row.get("category"),
        row.get("subcategory"),
        row.get("summary"),
        row.get("description"),
        row.get("counterparty"),
    )
    return " ".join(str(x) for x in fields if x).lower()


def _has_any(text: str, keywords: Sequence[str]) -> bool:
    text_l = text.lower()
    return any(keyword.lower() in text_l for keyword in keywords)


def make_source_hash(source_table: str, source_identity: Mapping[str, Any]) -> str:
    """Return a stable sha256 hash for source-level de-duplication."""

    stable_id = (
        source_identity.get("source_uid")
        or source_identity.get("source_key")
        or source_identity.get("source_id")
    )
    payload = {
        "source_table": source_table,
        "source_identity": stable_id,
    }
    encoded = json.dumps(_json_ready(payload), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _counter_account(row: Mapping[str, Any], transfer_match: Mapping[str, Any] | None) -> str | None:
    if not transfer_match:
        value = row.get("counter_account_code")
        return str(value) if value else None
    if str(transfer_match.get("match_type") or "").lower() == "payment_proxy" and _match_side(row, transfer_match) == "target":
        return None
    account_code = str(row.get("account_code") or "")
    from_account = str(transfer_match.get("from_account_code") or "")
    to_account = str(transfer_match.get("to_account_code") or "")
    if account_code and account_code == from_account:
        return to_account or None
    if account_code and account_code == to_account:
        return from_account or None
    return to_account or from_account or None


def _match_side(row: Mapping[str, Any], transfer_match: Mapping[str, Any] | None) -> str | None:
    if not transfer_match:
        return None
    row_id = str(row.get("id") or "")
    if row_id and row_id == str(transfer_match.get("source_row_id") or ""):
        return "source"
    if row_id and row_id == str(transfer_match.get("target_row_id") or ""):
        return "target"
    return None


def classify_cashflow_type(row: Mapping[str, Any], transfer_match: Mapping[str, Any] | None = None) -> str:
    """Classify a statement row into the P0 cashflow_type whitelist."""

    amount = _dec(row.get("amount"))
    text = _row_text(row)
    if transfer_match:
        match_type = str(transfer_match.get("match_type") or "").lower()
        if match_type == "credit_card_payment":
            return "debt_repayment"
        if match_type == "investment_funding":
            return "investment_withdrawal" if amount > 0 else "investment_funding"
        if match_type == "payment_proxy":
            if _match_side(row, transfer_match) == "target":
                return "operating_income" if amount > 0 else "operating_expense"
            return "internal_transfer"
        if match_type in {"account_transfer", "payment_proxy", "internal_transfer"}:
            return "internal_transfer"

    if amount == 0:
        return "balance_adjustment"
    if _has_any(text, CREDIT_REPAYMENT_KEYWORDS):
        return "debt_repayment"
    if _has_any(text, INVESTMENT_RETURN_KEYWORDS):
        return "investment_return_cash"
    if _has_any(text, TAX_KEYWORDS):
        return "tax"
    if _has_any(text, FEE_KEYWORDS):
        return "fee"
    if _has_any(text, INVESTMENT_KEYWORDS):
        return "investment_withdrawal" if amount > 0 else "investment_funding"
    return "operating_income" if amount > 0 else "operating_expense"


def _cashflow_flags(cashflow_type: str) -> dict[str, bool]:
    affects_spending = cashflow_type in {"operating_income", "operating_expense"}
    affects_investment_capital = cashflow_type in {"investment_funding", "investment_withdrawal"}
    is_internal_transfer = cashflow_type in {"internal_transfer", "fx_conversion"}
    affects_nav = cashflow_type in {
        "operating_income",
        "operating_expense",
        "investment_return_cash",
        "debt_drawdown",
        "fee",
        "tax",
        "balance_adjustment",
    }
    return {
        "affects_spending": affects_spending,
        "affects_nav": affects_nav,
        "affects_investment_capital": affects_investment_capital,
        "is_internal_transfer": is_internal_transfer,
    }


def build_cashflow_fact(
    row: Mapping[str, Any],
    transfer_match: Mapping[str, Any] | None = None,
    *,
    base_currency: str = "CNY",
) -> dict[str, Any]:
    """Normalize one raw bank/payment statement row into a cashflow fact."""

    if base_currency.upper() != 'CNY':
        raise ValueError('cashflow fact CNY fields require CNY base currency')

    source_id = str(row["id"])
    source_uid = _first_text(row, "source_uid") or source_id
    source_hash = make_source_hash(SOURCE_TABLE, {"source_uid": source_uid, "source_id": source_id})
    cashflow_type = classify_cashflow_type(row, transfer_match)
    amount = _dec(row.get("amount"))
    currency = _first_text(row, "currency") or base_currency
    if currency.upper() == base_currency.upper():
        fx_rate = Decimal("1")
        raw_rate = row.get("fx_rate_to_cny", row.get("fx_rate"))
        if raw_rate not in (None, "") and _dec(raw_rate) != fx_rate:
            raise ValueError("CNY cashflow exchange rate must equal one")
    else:
        raw_rate = row.get("fx_rate_to_cny", row.get("fx_rate"))
        if raw_rate in (None, ""):
            raise ValueError("foreign cashflow requires an evidenced transaction-day exchange rate")
        fx_rate = _dec(raw_rate)
        if not fx_rate.is_finite() or fx_rate <= 0:
            raise ValueError("foreign cashflow exchange rate must be positive and finite")
    amount_cny = _dec(row.get("amount_cny")) if row.get("amount_cny") is not None else amount * fx_rate
    if not amount.is_finite() or not amount_cny.is_finite():
        raise ValueError("cashflow amounts must be finite")
    precision = Decimal('0.0001')
    if amount_cny.quantize(precision, rounding=ROUND_HALF_UP) != (amount * fx_rate).quantize(precision, rounding=ROUND_HALF_UP):
        raise ValueError("cashflow amount conflicts with the stated exchange rate")
    flags = _cashflow_flags(cashflow_type)
    match_key = transfer_match.get("match_key") if transfer_match else None
    match_id = transfer_match.get("id") if transfer_match else None

    return {
        "event_date": row.get("transaction_date"),
        "posting_date": row.get("posting_date"),
        "account_code": str(row.get("account_code") or ""),
        "counter_account_code": _counter_account(row, transfer_match),
        "amount": amount,
        "currency": currency,
        "fx_rate_to_cny": fx_rate,
        "amount_cny": amount_cny,
        "cashflow_type": cashflow_type,
        "category": row.get("category"),
        "subcategory": row.get("subcategory"),
        "counterparty": row.get("counterparty"),
        "summary": row.get("summary"),
        "description": row.get("description"),
        **flags,
        "transfer_group_id": str(match_key or match_id) if match_key or match_id else None,
        "source_table": SOURCE_TABLE,
        "source_id": source_id,
        "source_uid": source_uid,
        "source_hash": source_hash,
        "raw_json": {
            "statement_row": _json_ready(dict(row)),
            "transfer_match": _json_ready(dict(transfer_match)) if transfer_match else None,
        },
        "classification_source": "transfer_match" if transfer_match else "bank_statement",
        "confidence": _dec(transfer_match.get("confidence")) if transfer_match else Decimal("0.6000"),
        "review_status": "auto",
    }


def insert_cashflow_facts(cur, facts: Sequence[Mapping[str, Any]]) -> int:
    """Insert normalized cashflow facts, skipping duplicates by source keys."""

    inserted = 0
    for fact in facts:
        params = dict(fact)
        currency = str(params.get('currency') or '').upper()
        amount, rate, converted = (_dec(params.get(key)) for key in ('amount','fx_rate_to_cny','amount_cny'))
        if (not currency or not amount.is_finite() or not converted.is_finite() or not rate.is_finite()
                or rate <= 0 or (currency == 'CNY' and rate != 1)):
            raise ValueError('cashflow fact requires valid currency, amounts and exchange rate')
        precision = Decimal('0.0001')
        if converted.quantize(precision, rounding=ROUND_HALF_UP) != (amount * rate).quantize(precision, rounding=ROUND_HALF_UP):
            raise ValueError('cashflow amount conflicts with the stated exchange rate')
        if currency != 'CNY':
            cur.execute("SELECT to_regclass('public.pams_fx_rates_daily') AS relation")
            found = cur.fetchone()
            relation = found.get('relation') if isinstance(found, dict) else found[0] if found else None
            if not relation:
                raise ValueError('foreign cashflow requires a transaction-day exchange rate')
            cur.execute("""SELECT rate,source,as_of FROM pams_fx_rates_daily WHERE base_currency='CNY'
                           AND quote_currency=%s AND as_of=%s FOR SHARE""", (currency, params.get('event_date')))
            found = cur.fetchone()
            recorded_rate = found.get('rate') if isinstance(found, dict) else found[0] if found else None
            source = found.get('source') if isinstance(found, dict) else found[1] if found else None
            if recorded_rate is None or _dec(recorded_rate) != rate or not isinstance(source,str) or not source.strip():
                raise ValueError('foreign cashflow exchange rate has no matching transaction-day evidence')
            fx_day = found.get('as_of') if isinstance(found, dict) else found[2]
            raw = dict(params.get('raw_json') or {})
            preview_fx = raw.get('fx_evidence')
            if preview_fx is not None and (not isinstance(preview_fx, dict) or preview_fx.get('source') != source
                    or preview_fx.get('as_of') != fx_day.isoformat() or preview_fx.get('quote_currency') != currency
                    or preview_fx.get('base_currency') != 'CNY' or preview_fx.get('basis') != 'daily_market_projection'
                    or _dec(preview_fx.get('rate')) != rate):
                raise ValueError('foreign cashflow exchange rate evidence changed after preview')
            raw['fx_evidence'] = {'basis':'daily_market_projection','base_currency':'CNY',
                                  'quote_currency':currency,'as_of':fx_day,'rate':rate,'source':source}
            params['raw_json'] = raw
        params["raw_json"] = psycopg2.extras.Json(_json_ready(params.get("raw_json") or {}))
        cur.execute(
            """
            INSERT INTO pams_cashflow_fact (
                event_date, posting_date, account_code, counter_account_code,
                amount, currency, fx_rate_to_cny, amount_cny,
                cashflow_type, category, subcategory, counterparty, summary, description,
                affects_spending, affects_nav, affects_investment_capital,
                is_internal_transfer, transfer_group_id,
                source_table, source_id, source_uid, source_hash, raw_json,
                classification_source, confidence, review_status
            ) VALUES (
                %(event_date)s, %(posting_date)s, %(account_code)s, %(counter_account_code)s,
                %(amount)s, %(currency)s, %(fx_rate_to_cny)s, %(amount_cny)s,
                %(cashflow_type)s, %(category)s, %(subcategory)s, %(counterparty)s, %(summary)s, %(description)s,
                %(affects_spending)s, %(affects_nav)s, %(affects_investment_capital)s,
                %(is_internal_transfer)s, %(transfer_group_id)s,
                %(source_table)s, %(source_id)s, %(source_uid)s, %(source_hash)s, %(raw_json)s,
                %(classification_source)s, %(confidence)s, %(review_status)s
            )
            ON CONFLICT DO NOTHING
            """,
            params,
        )
        inserted += max(int(getattr(cur, "rowcount", 0) or 0), 0)
    return inserted
