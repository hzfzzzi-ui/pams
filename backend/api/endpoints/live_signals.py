"""
PAMS inbound live signal package API.

PAMS receives versioned packages from the quantitative research platform, stores
them idempotently, and exposes read-only package/action views for app/web
consumers. Strategy calculation remains outside PAMS.
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Any, Literal, Optional

import psycopg2
import psycopg2.extras
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


router = APIRouter(prefix="/live-signals", tags=["live_signals"])

PACKAGE_SCHEMA_VERSION = "daily_position_adjustment_package@1"

PackageStatus = Literal["ready", "no_op", "blocked", "invalid"]
PublishMode = Literal["live", "dry_run", "audit_copy"]
ActionType = Literal["buy", "sell", "hold", "increase", "reduce", "rebalance"]
ActionStatus = Literal["proposed", "no_op", "blocked"]
ValidationStatus = Literal["passed", "blocked", "invalid"]
RiskSeverity = Literal["info", "warning", "blocker"]
TransactionStatus = Literal[
    "received",
    "validated",
    "notified",
    "needs_review",
    "approved",
    "rejected",
    "submitted",
    "partially_filled",
    "filled",
    "cancelled",
    "expired",
    "blocked",
]


def get_conn():
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "pams-postgres"),
        port=int(os.environ.get("PGPORT", "5432")),
        dbname=os.environ.get("PGDATABASE", "pams"),
        user=os.environ.get("PGUSER", "pams"),
        password=os.environ.get("PGPASSWORD", ""),
    )


def _norm(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


class _StrictContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PackageHeader(_StrictContractModel):
    schema_version: str = Field(..., min_length=1)
    package_id: str = Field(..., min_length=1)
    idempotency_key: str = Field(..., min_length=1)
    generated_at: str = Field(..., min_length=1)

    @field_validator("schema_version")
    @classmethod
    def _schema_version_must_match(cls, value: str) -> str:
        if value != PACKAGE_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {PACKAGE_SCHEMA_VERSION}")
        return value


class PackageSource(_StrictContractModel):
    strategy_id: str = Field(..., min_length=1)
    strategy_run_id: str = Field(..., min_length=1)
    baseline_id: str = Field(..., min_length=1)
    config_hash: str = Field(..., min_length=1)
    execution_contract_hash: str = Field(..., min_length=1)
    publish_mode: PublishMode = "live"


class PackageClock(_StrictContractModel):
    signal_date: str = Field(..., min_length=1)
    execution_date: str = Field(..., min_length=1)
    signal_clock: str = Field(..., min_length=1)
    execution_clock: str = Field(..., min_length=1)
    reference_price_basis: str = Field(..., min_length=1)


class PackageScope(_StrictContractModel):
    market: str = Field(..., min_length=1)
    asset_scope: str = Field(..., min_length=1)
    asset_class: str = Field(..., min_length=1)
    account_scope: str = Field(..., min_length=1)
    currency: str = Field(..., min_length=1)
    symbol_namespace: str = Field(..., min_length=1)


class PackageInputs(_StrictContractModel):
    market_snapshot_id: str = Field(..., min_length=1)
    holdings_snapshot_id: str = Field(..., min_length=1)
    cash_snapshot_id: str = Field(..., min_length=1)
    reconcile_id: str = Field(..., min_length=1)


class TargetPosition(_StrictContractModel):
    symbol: str = Field(..., min_length=1)
    target_weight: float = Field(..., ge=0.0, le=1.0)
    target_qty: Optional[int] = Field(default=None, ge=0)
    current_qty: Optional[int] = Field(default=None, ge=0)
    reference_price: Optional[float] = Field(default=None, ge=0)


class PackageAction(_StrictContractModel):
    action_id: str = Field(..., min_length=1)
    idempotency_key: str = Field(..., min_length=1)
    symbol: str = Field(..., min_length=1)
    market: str = Field(..., min_length=1)
    asset_class: str = Field(..., min_length=1)
    action_type: ActionType
    action_status: ActionStatus = "proposed"
    current_qty: int = Field(..., ge=0)
    target_qty: int = Field(..., ge=0)
    delta_qty: int
    suggested_qty: int = Field(..., ge=0)
    target_weight: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    reference_price: float = Field(..., ge=0)
    reference_price_basis: str = Field(..., min_length=1)
    estimated_amount: Optional[float] = Field(default=None, ge=0.0)
    reason_code: str = Field(..., min_length=1)
    reason_tags: list[str] = Field(...)
    reason_codes: list[str] = Field(default_factory=list)
    blocked: bool = Field(...)
    blocked_reason: str = ""
    expires_at: str = Field(..., min_length=1)


class RiskFlag(_StrictContractModel):
    code: str = Field(..., min_length=1)
    severity: RiskSeverity
    message: str = Field(..., min_length=1)


class PackageValidation(_StrictContractModel):
    status: ValidationStatus
    failure_reason_codes: list[str] = Field(default_factory=list)
    stale: bool = False
    drifted: bool = False


class DailyPositionAdjustmentPackage(_StrictContractModel):
    header: PackageHeader
    source: PackageSource
    clock: PackageClock
    scope: PackageScope
    inputs: PackageInputs
    package_status: PackageStatus
    target_positions: list[TargetPosition] = Field(...)
    action_sheet: list[PackageAction] = Field(...)
    risk_flags: list[RiskFlag] = Field(...)
    validation: PackageValidation

    @model_validator(mode="after")
    def _validate_status_contract(self) -> "DailyPositionAdjustmentPackage":
        if self.package_status == "ready" and not self.action_sheet:
            raise ValueError("ready package requires at least one action")
        if self.package_status in {"blocked", "invalid"} and not self.validation.failure_reason_codes:
            raise ValueError("blocked/invalid package requires failure_reason_codes")
        if self.validation.status == "passed" and self.validation.failure_reason_codes:
            raise ValueError("passed validation must not include failure_reason_codes")
        if self.validation.status in {"blocked", "invalid"} and not self.validation.failure_reason_codes:
            raise ValueError("blocked/invalid validation requires failure_reason_codes")
        return self


def validate_daily_position_adjustment_package(payload: dict[str, Any]) -> DailyPositionAdjustmentPackage:
    return DailyPositionAdjustmentPackage.model_validate(payload)


class ActionDecisionRequest(BaseModel):
    operator: str = Field(default="manual", min_length=1)
    reason: Optional[str] = None
    final_qty: Optional[int] = Field(default=None, ge=0)


class ActionLifecycleUpdateRequest(BaseModel):
    status: TransactionStatus
    operator: str = Field(default="manual", min_length=1)
    reason: Optional[str] = None
    final_qty: Optional[int] = Field(default=None, ge=0)
    fill_qty: Optional[int] = Field(default=None, ge=0)
    fill_price: Optional[float] = Field(default=None, ge=0)


def _jsonb(value: Any) -> Any:
    return psycopg2.extras.Json(value)


def _package_payload(package: DailyPositionAdjustmentPackage) -> dict[str, Any]:
    return package.model_dump(mode="json", exclude_none=True)


def _fetch_package(cur, package_id: str) -> dict[str, Any] | None:
    cur.execute(
        """
        SELECT *
        FROM pams_live_signal_packages
        WHERE package_id = %s
        """,
        (package_id,),
    )
    package_row = cur.fetchone()
    if not package_row:
        return None

    cur.execute(
        """
        SELECT *
        FROM pams_live_signal_actions
        WHERE package_id = %s
        ORDER BY id ASC
        """,
        (package_id,),
    )
    action_rows = cur.fetchall()
    package = {k: _norm(v) for k, v in dict(package_row).items()}
    package["action_sheet"] = [{k: _norm(v) for k, v in dict(row).items()} for row in action_rows]
    return package


def _package_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "package_id": _norm(row.get("package_id")),
        "schema_version": _norm(row.get("schema_version")),
        "strategy_id": _norm(row.get("strategy_id")),
        "strategy_run_id": _norm(row.get("strategy_run_id")),
        "baseline_id": _norm(row.get("baseline_id")),
        "config_hash": _norm(row.get("config_hash")),
        "publish_mode": _norm(row.get("publish_mode")),
        "signal_date": _norm(row.get("signal_date")),
        "execution_date": _norm(row.get("execution_date")),
        "account_scope": _norm(row.get("account_scope")),
        "package_status": _norm(row.get("package_status")),
        "validation_status": _norm(row.get("validation_status")),
        "failure_reason_codes": _norm(row.get("failure_reason_codes") or []),
        "risk_flags": _norm(row.get("risk_flags") or []),
        "received_at": _norm(row.get("received_at")),
    }


def _notification_body(package: dict[str, Any]) -> str:
    action_lines = []
    for action in package.get("action_sheet") or []:
        action_lines.append(
            "- {action_id} {action_type} {symbol} qty={qty} reason={reason}".format(
                action_id=action.get("action_id"),
                action_type=action.get("action_type"),
                symbol=action.get("symbol"),
                qty=action.get("suggested_qty"),
                reason=action.get("reason_code"),
            )
        )
    if not action_lines:
        action_lines.append("- no actionable order")
    risk_codes = [
        str(item.get("code"))
        for item in (package.get("risk_flags") or [])
        if isinstance(item, dict) and item.get("code")
    ]
    return "\n".join(
        [
            f"package_id={package.get('package_id')}",
            f"strategy_id={package.get('strategy_id')}",
            f"strategy_run_id={package.get('strategy_run_id')}",
            f"config_hash={package.get('config_hash')}",
            f"signal_date={package.get('signal_date')}",
            f"package_status={package.get('package_status')}",
            f"risk_flags={','.join(risk_codes) if risk_codes else 'none'}",
            "actions:",
            *action_lines,
        ]
    )


def _find_action(package: dict[str, Any], action_id: str) -> dict[str, Any] | None:
    for action in package.get("action_sheet") or []:
        if action.get("action_id") == action_id:
            return action
    return None


def _upsert_transaction_lifecycle(
    cur,
    *,
    package: dict[str, Any],
    action: dict[str, Any],
    status: TransactionStatus,
    operator: str,
    reason: Optional[str] = None,
    final_qty: Optional[int] = None,
    fill_qty: Optional[int] = None,
    fill_price: Optional[float] = None,
) -> dict[str, Any]:
    cur.execute(
        """
        INSERT INTO pams_live_signal_transaction_lifecycle (
            source_package_id, source_action_id,
            strategy_id, strategy_run_id, signal_date, execution_date, config_hash,
            account_scope,
            symbol, market, action_type, transaction_status,
            requested_qty, final_qty, fill_qty, fill_price,
            operator, decision_reason, raw_action_payload
        ) VALUES (
            %s, %s,
            %s, %s, %s, %s, %s,
            %s,
            %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s
        )
        ON CONFLICT (source_package_id, source_action_id) DO UPDATE
        SET transaction_status = EXCLUDED.transaction_status,
            final_qty = EXCLUDED.final_qty,
            fill_qty = EXCLUDED.fill_qty,
            fill_price = EXCLUDED.fill_price,
            operator = EXCLUDED.operator,
            decision_reason = EXCLUDED.decision_reason,
            raw_action_payload = EXCLUDED.raw_action_payload,
            updated_at = NOW()
        RETURNING *
        """,
        (
            package["package_id"],
            action["action_id"],
            package["strategy_id"],
            package["strategy_run_id"],
            package["signal_date"],
            package["execution_date"],
            package["config_hash"],
            package["account_scope"],
            action["symbol"],
            action["market"],
            action["action_type"],
            status,
            int(action.get("suggested_qty") or 0),
            final_qty,
            fill_qty,
            fill_price,
            operator,
            reason,
            _jsonb(action),
        ),
    )
    row = cur.fetchone()
    return {k: _norm(v) for k, v in dict(row).items()}


def _insert_package(cur, package: DailyPositionAdjustmentPackage) -> None:
    payload = _package_payload(package)
    cur.execute(
        """
        INSERT INTO pams_live_signal_packages (
            schema_version, package_id, idempotency_key, generated_at,
            strategy_id, strategy_run_id, baseline_id, config_hash,
            execution_contract_hash, publish_mode,
            signal_date, execution_date, signal_clock, execution_clock,
            reference_price_basis,
            market, asset_scope, asset_class, account_scope, currency,
            symbol_namespace,
            market_snapshot_id, holdings_snapshot_id, cash_snapshot_id,
            reconcile_id,
            package_status, validation_status, failure_reason_codes,
            stale, drifted, target_positions, risk_flags, raw_payload
        ) VALUES (
            %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s,
            %s, %s, %s, %s,
            %s,
            %s, %s, %s, %s, %s,
            %s,
            %s, %s, %s,
            %s,
            %s, %s, %s,
            %s, %s, %s, %s, %s
        )
        """,
        (
            package.header.schema_version,
            package.header.package_id,
            package.header.idempotency_key,
            package.header.generated_at,
            package.source.strategy_id,
            package.source.strategy_run_id,
            package.source.baseline_id,
            package.source.config_hash,
            package.source.execution_contract_hash,
            package.source.publish_mode,
            package.clock.signal_date,
            package.clock.execution_date,
            package.clock.signal_clock,
            package.clock.execution_clock,
            package.clock.reference_price_basis,
            package.scope.market,
            package.scope.asset_scope,
            package.scope.asset_class,
            package.scope.account_scope,
            package.scope.currency,
            package.scope.symbol_namespace,
            package.inputs.market_snapshot_id,
            package.inputs.holdings_snapshot_id,
            package.inputs.cash_snapshot_id,
            package.inputs.reconcile_id,
            package.package_status,
            package.validation.status,
            _jsonb(package.validation.failure_reason_codes),
            package.validation.stale,
            package.validation.drifted,
            _jsonb(payload["target_positions"]),
            _jsonb(payload["risk_flags"]),
            _jsonb(payload),
        ),
    )

    for action in package.action_sheet:
        action_payload = action.model_dump(mode="json", exclude_none=True)
        cur.execute(
            """
            INSERT INTO pams_live_signal_actions (
                package_id, action_id, idempotency_key,
                symbol, market, asset_class,
                action_type, action_status,
                current_qty, target_qty, delta_qty, suggested_qty,
                target_weight, reference_price, reference_price_basis,
                estimated_amount, reason_code, reason_tags, reason_codes,
                blocked, blocked_reason, expires_at, raw_payload
            ) VALUES (
                %s, %s, %s,
                %s, %s, %s,
                %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s
            )
            """,
            (
                package.header.package_id,
                action.action_id,
                action.idempotency_key,
                action.symbol,
                action.market,
                action.asset_class,
                action.action_type,
                action.action_status,
                action.current_qty,
                action.target_qty,
                action.delta_qty,
                action.suggested_qty,
                action.target_weight,
                action.reference_price,
                action.reference_price_basis,
                action.estimated_amount,
                action.reason_code,
                _jsonb(action.reason_tags),
                _jsonb(action.reason_codes),
                action.blocked,
                action.blocked_reason,
                action.expires_at,
                _jsonb(action_payload),
            ),
        )


@router.post("/packages")
def receive_live_signal_package(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        package = validate_daily_position_adjustment_package(payload)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "failure_reason_codes": ["schema_validation_failed"],
                "errors": exc.errors(),
            },
        ) from exc

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT package_id
                FROM pams_live_signal_packages
                WHERE package_id = %s OR idempotency_key = %s
                LIMIT 1
                """,
                (package.header.package_id, package.header.idempotency_key),
            )
            existing = cur.fetchone()
            if existing:
                existing_package = _fetch_package(cur, existing["package_id"])
                return {"success": True, "status": "exists", "package": existing_package}

            _insert_package(cur, package)
            conn.commit()
            created_package = _fetch_package(cur, package.header.package_id)
            return {"success": True, "status": "created", "package": created_package}
    except HTTPException:
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"live signal package ingest failed: {exc}") from exc
    finally:
        conn.close()


@router.get("/packages")
def list_live_signal_packages(
    strategy_id: str = Query(..., min_length=1),
    account_scope: str = Query(..., min_length=1),
    signal_date: Optional[str] = Query(default=None, min_length=1),
    limit: int = Query(default=20, ge=1, le=200),
) -> dict[str, Any]:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            conditions = ["strategy_id = %s", "account_scope = %s"]
            params: list[Any] = [strategy_id, account_scope]
            if signal_date:
                conditions.append("signal_date = %s")
                params.append(signal_date)
            params.append(limit)
            cur.execute(
                f"""
                SELECT *
                FROM pams_live_signal_packages
                WHERE {' AND '.join(conditions)}
                ORDER BY signal_date DESC, received_at DESC
                LIMIT %s
                """,
                tuple(params),
            )
            rows = cur.fetchall()
    return {"packages": [_package_summary(dict(row)) for row in rows]}


@router.get("/packages/latest")
def get_latest_live_signal_package(
    strategy_id: str = Query(..., min_length=1),
    account_scope: str = Query(..., min_length=1),
) -> dict[str, Any]:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT package_id
                FROM pams_live_signal_packages
                WHERE strategy_id = %s
                  AND account_scope = %s
                ORDER BY signal_date DESC, received_at DESC
                LIMIT 1
                """,
                (strategy_id, account_scope),
            )
            row = cur.fetchone()
            package = _fetch_package(cur, row["package_id"]) if row else None
    if package is None:
        raise HTTPException(status_code=404, detail="latest package not found")
    return {"package": package}


@router.get("/packages/{package_id}")
def get_live_signal_package(package_id: str) -> dict[str, Any]:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            package = _fetch_package(cur, package_id)
    if package is None:
        raise HTTPException(status_code=404, detail=f"package {package_id} not found")
    return {"package": package}


@router.post("/packages/{package_id}/notifications")
def enqueue_live_signal_package_notification(
    package_id: str,
    chat_id: Optional[int] = Query(default=None),
) -> dict[str, Any]:
    target_chat_id = chat_id or os.environ.get("OWNER_CHAT_ID")
    if target_chat_id is None or str(target_chat_id).strip() == "":
        raise HTTPException(status_code=400, detail="chat_id or OWNER_CHAT_ID is required")

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            package = _fetch_package(cur, package_id)
            if package is None:
                raise HTTPException(status_code=404, detail=f"package {package_id} not found")
            title = f"Live signal {package.get('strategy_id')} {package.get('signal_date')}"
            body = _notification_body(package)
            cur.execute(
                """
                INSERT INTO pams_notifications (chat_id, title, body, source)
                VALUES (%s, %s, %s, %s)
                RETURNING id, status, created_at
                """,
                (int(target_chat_id), title, body, "live_signal_package"),
            )
            row = cur.fetchone()
            conn.commit()
            return {"success": True, "notification": {k: _norm(v) for k, v in dict(row).items()}}
    except HTTPException:
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"live signal notification enqueue failed: {exc}") from exc
    finally:
        conn.close()


@router.post("/packages/{package_id}/actions/{action_id}/approve")
def approve_live_signal_action(
    package_id: str,
    action_id: str,
    body: ActionDecisionRequest,
) -> dict[str, Any]:
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            package = _fetch_package(cur, package_id)
            if package is None:
                raise HTTPException(status_code=404, detail=f"package {package_id} not found")
            action = _find_action(package, action_id)
            if action is None:
                raise HTTPException(status_code=404, detail=f"action {action_id} not found")
            blocked = (
                package.get("package_status") == "blocked"
                or action.get("blocked") is True
                or action.get("action_status") == "blocked"
            )
            status: TransactionStatus = "blocked" if blocked else "approved"
            reason = body.reason
            if blocked and not reason:
                reason = action.get("blocked_reason") or ",".join(package.get("failure_reason_codes") or []) or "blocked"
            lifecycle = _upsert_transaction_lifecycle(
                cur,
                package=package,
                action=action,
                status=status,
                operator=body.operator,
                reason=reason,
                final_qty=body.final_qty if body.final_qty is not None else int(action.get("suggested_qty") or 0),
            )
            conn.commit()
            return {"success": True, "transaction": lifecycle}
    except HTTPException:
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"live signal action approval failed: {exc}") from exc
    finally:
        conn.close()


@router.post("/packages/{package_id}/actions/{action_id}/reject")
def reject_live_signal_action(
    package_id: str,
    action_id: str,
    body: ActionDecisionRequest,
) -> dict[str, Any]:
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            package = _fetch_package(cur, package_id)
            if package is None:
                raise HTTPException(status_code=404, detail=f"package {package_id} not found")
            action = _find_action(package, action_id)
            if action is None:
                raise HTTPException(status_code=404, detail=f"action {action_id} not found")
            lifecycle = _upsert_transaction_lifecycle(
                cur,
                package=package,
                action=action,
                status="rejected",
                operator=body.operator,
                reason=body.reason or "rejected_by_operator",
                final_qty=0,
            )
            conn.commit()
            return {"success": True, "transaction": lifecycle}
    except HTTPException:
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"live signal action rejection failed: {exc}") from exc
    finally:
        conn.close()


@router.post("/packages/{package_id}/actions/{action_id}/lifecycle")
def update_live_signal_action_lifecycle(
    package_id: str,
    action_id: str,
    body: ActionLifecycleUpdateRequest,
) -> dict[str, Any]:
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            package = _fetch_package(cur, package_id)
            if package is None:
                raise HTTPException(status_code=404, detail=f"package {package_id} not found")
            action = _find_action(package, action_id)
            if action is None:
                raise HTTPException(status_code=404, detail=f"action {action_id} not found")
            lifecycle = _upsert_transaction_lifecycle(
                cur,
                package=package,
                action=action,
                status=body.status,
                operator=body.operator,
                reason=body.reason,
                final_qty=body.final_qty,
                fill_qty=body.fill_qty,
                fill_price=body.fill_price,
            )
            conn.commit()
            return {"success": True, "transaction": lifecycle}
    except HTTPException:
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"live signal lifecycle update failed: {exc}") from exc
    finally:
        conn.close()


__all__ = [
    "ActionDecisionRequest",
    "ActionLifecycleUpdateRequest",
    "DailyPositionAdjustmentPackage",
    "PACKAGE_SCHEMA_VERSION",
    "approve_live_signal_action",
    "receive_live_signal_package",
    "reject_live_signal_action",
    "update_live_signal_action_lifecycle",
    "validate_daily_position_adjustment_package",
    "router",
]
