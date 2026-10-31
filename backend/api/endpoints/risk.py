"""Portfolio risk endpoints for the PAMS risk module."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from domain.risk.risk_state import build_risk_state

router = APIRouter(prefix="/risk", tags=["risk"])


@router.get("/summary")
def risk_summary() -> dict[str, Any]:
    """Return current portfolio risk metrics."""
    return build_risk_state()
