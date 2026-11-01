"""Account import transport. Drafts never call a financial writer."""
from __future__ import annotations

from typing import Any, Literal
from functools import wraps
from uuid import UUID

import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field

from domain.imports import account_import_access as access
from domain.imports import account_import_store as store
from domain.imports import native_source as source
from domain.imports import native_source_http as source_http
from domain.imports import isolation
from domain.imports.account_import_ocr import ocr_available

def private_response(response: Response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"


router = APIRouter(prefix="/account-import", tags=["account-import"], dependencies=[Depends(private_response)])


def import_scope(fn):
    @wraps(fn)
    def scoped(*args, **kwargs):
        with isolation.account_import_scope():
            return fn(*args, **kwargs)
    return scoped


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Create(StrictRequest):
    account_code: str
    mode: Literal["manual", "upload"]
    kind: Literal["holding", "trade", "cash"]
    completeness: Literal["partial", "complete"] = "partial"


class Save(StrictRequest):
    revision: int = Field(strict=True, gt=0)
    completeness: Literal["partial", "complete"] = "partial"
    rows: list[dict[str, Any]] = Field(max_length=store.MAX_ROWS)


class Mapping(StrictRequest):
    mapping: dict[str, str] = Field(max_length=40)
    kind: Literal["holding", "trade", "cash"] | None = None


class Revision(StrictRequest):
    revision: int = Field(strict=True, gt=0)


class CompleteScope(Revision):
    as_of: str
    member_count: int = Field(strict=True, gt=0, le=store.MAX_ROWS)
    cash_amount: str = Field(min_length=1, max_length=40)
    note: str = Field(min_length=1, max_length=500)
    confirmed: bool = False


class Publish(Revision):
    preview_token: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]+$")


class ConfirmFlowCoverage(Revision):
    mappings: dict[str, int] = Field(max_length=store.MAX_ROWS)
    note: str = Field(min_length=1, max_length=500)
    confirmed: bool = False


class TradeSnapshotPreview(StrictRequest):
    account_code: Literal['tonghuashun']


class TradeSnapshotPublish(StrictRequest):
    preview_token: str = Field(min_length=1, max_length=20000)
    confirmed_scope: bool = False


def _schema_ready(cur):
    try:
        if not isolation.source_enabled(cur):
            raise store.ImportProblem('isolated_only', '统一来源仅允许隔离测试库', 403)
    except isolation.IsolationError as exc:
        raise store.ImportProblem('isolated_only', '统一来源仅允许隔离测试库', 403) from exc
    cur.execute("SELECT to_regclass('public.source_batches') IS NOT NULL AND "
                "to_regclass('public.source_files') IS NOT NULL AND "
                "to_regclass('public.source_items') IS NOT NULL AND "
                "to_regclass('public.source_templates') IS NOT NULL AS ready")
    return bool(cur.fetchone()['ready'])


@import_scope
def execute(request: Request, fn, *, write=False, serializable=False):
    if write:
        access.same_origin(request)
    try:
        with store.get_conn() as conn:
            if serializable:
                conn.set_session(isolation_level="SERIALIZABLE")
            if not write:
                conn.set_session(readonly=True)
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SET LOCAL statement_timeout='10s'")
                cur.execute("SET LOCAL lock_timeout='3s'")
                if not _schema_ready(cur):
                    raise store.ImportProblem("schema_unavailable", "导入暂存结构尚未启用", 503)
                return fn(cur)
    except store.ImportProblem as error:
        raise HTTPException(error.status, detail={"code": error.code, "message": str(error)}) from error
    except (psycopg2.errors.SerializationFailure, psycopg2.errors.DeadlockDetected) as error:
        raise HTTPException(409, detail={"code": "source_changed", "message": "资料并发更新，整批未发布；请重新读取和预检"}) from error
    except psycopg2.Error as error:
        raise HTTPException(503, detail={"code": "import_unavailable", "message": "导入服务暂不可用，未完成的事务已回滚"}) from error
    except (ValueError, UnicodeError) as error:
        raise HTTPException(400, detail={"code": "invalid_document", "message": str(error)[:250]}) from error


@router.get("/capabilities")
@import_scope
def capabilities():
    ready = False
    try:
        with store.get_conn() as conn:
            conn.set_session(readonly=True)
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                ready = _schema_ready(cur)
    except (psycopg2.Error, store.ImportProblem):
        pass
    # Capability keys mean access is available, not a verified user session.
    return {"configured": True, "authenticated": True, "authentication_required": False,
            "schema_ready": ready, "publish_enabled": ready and source.enabled(),
            "ocr_available": ready and ocr_available(),
            "limits": {"file_bytes": store.MAX_FILE_BYTES, "batch_bytes": store.MAX_BATCH_BYTES,
                       "files": store.MAX_FILES, "rows": store.MAX_ROWS}}


@router.get("/context")
def context(request: Request):
    return execute(request, store.context)


@router.post('/trade-snapshot/preview')
def preview_trade_snapshot(body: TradeSnapshotPreview, request: Request):
    from domain.investments import trade_snapshot_http
    access.same_origin(request)
    return execute(request, lambda cur: trade_snapshot_http.preview(cur, body.account_code), serializable=True)


@router.post('/trade-snapshot/publish')
def publish_trade_snapshot(body: TradeSnapshotPublish, request: Request):
    from domain.investments import trade_snapshot_http
    if not source.enabled():
        raise HTTPException(403, detail={'code': 'publish_disabled', 'message': '持仓发布未启用'})
    return execute(request, lambda cur: trade_snapshot_http.publish(
        cur, body.preview_token, confirmed_scope=body.confirmed_scope), write=True, serializable=True)


@router.get("/assets")
def assets(request: Request, q: str = Query("", max_length=80)):
    return execute(request, lambda cur: store.assets(cur, q))


@router.get("/batches")
def list_batches(request: Request, limit: int = Query(20, ge=1, le=50)):
    return execute(request, lambda cur: source_http.list_batches(cur, limit))


@router.post("/flow-coverage/stage")
async def stage_flow_coverage(request: Request, account_code: str = Query(..., max_length=50),
                              filename: str = Query(..., max_length=180)):
    access.same_origin(request)
    content = bytearray()
    async for part in request.stream():
        content.extend(part)
        if len(content) > store.MAX_FILE_BYTES:
            raise HTTPException(413, detail={"code":"file_size", "message":"文件超过10MB"})
    from domain.accounts import flow_coverage
    return await run_in_threadpool(execute, request, lambda cur: flow_coverage.stage(
        cur, account_code=account_code, filename=filename, content=bytes(content)), write=True, serializable=True)


@router.get("/flow-coverage/{batch_id}")
def get_flow_coverage(batch_id: UUID, request: Request):
    from domain.accounts import flow_coverage
    return execute(request, lambda cur: flow_coverage.inspect(cur, batch_id))


@router.post("/flow-coverage/{batch_id}/publish")
def publish_flow_coverage(batch_id: UUID, body: ConfirmFlowCoverage, request: Request):
    from domain.accounts import flow_coverage
    return execute(request, lambda cur: flow_coverage.publish(cur, batch_id=batch_id, **body.model_dump()),
                   write=True, serializable=True)


@router.post("/batches")
def create(body: Create, request: Request):
    return execute(request, lambda cur: source_http.create(cur, **body.model_dump()), write=True)


@router.post('/detect')
async def detect_document(request: Request, account_code: str = Query(..., max_length=50),
                          filename: str = Query(..., max_length=180)):
    access.same_origin(request)
    if any(c in filename for c in ('/', '\\', '\x00', '\r', '\n')):
        raise HTTPException(400, detail={'code': 'invalid_filename', 'message': '文件名无效'})
    content = bytearray()
    async for part in request.stream():
        content.extend(part)
        if len(content) > store.MAX_FILE_BYTES:
            raise HTTPException(413, detail={'code': 'file_size', 'message': '文件超过10MB'})
    if not content:
        return {'kind': None, 'status': 'mismatch'}
    from domain.imports.document_kind import detect
    try:
        return await run_in_threadpool(detect, bytes(content), filename, account_code)
    except (ValueError, UnicodeError):
        return {'kind': None, 'status': 'mismatch'}


@router.get("/batches/{batch_id}")
def get_batch(batch_id: UUID, request: Request):
    return execute(request, lambda cur: source_http.batch(cur, batch_id))


@router.post("/batches/{batch_id}/void")
def void_draft(batch_id: UUID, body: Revision, request: Request):
    return execute(request, lambda cur: source_http.void_draft(cur, batch_id, body.revision),
                   write=True, serializable=True)


@router.put("/batches/{batch_id}")
def save(batch_id: UUID, body: Save, request: Request):
    return execute(request, lambda cur: source_http.save(cur, batch_id, **body.model_dump()), write=True)


@router.post("/batches/{batch_id}/files")
async def upload(batch_id: UUID, request: Request, filename: str = Query(..., max_length=180)):
    access.same_origin(request)
    content = bytearray()
    async for part in request.stream():
        content.extend(part)
        if len(content) > store.MAX_FILE_BYTES:
            raise HTTPException(413, detail={"code": "file_size", "message": "文件超过10MB"})
    return await run_in_threadpool(execute, request, lambda cur: source_http.upload(
        cur, batch_id, filename, bytes(content), request.headers.get('content-type', '')), write=True)


@router.get("/batches/{batch_id}/files/{file_id}")
def evidence(batch_id: UUID, file_id: UUID, request: Request):
    file = execute(request, lambda cur: source_http.get_file(cur, batch_id, file_id))
    return Response(bytes(file["content"]), media_type=file["content_type"],
                    headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                             "Content-Disposition": "inline"})


@router.post("/batches/{batch_id}/files/{file_id}/parse")
def reparse(batch_id: UUID, file_id: UUID, body: Mapping, request: Request):
    return execute(request, lambda cur: source_http.reparse(cur, batch_id, file_id, body.mapping, body.kind), write=True)


@router.post("/batches/{batch_id}/confirm-scope")
def confirm_complete_scope(batch_id: UUID, body: CompleteScope, request: Request):
    return execute(request, lambda cur: source.attest_complete_scope(cur, batch_id=batch_id, **body.model_dump()), write=True)


@router.post("/batches/{batch_id}/preview")
def preview(batch_id: UUID, body: Revision, request: Request):
    return execute(request, lambda cur: source_http.preview(cur, batch_id, body.revision), write=True)


@router.post("/batches/{batch_id}/files/{file_id}/review")
def review_image(batch_id: UUID, file_id: UUID, body: Revision, request: Request):
    return execute(request, lambda cur: source_http.review_image(cur, batch_id, file_id, body.revision), write=True)


@router.post("/batches/{batch_id}/publish")
def publish(batch_id: UUID, body: Publish, request: Request):
    return execute(request, lambda cur: source_http.publish(cur, batch_id, body.revision, body.preview_token),
                   write=True, serializable=True)


@router.post('/batches/{batch_id}/publish-ready')
def publish_ready(batch_id: UUID, body: Publish, request: Request):
    from domain.imports.account_import_partial import publish_ready as publish_ready_domain
    return execute(request, lambda cur: publish_ready_domain(cur, batch_id, body.revision, body.preview_token),
                   write=True, serializable=True)
