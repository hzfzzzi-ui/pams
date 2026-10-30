"""Analysis endpoints for investment diagnostics."""

from __future__ import annotations

from typing import Any

import psycopg2
import psycopg2.extras
from fastapi import APIRouter, Query

from domain.assets.lookthrough_state import build_lookthrough_payload
from domain.investments.portfolio_state import get_conn
from domain.investments.return_attribution import build_return_attribution_payload


router = APIRouter(prefix="/analysis", tags=["analysis"])


@router.get("/lookthrough")
def lookthrough_summary(baseCurrency: str = Query("CNY")) -> dict[str, Any]:
    """Return the current portfolio lookthrough exposure summary."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            return build_lookthrough_payload(cur, base_currency=baseCurrency)


@router.get("/return-attribution")
def return_attribution_summary(baseCurrency: str = Query("CNY")) -> dict[str, Any]:
    """Return portfolio PnL attribution by holding and exposure dimensions."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            return build_return_attribution_payload(cur, base_currency=baseCurrency)


@router.post("/asset-master/sync")
def sync_asset_master_from_current_portfolio(baseCurrency: str = Query("CNY")) -> dict[str, Any]:
    """Explicitly sync asset master from current lookthrough analysis."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            payload = build_lookthrough_payload(
                cur,
                base_currency=baseCurrency,
                sync_asset_master=True,
            )
        conn.commit()
    return {
        "status": "ok",
        "as_of": payload.get("as_of"),
        "master_data": payload.get("master_data"),
        "data_warnings": payload.get("data_warnings") or [],
    }
