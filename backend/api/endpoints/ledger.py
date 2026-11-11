"""Cash ledger workbench API."""

from __future__ import annotations

import os
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from domain.ledger.cashflow_fact import _cashflow_flags as cashflow_flags_for_type
from domain.ledger.ledger_view import build_ledger_workbench_payload, _table_exists


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

LEDGER_NEW_MANUAL_CASHFLOW_TYPES = {"fx_conversion", "debt_drawdown", "fee"}

LEDGER_MANUAL_DEFAULTS = {
    "operating_income": ("其他收入", "手动确认"),
    "operating_expense": ("其他支出", "手动确认"),
    "investment_funding": ("内部划转", "投资入金"),
    "investment_withdrawal": ("内部划转", "投资出金"),
    "internal_transfer": ("内部划转", "内部转账"),
    "debt_repayment": ("内部划转", "信用卡还款"),
    "fx_conversion": ("内部划转", "换汇转账"),
    "debt_drawdown": ("融资", "信用卡取现"),
    "fee": ("其他支出", "银行费用"),
}


def _clean_optional(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


def _manual_category(cashflow_type: str, category: str | None, subcategory: str | None) -> tuple[str, str]:
    default_category, default_subcategory = LEDGER_MANUAL_DEFAULTS[cashflow_type]
    return (_clean_optional(category) or default_category, _clean_optional(subcategory) or default_subcategory)


def _verified_new_cashflow(cur, row_id: int):
    from domain.ledger.cashflow_read_v2 import load_cash_rows
    cur.execute('SELECT event_date FROM pams_cashflow_fact WHERE cashflow_id=%s FOR UPDATE', (row_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail='ledger row not found')
    rows = load_cash_rows(cur, start=row['event_date'], end=row['event_date'], base_currency='CNY')
    fact = next((item for item in rows if item['id'] == row_id), None)
    if not fact or fact['source_evidence_status'] != 'verified':
        raise HTTPException(status_code=409, detail='source evidence requires review')
    return fact


def get_conn():
    """Create a Postgres connection using the same environment as the API."""
    from domain.imports.isolation import guarded_connect

    return guarded_connect(
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
    includeTransferPairs: bool = False,
    view: str = Query("full", pattern="^(full|overview|stream|transfers)$"),
    period: str = Query("ytd", pattern="^(month|ytd)$"),
    account: str = Query("", max_length=120),
    accountGroup: str = Query("", max_length=120),
    flow: str = Query("", pattern="^(|income|expense|income_expense|internal_transfer|cashflow|cash_transfer)$"),
    search: str = Query("", max_length=200),
    matchingAccounts: str = Query("", max_length=1800),
    sort: str = Query("", pattern="^(|amount_asc|amount_desc)$"),
    offset: int = Query(0, ge=0),
    limit: int = Query(80, ge=1, le=100),
):
    """Return cashflow KPI, category, trend, and review rows for the ledger page."""
    if month:
        try:
            requested_month = date.fromisoformat(f"{month}-01")
        except ValueError as exc:
            raise HTTPException(422, detail="月份格式无效") from exc
        if requested_month > datetime.now(ZoneInfo("Asia/Shanghai")).date().replace(day=1):
            raise HTTPException(422, detail="历史月份不能晚于当前业务月")

    with get_conn() as conn:
        conn.set_session(isolation_level="REPEATABLE READ", readonly=True)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            return build_ledger_workbench_payload(cur, month=month, base_currency=baseCurrency.upper(),
                                                  recent_days=recentDays, include_transfer_pairs=includeTransferPairs,
                                                  view=view, period=period, account=account, account_group=accountGroup,
                                                  flow=flow, search=search,
                                                  sort=sort, offset=offset, limit=limit,
                                                  matching_accounts=set(matchingAccounts.split(",")) if matchingAccounts else None)


@router.post("/ledger/review/{row_id}/confirm")
def confirm_ledger_review(row_id: int, payload: LedgerReviewConfirm):
    """Mark one pending cashflow row as manually confirmed expense."""

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if _table_exists(cur, 'source_records'):
                from domain.ledger.cashflow_classification import confirm
                row = _verified_new_cashflow(cur, row_id)
                if row['amount'] >= 0:
                    raise HTTPException(status_code=400, detail='only expense rows can be confirmed here')
                category = (payload.confirmed_category or '其他消费').strip()
                subcategory = (payload.confirmed_subcategory or '手动确认').strip()
                confirm(cur, cashflow_id=row_id, cashflow_type='operating_expense', category=category, subcategory=subcategory)
                return {'ok': True, 'row_id': row_id, 'category': category, 'subcategory': subcategory}
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
    if cashflow_type not in LEDGER_MANUAL_CASHFLOW_TYPES | LEDGER_NEW_MANUAL_CASHFLOW_TYPES:
        raise HTTPException(status_code=400, detail="unsupported cashflow_type")

    category, subcategory = _manual_category(cashflow_type, payload.category, payload.subcategory)
    counterparty = _clean_optional(payload.counterparty)
    counter_account_code = _clean_optional(payload.counter_account_code)

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if _table_exists(cur, 'source_records'):
                from domain.ledger.cashflow_classification import confirm
                row = _verified_new_cashflow(cur, row_id)
                if counterparty is not None and counterparty != _clean_optional(row.get('counterparty')):
                    raise HTTPException(status_code=409, detail='source counterparty cannot be changed by classification')
                if counter_account_code is not None:
                    raise HTTPException(status_code=409, detail='counter account requires a separate transfer confirmation')
                confirm(cur, cashflow_id=row_id, cashflow_type=cashflow_type, category=category, subcategory=subcategory)
                return {'ok': True, 'row_id': row_id, 'cashflow_type': cashflow_type,
                        'category': category, 'subcategory': subcategory}
            if cashflow_type in LEDGER_NEW_MANUAL_CASHFLOW_TYPES:
                raise HTTPException(status_code=400, detail="unsupported cashflow_type")
            flags = cashflow_flags_for_type(cashflow_type)
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
