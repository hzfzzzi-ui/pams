"""Unified import inbox API: auto-routing uploads, local dropzone scan, and cash-yield reconciliation."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import psycopg2
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from psycopg2.extras import RealDictCursor
from pydantic import BaseModel, ConfigDict, Field

from domain.imports import isolation
from domain.imports.account_import import account_import_access as access
from domain.imports.account_import import account_import_store as store
from domain.imports.account_import.inbox_router import (
    compute_cash_yield_reconciliation,
    route_file_to_accounts,
    scan_inbox_directory,
)


def _private_response(response: Response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"


router = APIRouter(
    prefix="/import-inbox",
    tags=["import-inbox"],
    dependencies=[Depends(_private_response)],
)

DEFAULT_INBOX_DIR = Path(os.environ.get("PAMS_IMPORT_INBOX_DIR", "/host/account-ledger/_Inbox"))


class CashYieldRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    previous_balance: str = Field(min_length=1, max_length=40)
    current_balance: str = Field(min_length=1, max_length=40)
    net_transfers: str = Field(default="0", max_length=40)


def _inbox_dir() -> Path:
    return Path(os.environ.get("PAMS_IMPORT_INBOX_DIR", str(DEFAULT_INBOX_DIR)))


def _known_source_hashes(cur) -> set[str]:
    cur.execute("SELECT to_regclass('public.source_records') IS NOT NULL AS ok")
    row = cur.fetchone()
    if not (row and row["ok"]):
        return set()
    cur.execute(
        """
        SELECT DISTINCT LOWER(content_sha256) AS digest
        FROM public.source_records
        WHERE content_sha256 IS NOT NULL AND content_sha256 <> ''
        """
    )
    return {r["digest"] for r in cur.fetchall() if r.get("digest")}


def _pending_drafts(cur, limit: int = 50) -> list[dict[str, Any]]:
    cur.execute("SELECT to_regclass('public.source_records') IS NOT NULL AS ok")
    row = cur.fetchone()
    if not (row and row["ok"]):
        return []
    cur.execute(
        """
        SELECT r.source_id, r.account_code, COALESCE(a.display_name, r.account_code) AS account_label,
               r.source_name, r.document_type, r.filename, r.status, r.revision, r.updated_at
        FROM public.source_records r
        LEFT JOIN public.pams_accounts a ON a.account_code = r.account_code
        WHERE r.status = 'draft'
        ORDER BY r.updated_at DESC, r.source_id DESC
        LIMIT %s
        """,
        (limit,),
    )
    return [store.norm(dict(r)) for r in cur.fetchall()]


@router.get("/summary")
def inbox_summary():
    """Return pending drafts across all subaccounts plus local dropzone scan status."""
    inbox_path = _inbox_dir()
    drafts: list[dict[str, Any]] = []
    known_hashes: set[str] = set()
    try:
        with isolation.account_import_scope(), store.get_conn() as conn:
            conn.set_session(readonly=True)
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                drafts = _pending_drafts(cur)
                known_hashes = _known_source_hashes(cur)
    except (psycopg2.Error, store.ImportProblem):
        drafts = []

    local_scan = scan_inbox_directory(inbox_path, known_hashes=known_hashes)
    return {
        "pending_drafts": drafts,
        "pending_count": len(drafts),
        "local_inbox": {
            "exists": local_scan["exists"],
            "directory": local_scan["directory"],
            "new_count": len(local_scan["new_items"]),
            "duplicate_count": local_scan["duplicate_count"],
            "unrecognized_count": len(local_scan["unrecognized"]),
            "new_items": local_scan["new_items"][:30],
        },
    }


def _stage_bytes_for_decision(cur, *, content: bytes, filename: str, decision) -> list[dict[str, Any]]:
    from domain.imports.sources.document_stage_v2 import stage_document

    staged: list[dict[str, Any]] = []
    for account_code in decision.accounts:
        result = stage_document(
            cur,
            account_code=account_code,
            source_name=decision.source_name,
            filename=filename,
            content=content,
            document_type=decision.document_type,
        )
        staged.append({
            "account_code": account_code,
            "document_type": decision.document_type,
            "filename": filename,
            "rule_id": decision.rule_id,
            "source_id": store.norm(result.get("source_id")),
            "status": result.get("status", "draft"),
        })
    return staged


@router.post("/upload")
async def inbox_upload(
    request: Request,
    filename: str = Query(..., min_length=1, max_length=180),
    rel_path: str = Query("", max_length=260),
):
    """Upload one file without pre-selecting an account; auto-route and stage draft(s)."""
    access.same_origin(request)
    decision = route_file_to_accounts(filename, rel_path)
    if decision is None:
        raise HTTPException(
            status_code=422,
            detail={"code": "unrecognized_file", "message": f"暂无法自动识别文件归属账户：{filename}"},
        )
    content = bytearray()
    async for part in request.stream():
        content.extend(part)
        if len(content) > store.MAX_FILE_BYTES:
            raise HTTPException(status_code=413, detail={"code": "file_size", "message": "文件超过 10MB"})

    def _run():
        with isolation.account_import_scope(), store.get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                staged = _stage_bytes_for_decision(cur, content=bytes(content), filename=filename, decision=decision)
            conn.commit()
            return {
                "filename": filename,
                "rule_id": decision.rule_id,
                "multi_account": decision.multi_account,
                "staged": staged,
            }

    try:
        return await run_in_threadpool(_run)
    except store.ImportProblem as exc:
        raise HTTPException(status_code=exc.status, detail={"code": exc.code, "message": str(exc)}) from exc


@router.post("/scan-local")
def inbox_scan_local(request: Request):
    """Scan local dropzone directory and stage all newly discovered files into reviewable drafts."""
    access.same_origin(request)
    inbox_path = _inbox_dir()
    staged_all: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    with isolation.account_import_scope(), store.get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            known = _known_source_hashes(cur)
            scan = scan_inbox_directory(inbox_path, known_hashes=known)
            for item in scan["new_items"]:
                file_path = inbox_path / item["rel_path"]
                if not file_path.is_file() or file_path.stat().st_size > store.MAX_FILE_BYTES:
                    errors.append({"file": item["rel_path"], "error": "文件不存在或超过 10MB"})
                    continue
                decision = route_file_to_accounts(item["filename"], item["rel_path"])
                if decision is None:
                    continue
                try:
                    raw_bytes = file_path.read_bytes()
                    staged_all.extend(
                        _stage_bytes_for_decision(cur, content=raw_bytes, filename=item["filename"], decision=decision)
                    )
                except Exception as exc:
                    errors.append({"file": item["rel_path"], "error": str(exc)})
        conn.commit()

    return {
        "directory": str(inbox_path),
        "staged_count": len(staged_all),
        "staged": staged_all,
        "skipped_duplicates": scan["duplicate_count"],
        "unrecognized": scan["unrecognized"],
        "errors": errors,
    }


@router.post("/cash-yield")
def inbox_cash_yield(body: CashYieldRequest):
    """Compute implied monthly money-market yield from period-end balance snapshots."""
    try:
        return compute_cash_yield_reconciliation(
            previous_balance=body.previous_balance,
            current_balance=body.current_balance,
            net_transfers=body.net_transfers,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "invalid_amount", "message": str(exc)}) from exc
