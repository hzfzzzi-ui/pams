"""Backfill service for the canonical cashflow fact table."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Mapping, Sequence

import psycopg2.extras


SOURCE_TABLE = "pams_bank_statement_rows"

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

    source_id = str(row["id"])
    source_uid = _first_text(row, "source_uid") or source_id
    source_hash = make_source_hash(SOURCE_TABLE, {"source_uid": source_uid, "source_id": source_id})
    cashflow_type = classify_cashflow_type(row, transfer_match)
    amount = _dec(row.get("amount"))
    currency = _first_text(row, "currency") or base_currency
    fx_rate = _dec(row.get("fx_rate_to_cny", row.get("fx_rate", 1))) or Decimal("1")
    amount_cny = _dec(row.get("amount_cny")) if row.get("amount_cny") is not None else amount * fx_rate
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


def fetch_cashflow_source_rows(cur, *, limit: int | None = None, base_currency: str = "CNY") -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
    """Load source statement rows that have not been materialized by source id."""

    limit_sql = "LIMIT %(limit)s" if limit else ""
    params: dict[str, Any] = {"base": base_currency}
    if limit:
        params["limit"] = int(limit)
    cur.execute(
        f"""
        WITH source_rows AS (
            SELECT r.*
            FROM pams_bank_statement_rows r
            WHERE NOT EXISTS (
                SELECT 1
                FROM pams_cashflow_fact f
                WHERE f.source_table = %(source_table)s
                  AND f.source_id = r.id::text
            )
            ORDER BY r.transaction_date, r.id
            {limit_sql}
        )
        SELECT
            r.id,
            r.import_id,
            r.source_uid,
            r.profile_code,
            r.bank,
            r.account_code,
            r.account_kind,
            r.transaction_date,
            r.posting_date,
            r.amount,
            r.currency,
            r.balance,
            r.original_amount,
            r.original_currency,
            r.category,
            r.subcategory,
            r.auto_classified,
            r.summary,
            r.description,
            r.counterparty,
            r.raw_json,
            CASE
                WHEN COALESCE(NULLIF(r.currency, ''), %(base)s) = %(base)s THEN 1
                ELSE COALESCE(fx_hist.rate, fx_latest.rate, 1)
            END AS fx_rate_to_cny,
            r.amount * CASE
                WHEN COALESCE(NULLIF(r.currency, ''), %(base)s) = %(base)s THEN 1
                ELSE COALESCE(fx_hist.rate, fx_latest.rate, 1)
            END AS amount_cny,
            m.id AS transfer_match_id,
            m.match_key,
            m.match_type,
            m.relation_code,
            m.from_account_code,
            m.to_account_code,
            m.source_row_id,
            m.target_row_id,
            m.amount AS transfer_amount,
            m.currency AS transfer_currency,
            m.match_date,
            m.confidence AS transfer_confidence,
            m.status AS transfer_status
        FROM source_rows r
        LEFT JOIN LATERAL (
            SELECT *
            FROM pams_bank_transfer_matches tm
            WHERE (tm.source_row_id = r.id OR tm.target_row_id = r.id)
              AND COALESCE(tm.status, 'confirmed') <> 'rejected'
            ORDER BY tm.confidence DESC, tm.id DESC
            LIMIT 1
        ) m ON TRUE
        LEFT JOIN LATERAL (
            SELECT rate
            FROM pams_fx_rates_daily fx
            WHERE fx.base_currency = %(base)s
              AND fx.quote_currency = COALESCE(NULLIF(r.currency, ''), %(base)s)
              AND fx.as_of <= r.transaction_date
            ORDER BY fx.as_of DESC
            LIMIT 1
        ) fx_hist ON TRUE
        LEFT JOIN LATERAL (
            SELECT rate
            FROM pams_fx_rates_daily fx
            WHERE fx.base_currency = %(base)s
              AND fx.quote_currency = COALESCE(NULLIF(r.currency, ''), %(base)s)
            ORDER BY fx.as_of DESC
            LIMIT 1
        ) fx_latest ON TRUE
        ORDER BY r.transaction_date, r.id
        """,
        {**params, "source_table": SOURCE_TABLE},
    )

    rows: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    for raw in cur.fetchall():
        row = dict(raw)
        match = None
        if row.get("transfer_match_id") is not None:
            match = {
                "id": row.get("transfer_match_id"),
                "match_key": row.get("match_key"),
                "match_type": row.get("match_type"),
                "relation_code": row.get("relation_code"),
                "from_account_code": row.get("from_account_code"),
                "to_account_code": row.get("to_account_code"),
                "source_row_id": row.get("source_row_id"),
                "target_row_id": row.get("target_row_id"),
                "amount": row.get("transfer_amount"),
                "currency": row.get("transfer_currency"),
                "match_date": row.get("match_date"),
                "confidence": row.get("transfer_confidence"),
                "status": row.get("transfer_status"),
            }
        rows.append((row, match))
    return rows


def backfill_cashflow_facts(cur, *, limit: int | None = None, base_currency: str = "CNY") -> dict[str, int]:
    """Backfill pams_cashflow_fact from raw statement rows."""

    source_rows = fetch_cashflow_source_rows(cur, limit=limit, base_currency=base_currency)
    facts = [build_cashflow_fact(row, match, base_currency=base_currency) for row, match in source_rows]
    inserted = insert_cashflow_facts(cur, facts)
    return {
        "source_count": len(source_rows),
        "inserted_count": inserted,
        "skipped_count": len(source_rows) - inserted,
    }
