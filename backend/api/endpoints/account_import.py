"""Account import transport. Drafts never call a financial writer."""
from __future__ import annotations

import hmac
import os
from typing import Any, Literal
from uuid import UUID

import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field

from services import account_import_access as access
from services import account_import_store as store

def private_response(response: Response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"


router = APIRouter(prefix="/account-import", tags=["account-import"], dependencies=[Depends(private_response)])


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Unlock(StrictRequest):
    token: str = Field(max_length=300)


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


def execute(request: Request, fn, *, write=False):
    access.require_access(request)
    if write:
        access.same_origin(request)
    try:
        with store.get_conn() as conn:
            if not write:
                conn.set_session(readonly=True)
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SET LOCAL statement_timeout='10s'")
                cur.execute("SET LOCAL lock_timeout='3s'")
                if not store.schema_ready(cur):
                    raise store.ImportProblem("schema_unavailable", "导入暂存结构尚未启用", 503)
                return fn(cur)
    except store.ImportProblem as error:
        raise HTTPException(error.status, detail={"code": error.code, "message": str(error)}) from error
    except (ValueError, UnicodeError) as error:
        raise HTTPException(400, detail={"code": "invalid_document", "message": str(error)[:250]}) from error


@router.get("/capabilities")
def capabilities(request: Request):
    ready = False
    if access.authenticated(request):
        try:
            with store.get_conn() as conn:
                conn.set_session(readonly=True)
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    ready = store.schema_ready(cur)
        except psycopg2.Error:
            pass
    return {"configured": access.configured(), "authenticated": access.authenticated(request),
            "schema_ready": ready, "publish_enabled": False, "ocr_available": False,
            "limits": {"file_bytes": store.MAX_FILE_BYTES, "batch_bytes": store.MAX_BATCH_BYTES,
                       "files": store.MAX_FILES, "rows": store.MAX_ROWS}}


@router.post("/unlock")
def unlock(body: Unlock, request: Request, response: Response):
    access.same_origin(request)
    expected = os.environ.get("PAMS_IMPORT_ACCESS_TOKEN", "")
    if not access.configured() or not hmac.compare_digest(body.token.encode(), expected.encode()):
        raise HTTPException(401, detail={"code": "unlock_failed", "message": "导入口令无效或尚未配置"})
    response.set_cookie(access.COOKIE, access.signed_session(), httponly=True, samesite="strict",
                        secure=request.url.scheme == "https", max_age=access.TTL, path="/api/account-import")
    response.headers["Cache-Control"] = "no-store"
    return {"authenticated": True}


@router.get("/context")
def context(request: Request):
    return execute(request, store.context)


@router.get("/assets")
def assets(request: Request, q: str = Query("", max_length=80)):
    return execute(request, lambda cur: store.assets(cur, q))


@router.get("/batches")
def list_batches(request: Request, limit: int = Query(20, ge=1, le=50)):
    return execute(request, lambda cur: store.list_batches(cur, limit))


@router.post("/batches")
def create(body: Create, request: Request):
    return execute(request, lambda cur: store.create(cur, **body.model_dump()), write=True)


@router.get("/batches/{batch_id}")
def get_batch(batch_id: UUID, request: Request):
    return execute(request, lambda cur: store.batch(cur, str(batch_id)))


@router.put("/batches/{batch_id}")
def save(batch_id: UUID, body: Save, request: Request):
    return execute(request, lambda cur: store.save(cur, str(batch_id), **body.model_dump()), write=True)


@router.post("/batches/{batch_id}/files")
async def upload(batch_id: UUID, request: Request, filename: str = Query(..., max_length=180)):
    access.require_access(request)
    access.same_origin(request)
    content = bytearray()
    async for part in request.stream():
        content.extend(part)
        if len(content) > store.MAX_FILE_BYTES:
            raise HTTPException(413, detail={"code": "file_size", "message": "文件超过10MB"})
    return await run_in_threadpool(execute, request, lambda cur: store.upload(
        cur, str(batch_id), filename, bytes(content), request.headers.get("content-type", "")), write=True)


@router.get("/batches/{batch_id}/files/{file_id}")
def evidence(batch_id: UUID, file_id: UUID, request: Request):
    file = execute(request, lambda cur: store.get_file(cur, str(batch_id), str(file_id)))
    return Response(bytes(file["content"]), media_type=file["content_type"],
                    headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                             "Content-Disposition": "inline"})


@router.post("/batches/{batch_id}/files/{file_id}/parse")
def reparse(batch_id: UUID, file_id: UUID, body: Mapping, request: Request):
    return execute(request, lambda cur: store.reparse(cur, str(batch_id), str(file_id), body.mapping, body.kind), write=True)


@router.post("/batches/{batch_id}/preview")
def preview(batch_id: UUID, body: Revision, request: Request):
    def build(cur):
        value = store.batch(cur, str(batch_id))
        store.editable(value, body.revision)
        return {"batch_id": str(batch_id), "revision": body.revision, "can_publish": False,
                "rows": value["rows"], "counts": {"new": len(value["rows"]), "blocked": 0, "duplicates": 0, "pending": 0},
                "warnings": ["此阶段仅保存资料，尚未开放资产发布"],
                "impact": {"nav_change": None, "nav_estimate_status": "unavailable", "message": "预览未写入资产账目"}}
    return execute(request, build)
