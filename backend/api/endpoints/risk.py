"""Portfolio risk endpoints for the PAMS risk module."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services.risk_state import build_risk_state, run_pretrade_check

router = APIRouter(prefix="/risk", tags=["risk"])


class PreTradeRequest(BaseModel):
    account_code: str = "manual"
    asset_id: str
    asset_name: str | None = None
    asset_type: str = "manual_asset"
    amount_cny: float


@router.get("/summary")
def risk_summary() -> dict[str, Any]:
    """Return current portfolio risk metrics."""
    return build_risk_state()


@router.post("/pretrade-check")
def pretrade_check(req: PreTradeRequest) -> dict[str, Any]:
    """Check whether a proposed cash-funded buy violates configured risk limits."""
    payload = req.model_dump() if hasattr(req, "model_dump") else req.dict()
    try:
        return run_pretrade_check(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
