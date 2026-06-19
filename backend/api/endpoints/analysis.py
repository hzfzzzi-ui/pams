"""Analysis endpoints for investment diagnostics."""

from __future__ import annotations

from typing import Any

import psycopg2
import psycopg2.extras
from fastapi import APIRouter, Query

from services.lookthrough_state import build_lookthrough_payload
from services.portfolio_state import get_conn
from services.return_attribution import build_return_attribution_payload


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
