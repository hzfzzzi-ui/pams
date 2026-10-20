"""Cash ledger workbench API."""

from __future__ import annotations

import os
from typing import Optional

import psycopg2
import psycopg2.extras
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from services.cashflow_fact import _cashflow_flags as cashflow_flags_for_type
from services.ledger_view import build_ledger_workbench_payload


router = APIRouter()


class LedgerReviewConfirm(BaseModel):
    """Manual confirmation payload for one pending cash ledger row."""

    confirmed_category: str = "其他支出"
    confirmed_subcategory: str = "手动确认"


class LedgerRowUpdate(BaseModel):
    """Manual update payload for one cash ledger fact row."""

    cashflow_type: str
    category: Optional[str] = None
    subcategory: Optional[str] = None
    counterparty: Optional[str] = None
    counter_account_code: Optional[str] = None


LEDGER_MANUAL_CASHFLOW_TYPES = {
    "operating_income",
    "operating_expense",
    "investment_funding",
    "investment_withdrawal",
    "internal_transfer",
    "debt_repayment",
}

LEDGER_MANUAL_DEFAULTS = {
    "operating_income": ("其他收入", "手动确认"),
    "operating_expense": ("其他支出", "手动确认"),
    "investment_funding": ("内部划转", "投资入金"),
    "investment_withdrawal": ("内部划转", "投资出金"),
    "internal_transfer": ("内部划转", "内部转账"),
    "debt_repayment": ("内部划转", "信用卡还款"),
}


def _clean_optional(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


def _manual_category(cashflow_type: str, category: str | None, subcategory: str | None) -> tuple[str, str]:
    default_category, default_subcategory = LEDGER_MANUAL_DEFAULTS[cashflow_type]
    return (_clean_optional(category) or default_category, _clean_optional(subcategory) or default_subcategory)


def get_conn():
    """Create a Postgres connection using the same environment as the API."""
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "pams-postgres"),
        port=int(os.environ.get("PGPORT", "5432")),
        dbname=os.environ.get("PGDATABASE", "pams"),
        user=os.environ.get("PGUSER", "pams"),
        password=os.environ.get("PGPASSWORD", ""),
    )


@router.get("/ledger/workbench")
def ledger_workbench(
    month: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}$"),
    baseCurrency: str = Query("CNY", min_length=3, max_length=3),
    recentDays: int = Query(30, ge=1, le=120),
):
    """Return cashflow KPI, category, trend, and review rows for the ledger page."""

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            return build_ledger_workbench_payload(cur, month=month, base_currency=baseCurrency.upper(), recent_days=recentDays)


@router.post("/ledger/review/{row_id}/confirm")
def confirm_ledger_review(row_id: int, payload: LedgerReviewConfirm):
    """Mark one pending cashflow row as manually confirmed expense."""

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT cashflow_id AS id, event_date AS transaction_date, amount, category, subcategory
                FROM pams_cashflow_fact
                WHERE cashflow_id = %s
                """,
                (row_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="ledger row not found")

            amount = float(row.get("amount") or 0)
            if amount >= 0:
                raise HTTPException(status_code=400, detail="only expense rows can be confirmed here")

            cur.execute(
                """
                UPDATE pams_cashflow_fact
                SET category = %s,
                    subcategory = %s,
                    classification_source = 'manual',
                    auto_classified = FALSE,
                    review_status = 'confirmed',
                    updated_at = now()
                WHERE cashflow_id = %s
                RETURNING cashflow_id AS id, category, subcategory
                """,
                (
                    (payload.confirmed_category or "其他消费").strip(),
                    (payload.confirmed_subcategory or "手动确认").strip(),
                    row_id,
                ),
            )
            updated = cur.fetchone()
        conn.commit()

    return {
        "ok": True,
        "row_id": row_id,
        "category": updated["category"],
        "subcategory": updated["subcategory"],
    }


@router.post("/ledger/rows/{row_id}/update")
def update_ledger_row(row_id: int, payload: LedgerRowUpdate):
    """Manually update one cashflow fact row's business classification."""

    cashflow_type = (payload.cashflow_type or "").strip().lower()
    if cashflow_type not in LEDGER_MANUAL_CASHFLOW_TYPES:
        raise HTTPException(status_code=400, detail="unsupported cashflow_type")

    category, subcategory = _manual_category(cashflow_type, payload.category, payload.subcategory)
    flags = cashflow_flags_for_type(cashflow_type)
    counterparty = _clean_optional(payload.counterparty)
    counter_account_code = _clean_optional(payload.counter_account_code)

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT cashflow_id AS id, counterparty, counter_account_code
                FROM pams_cashflow_fact
                WHERE cashflow_id = %s
                """,
                (row_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="ledger row not found")

            next_counterparty = counterparty if counterparty is not None else row.get("counterparty")
            next_counter_account_code = (
                counter_account_code if counter_account_code is not None else row.get("counter_account_code")
            )
            cur.execute(
                """
                UPDATE pams_cashflow_fact
                SET cashflow_type = %s,
                    category = %s,
                    subcategory = %s,
                    counterparty = %s,
                    counter_account_code = %s,
                    affects_spending = %s,
                    affects_nav = %s,
                    affects_investment_capital = %s,
                    is_internal_transfer = %s,
                    transfer_group_id = CASE
                        WHEN %s IN ('operating_income', 'operating_expense') THEN NULL
                        ELSE COALESCE(transfer_group_id, 'manual-' || %s || '-' || cashflow_id::text)
                    END,
                    classification_source = 'manual',
                    auto_classified = FALSE,
                    review_status = 'confirmed',
                    raw_json = jsonb_set(
                        COALESCE(raw_json, '{}'::jsonb),
                        '{manual_override}',
                        jsonb_build_object(
                            'cashflow_type', %s,
                            'category', %s,
                            'subcategory', %s,
                            'counterparty', %s,
                            'counter_account_code', %s,
                            'updated_at', now()
                        ),
                        true
                    ),
                    updated_at = now()
                WHERE cashflow_id = %s
                RETURNING
                    cashflow_id AS id,
                    cashflow_type,
                    category,
                    subcategory,
                    counterparty,
                    counter_account_code,
                    affects_spending,
                    affects_nav,
                    affects_investment_capital,
                    is_internal_transfer,
                    review_status
                """,
                (
                    cashflow_type,
                    category,
                    subcategory,
                    next_counterparty,
                    next_counter_account_code,
                    flags["affects_spending"],
                    flags["affects_nav"],
                    flags["affects_investment_capital"],
                    flags["is_internal_transfer"],
                    cashflow_type,
                    cashflow_type,
                    cashflow_type,
                    category,
                    subcategory,
                    next_counterparty,
                    next_counter_account_code,
                    row_id,
                ),
            )
            updated = cur.fetchone()
        conn.commit()

    return {"ok": True, "row_id": row_id, "row": dict(updated)}
