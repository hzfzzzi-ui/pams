"""Account import transport. Drafts never call a financial writer."""
from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field

from domain.imports import account_import_access as access
from domain.imports import account_import_store as store
from domain.imports import account_import_publish as publisher
from domain.imports.account_import_ocr import ocr_available

def private_response(response: Response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"


router = APIRouter(prefix="/account-import", tags=["account-import"], dependencies=[Depends(private_response)])


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


class Publish(Revision):
    preview_token: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]+$")


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
                if not store.schema_ready(cur):
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
def capabilities():
    ready = False
    try:
        with store.get_conn() as conn:
            conn.set_session(readonly=True)
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                ready = store.schema_ready(cur)
    except psycopg2.Error:
        pass
    # Legacy capability keys mean access is available, not a verified user session.
    return {"configured": True, "authenticated": True, "authentication_required": False,
            "schema_ready": ready, "publish_enabled": ready and publisher.enabled(),
            "ocr_available": ready and ocr_available(),
            "limits": {"file_bytes": store.MAX_FILE_BYTES, "batch_bytes": store.MAX_BATCH_BYTES,
                       "files": store.MAX_FILES, "rows": store.MAX_ROWS}}


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
    return execute(request, lambda cur: publisher.preview(cur, str(batch_id), body.revision), write=True)


@router.post("/batches/{batch_id}/files/{file_id}/review")
def review_image(batch_id: UUID, file_id: UUID, body: Revision, request: Request):
    return execute(request, lambda cur: store.review_image(cur, str(batch_id), str(file_id), body.revision), write=True)


@router.post("/batches/{batch_id}/publish")
def publish(batch_id: UUID, body: Publish, request: Request):
    return execute(request, lambda cur: publisher.publish(cur, str(batch_id), body.revision, body.preview_token),
                   write=True, serializable=True)
