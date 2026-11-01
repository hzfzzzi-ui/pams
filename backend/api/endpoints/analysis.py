"""Analysis endpoints for investment diagnostics."""

from __future__ import annotations

from typing import Any

import psycopg2
import psycopg2.extras
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from domain.assets.asset_master import AmbiguousAssetIdentityError, AssetMasterSchemaUnavailable
from domain.assets.lookthrough_state import build_lookthrough_payload
from domain.investments.portfolio_state import get_conn
from domain.investments.return_attribution import build_return_attribution_payload


router = APIRouter(prefix="/analysis", tags=["analysis"])


class ImportIdentityConfirmation(BaseModel):
    expected_updated_at: str = Field(max_length=80)
    asset_type: str = Field(max_length=80)
    currency: str = Field(max_length=16)


@router.get('/asset-import-review')
def pending_import_identities():
    from domain.assets import import_identity_review
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        return {'rows': import_identity_review.pending(cur)}


@router.post('/asset-import-review/{asset_id}')
def confirm_import_identity(asset_id: str, body: ImportIdentityConfirmation, request: Request):
    from domain.assets import import_identity_review
    from domain.imports.account_import_access import same_origin
    same_origin(request)
    try:
        with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            import_identity_review.confirm(cur, asset_id=asset_id, **body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {'status': 'confirmed'}


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
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                payload = build_lookthrough_payload(
                    cur,
                    base_currency=baseCurrency,
                    sync_asset_master=True,
                )
            conn.commit()
    except AmbiguousAssetIdentityError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "ambiguous_asset_identity", "message": "资产身份存在歧义，同步未执行"},
        ) from exc
    except AssetMasterSchemaUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "asset_schema_unavailable", "message": "资产主数据结构不可用，同步未执行"},
        ) from exc
    return {
        "status": "ok",
        "as_of": payload.get("as_of"),
        "master_data": payload.get("master_data"),
        "data_warnings": payload.get("data_warnings") or [],
    }
