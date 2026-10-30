"""PortfolioState contract endpoint."""

from __future__ import annotations

from datetime import date as Date

from fastapi import APIRouter, Query

from endpoints import accounts as accounts_endpoint
from domain.risk.portfolio_config import build_portfolio_config_payload
from domain.investments.portfolio_equity_bridge import build_equity_bridge_payload
from domain.risk.portfolio_risk_disclosure import build_portfolio_risk_disclosure_payload
from domain.investments.portfolio_state import build_portfolio_data_quality, build_portfolio_state
from domain.investments.portfolio_valuation_state import (
    build_portfolio_valuation_state_payload,
    publish_portfolio_valuation_state_payload,
)


router = APIRouter()


@router.get("/portfolio/state")
def portfolio_state(baseCurrency: str = Query("CNY")):
    """Return the unified read-only PortfolioState payload."""
    return build_portfolio_state(base_currency=baseCurrency)


@router.get("/portfolio/data-quality")
def portfolio_data_quality(baseCurrency: str = Query("CNY")):
    """Return only read-only PortfolioState data-quality metadata."""
    return build_portfolio_data_quality(base_currency=baseCurrency)


@router.get("/portfolio/config")
def portfolio_config(baseCurrency: str = Query("CNY")):
    """Return target allocation rules and rebalance actions for the config page."""
    return build_portfolio_config_payload(base_currency=baseCurrency)


@router.get("/portfolio/risk-disclosure")
def portfolio_risk_disclosure(baseCurrency: str = Query("CNY")):
    """Return seven-surface risk disclosure for the investment risk page."""
    return build_portfolio_risk_disclosure_payload(base_currency=baseCurrency)


@router.get("/portfolio/equity-bridge")
def portfolio_equity_bridge(
    start: Date = Query(Date(2026, 1, 1)),
    baseCurrency: str = Query("CNY"),
):
    """Return NAV bridge diagnostics between cashflow facts and investment PnL."""
    return build_equity_bridge_payload(
        start_date=start,
        base_currency=baseCurrency,
        overview_payload=accounts_endpoint._overview_payload(),
    )


@router.get("/portfolio/valuation-state")
def portfolio_valuation_state(
    baseCurrency: str = Query("CNY"),
):
    """Return the canonical published portfolio valuation state."""
    return build_portfolio_valuation_state_payload(
        base_currency=baseCurrency,
        refresh=False,
    )


@router.post("/portfolio/valuation-state/publish")
def portfolio_valuation_state_publish(
    baseCurrency: str = Query("CNY"),
    cycle: str = Query("manual_publish"),
):
    """Publish the current PortfolioState valuation as the canonical state."""
    return publish_portfolio_valuation_state_payload(
        base_currency=baseCurrency,
        cycle=cycle,
    )
