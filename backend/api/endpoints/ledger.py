"""Cash ledger workbench API."""

from __future__ import annotations

import os
from typing import Optional

import psycopg2
import psycopg2.extras
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from services.ledger_view import build_ledger_workbench_payload


router = APIRouter()


class LedgerReviewConfirm(BaseModel):
    """Manual confirmation payload for one pending cash ledger row."""

    confirmed_category: str = "其他消费"
    confirmed_subcategory: str = "手动确认"


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
