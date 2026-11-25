"""Investment transaction ledger API."""

from __future__ import annotations

import os
from datetime import date, datetime
from typing import Any, Optional

import psycopg2.extras
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from domain.investments.trades.trade_endpoint_service import (
    TradeServiceError,
    apply_trade_auto_review,
    load_reported_investment_returns,
    query_investment_transaction_options,
    query_investment_transactions,
    query_trade_attribution,
    query_trade_attribution_summary,
    query_trade_auto_review_queue,
    query_trade_campaigns,
    update_trade_review,
)

router = APIRouter()


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


def ensure_ledger_table(conn) -> None:
    """Investment ledger tables are provisioned by database schema migrations."""
    return None


class InvestmentTransactionCreate(BaseModel):
    """Payload for one manually recorded investment transaction."""

    trade_date: date
    trade_time: Optional[datetime] = None
    account_code: str
    major_category: Optional[str] = None
    minor_category: Optional[str] = None
    symbol: Optional[str] = None
    name: str = Field(min_length=1)
    side: str = Field(min_length=1)
    quantity: Optional[float] = None
    price: Optional[float] = None
    gross_amount: Optional[float] = None
    fees: Optional[float] = 0
    currency: str = "CNY"
    status: Optional[str] = "recorded"
    notes: Optional[str] = None


class InvestmentTradeReviewUpdate(BaseModel):
    """Manual review fields attached to one investment trade fact."""

    buy_reason: Optional[str] = None
    sell_reason: Optional[str] = None
    review_tags: list[str] = Field(default_factory=list)
    trade_intent: Optional[str] = None
    trade_plan: Optional[str] = None
    review_note: Optional[str] = None
    review_due_date: Optional[date] = None
    review_status: Optional[str] = None
    initial_position_identity: Optional[str] = None
    current_position_identity: Optional[str] = None
    identity_confidence: Optional[str] = None
    intended_holding_period: Optional[str] = None
    buy_logic: Optional[str] = None
    add_condition: Optional[str] = None
    reduce_condition: Optional[str] = None
    sell_condition: Optional[str] = None
    max_acceptable_loss_pct: Optional[float] = None
    max_position_pct: Optional[float] = None
    next_review_date: Optional[date] = None
    sell_rule_type: Optional[str] = None
    identity_transition_type: Optional[str] = None
    identity_transition_triggers: list[str] = Field(default_factory=list)
    identity_transition_evidence: Optional[str] = None


def _provided_fields(model: BaseModel) -> set[str]:
    fields: Any = getattr(model, "model_fields_set", None)
    if fields is None:
        fields = getattr(model, "__fields_set__", set())
    return set(fields)


def _model_fields(model: BaseModel) -> dict[str, Any]:
    dump = getattr(model, "model_dump", None)
    if callable(dump):
        return dump()
    return model.dict()


@router.get("/investment-returns/reported")
def reported_investment_returns():
    """Return manually confirmed investment return notes shown under investment assets."""
    return load_reported_investment_returns()


@router.get("/investment-transactions")
def list_investment_transactions(
    start: Optional[date] = None,
    end: Optional[date] = None,
    account_code: Optional[str] = None,
    source: Optional[str] = None,
    side: Optional[str] = None,
    review_status: Optional[str] = None,
    asset_key: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = Query(500, ge=1, le=5000),
):
    """List normalized investment transactions across manual and imported ledgers."""
    try:
        with get_conn() as conn:
            ensure_ledger_table(conn)
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                return query_investment_transactions(
                    cur,
                    start=start,
                    end=end,
                    account_code=account_code,
                    source=source,
                    side=side,
                    review_status=review_status,
                    asset_key=asset_key,
                    search=search,
                    limit=limit,
                )
    except TradeServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.post("/investment-transactions")
def create_investment_transaction(tx: InvestmentTransactionCreate):
    """Legacy entry: all new imports require the shared draft/preview approval."""
    raise HTTPException(
        409,
        detail={
            "code": "import_review_required",
            "message": "请通过导入资料统一核对后入库",
            "destination": "account_import.html?kind=trade&mode=manual",
        },
    )


@router.get("/investment-transactions/attribution/summary")
def investment_trade_attribution_summary(
    start: Optional[date] = None,
    end: Optional[date] = None,
    limit: int = Query(300, ge=1, le=1000),
):
    """Return event-level attribution grouped by manual review enums."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            return query_trade_attribution_summary(cur, start=start, end=end, limit=limit)


@router.get("/investment-transactions/campaigns")
def investment_trade_campaigns(
    start: Optional[date] = None,
    end: Optional[date] = None,
    account_code: Optional[str] = None,
    asset_key: Optional[str] = None,
    status: str = Query("all"),
    limit: int = Query(100, ge=1, le=500),
):
    """Return read-only account/asset position lifecycle campaigns."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            return query_trade_campaigns(
                cur,
                start=start,
                end=end,
                account_code=account_code,
                asset_key=asset_key,
                status=status,
                limit=limit,
            )


@router.get("/investment-transactions/auto-review/queue")
def investment_trade_auto_review_queue(
    kind: str = Query("needs_human_review"),
    start: Optional[date] = None,
    end: Optional[date] = None,
    limit: int = Query(100, ge=1, le=300),
):
    """Return system review queue for passive trade-review agents."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            return query_trade_auto_review_queue(
                cur, kind=kind, start=start, end=end, limit=limit
            )


@router.post("/investment-transactions/auto-review/apply")
def investment_trade_auto_review_apply(
    start: Optional[date] = None,
    end: Optional[date] = None,
    limit: int = Query(1000, ge=1, le=5000),
    dry_run: bool = Query(True),
    include_reviewed_tags: bool = Query(True),
):
    """Conservatively fill missing trade review annotations from system review."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            result, should_commit = apply_trade_auto_review(
                cur,
                start=start,
                end=end,
                limit=limit,
                dry_run=dry_run,
                include_reviewed_tags=include_reviewed_tags,
            )
            if should_commit:
                conn.commit()
            return result


@router.get("/investment-transactions/{trade_id}/attribution")
def investment_trade_attribution(trade_id: int):
    """Return automatic post-trade attribution for one trade event."""
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                return query_trade_attribution(cur, trade_id)
    except TradeServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.post("/investment-transactions/{trade_id}/review")
def update_investment_trade_review(trade_id: int, review: InvestmentTradeReviewUpdate):
    """Update manual review fields for one investment trade fact."""
    try:
        with get_conn() as conn:
            ensure_ledger_table(conn)
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                payload = update_trade_review(
                    cur,
                    trade_id=trade_id,
                    fields=_model_fields(review),
                    provided_fields=_provided_fields(review),
                )
            conn.commit()
            return payload
    except TradeServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.get("/investment-transactions/options")
def investment_transaction_options():
    """Return filter and form options for the investment ledger page."""
    with get_conn() as conn:
        ensure_ledger_table(conn)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            return query_investment_transaction_options(cur)
