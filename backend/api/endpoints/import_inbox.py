"""Unified import inbox API: auto-routing uploads, local dropzone scan, and cash-yield reconciliation."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg2
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from psycopg2.extras import RealDictCursor
from pydantic import BaseModel, ConfigDict, Field

from domain.imports import isolation
from domain.imports.account_import import account_import_access as access
from domain.imports.account_import import account_import_store as store
from domain.imports.account_import.email_inbox import fetch_gmail_bill_artifacts, imap_status
from domain.imports.account_import.inbox_router import (
    compute_cash_yield_reconciliation,
    route_file_to_accounts,
    scan_inbox_directory,
)
from domain.imports.publication.bank_source_confirmation import SOURCE_ONLY_ACCOUNTS


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
        SELECT DISTINCT LOWER(sha256) AS digest
        FROM public.source_records
        WHERE sha256 IS NOT NULL AND sha256 <> ''
        """
    )
    return {r["digest"] for r in cur.fetchall() if r.get("digest")}


YM_RE = re.compile(r"(20\d{2})[-年/]?(\d{2})")


def _filename_period_key(filename: str | None) -> str | None:
    if not filename:
        return None
    m = YM_RE.search(str(filename))
    if not m:
        return None
    return f"{m.group(1)}-{m.group(2)}"


def _published_account_periods(cur) -> set[tuple[str, str]]:
    cur.execute(
        """
        SELECT account_code, filename
        FROM public.source_records
        WHERE status = 'published' AND filename IS NOT NULL
        """
    )
    pairs: set[tuple[str, str]] = set()
    for r in cur.fetchall():
        ym = _filename_period_key(r.get("filename"))
        if ym and r.get("account_code"):
            pairs.add((str(r["account_code"]), ym))
    return pairs


def _pending_drafts(cur, limit: int = 50) -> list[dict[str, Any]]:
    cur.execute("SELECT to_regclass('public.source_records') IS NOT NULL AS ok")
    row = cur.fetchone()
    if not (row and row["ok"]):
        return []
    published_periods = _published_account_periods(cur)
    cur.execute(
        """
        SELECT r.source_id, r.account_code, COALESCE(a.display_name, r.account_code) AS account_label,
               r.source_name, r.acquisition_method, r.document_type, r.filename,
               r.status, r.revision, r.updated_at,
               (SELECT COUNT(*) FROM public.source_items si
                WHERE si.source_id = r.source_id AND si.review_status <> 'excluded') AS item_count
        FROM public.source_records r
        LEFT JOIN public.pams_accounts a ON a.account_code = r.account_code
        WHERE r.status = 'draft'
        ORDER BY r.updated_at DESC, r.source_id DESC
        LIMIT %s
        """,
        (limit,),
    )
    out: list[dict[str, Any]] = []
    for raw in cur.fetchall():
        rec = store.norm(dict(raw))
        code = str(rec.get("account_code") or "")
        ym = _filename_period_key(rec.get("filename"))
        dup_period = bool(ym and (code, ym) in published_periods)
        item_count = int(rec.get("item_count") or 0)
        is_bank_source = code in SOURCE_ONLY_ACCOUNTS
        rec["item_count"] = item_count
        rec["is_bank_source"] = is_bank_source
        rec["duplicate_period_hint"] = dup_period
        rec["auto_publishable"] = bool(
            item_count > 0
            and rec.get("acquisition_method") == "file"
            and (is_bank_source or rec.get("document_type") in {"cash", "trade"})
            and not dup_period
        )
        out.append(rec)
    return out


def _confirm_single_draft(cur, source_id: UUID) -> dict[str, Any]:
    cur.execute(
        """
        SELECT source_id, account_code, document_type, acquisition_method, filename, status, revision
        FROM public.source_records
        WHERE source_id = %s
        FOR UPDATE
        """,
        (str(source_id),),
    )
    rec = cur.fetchone()
    if not rec:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "未找到该草稿"})
    if rec["status"] != "draft":
        raise HTTPException(status_code=409, detail={"code": "not_draft", "message": f"该记录状态为 {rec['status']}，无需重复入账"})

    account_code = str(rec["account_code"])
    doc_type = str(rec["document_type"])
    revision = int(rec["revision"])

    if account_code in SOURCE_ONLY_ACCOUNTS and doc_type == "cash" and rec["acquisition_method"] != "manual":
        from domain.imports.publication import bank_source_confirmation

        res = bank_source_confirmation.confirm(cur, source_id, revision)
        return {
            "source_id": str(source_id),
            "account_code": account_code,
            "filename": rec.get("filename"),
            "mode": "bank_source_confirm",
            "revision": res["revision"],
        }

    if doc_type == "cash":
        from domain.imports.publication import cash_publication_v2

        prev = cash_publication_v2.preview(cur, source_id=source_id, revision=revision, mode="all")
        if not prev.get("publishable") or not prev.get("preview_token"):
            raise HTTPException(
                status_code=409,
                detail={"code": "needs_manual_review", "message": "存在待核对异常项，请点「去核对」逐条确认"},
            )
        pub = cash_publication_v2.publish(
            cur, source_id=source_id, revision=revision, mode="all", preview_token=prev["preview_token"]
        )
        return {
            "source_id": str(source_id),
            "account_code": account_code,
            "filename": rec.get("filename"),
            "mode": "cash_publish",
            "result": store.norm(pub),
        }

    if doc_type == "trade":
        from domain.imports.publication import trade_publication_v2

        prev = trade_publication_v2.preview(cur, source_id=source_id, revision=revision, mode="all")
        if not prev.get("publishable") or not prev.get("preview_token"):
            raise HTTPException(
                status_code=409,
                detail={"code": "needs_manual_review", "message": "交易草稿存在待核对项，请点「去核对」确认"},
            )
        pub = trade_publication_v2.publish(
            cur, source_id=source_id, revision=revision, mode="all", preview_token=prev["preview_token"]
        )
        return {
            "source_id": str(source_id),
            "account_code": account_code,
            "filename": rec.get("filename"),
            "mode": "trade_publish",
            "result": store.norm(pub),
        }

    raise HTTPException(
        status_code=409,
        detail={"code": "manual_review_required", "message": "持仓快照需在导入页核对完整范围后发布"},
    )


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
        "email_inbox": imap_status(),
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

    is_screenshot = Path(filename).suffix.lower() in {".png", ".jpg", ".jpeg"}
    staged: list[dict[str, Any]] = []
    for account_code in decision.accounts:
        result = stage_document(
            cur,
            account_code=account_code,
            source_name=decision.source_name,
            filename=filename,
            content=content,
            document_type=(decision.document_type if is_screenshot else None),
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
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "stage_rejected", "message": str(exc)}) from exc


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


@router.post("/pull-email")
def inbox_pull_email(request: Request):
    """Pull recent bank statement emails via configured Gmail IMAP and stage new drafts."""
    access.same_origin(request)
    status_info = imap_status()
    if not status_info["configured"]:
        return {
            "configured": False,
            "skipped": True,
            "message": "尚未配置 Gmail IMAP（请在宿主机 .env 配置 PAMS_IMAP_USER 与 PAMS_IMAP_PASSWORD）",
            "staged_count": 0,
            "skipped_duplicates": 0,
            "staged": [],
            "errors": [],
        }

    staged_all: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    with isolation.account_import_scope(), store.get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            known = _known_source_hashes(cur)
            try:
                pulled = fetch_gmail_bill_artifacts(known_hashes=known)
            except Exception as exc:
                raise HTTPException(
                    status_code=502,
                    detail={"code": "imap_pull_failed", "message": f"Gmail 拉取失败：{exc}"},
                ) from exc

            for artifact in pulled["artifacts"]:
                filename = artifact["filename"]
                decision = route_file_to_accounts(filename)
                if decision is None:
                    continue
                try:
                    staged_all.extend(
                        _stage_bytes_for_decision(
                            cur,
                            content=artifact["content"],
                            filename=filename,
                            decision=decision,
                        )
                    )
                except Exception as exc:
                    errors.append({"file": filename, "error": str(exc)})
        conn.commit()

    return {
        "configured": True,
        "skipped": False,
        "folder": pulled.get("folder"),
        "fetched_messages": pulled.get("fetched_messages", 0),
        "staged_count": len(staged_all),
        "skipped_duplicates": pulled.get("skipped_duplicates", 0),
        "staged": staged_all,
        "errors": errors,
    }


@router.post("/drafts/{source_id}/confirm")
def inbox_confirm_draft(source_id: UUID, request: Request):
    """One-click confirm/publish a single clean draft directly from the unified inbox."""
    access.same_origin(request)
    try:
        with isolation.account_import_scope(), store.get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                result = _confirm_single_draft(cur, source_id)
            conn.commit()
            return result
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": "confirm_failed", "message": str(exc)}) from exc


@router.post("/drafts/{source_id}/discard")
def inbox_discard_draft(source_id: UUID, request: Request):
    """Mark a redundant/obsolete draft as voided while preserving its sha256 for dedup."""
    access.same_origin(request)
    with isolation.account_import_scope(), store.get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                UPDATE public.source_records
                SET status = 'voided', revision = revision + 1, updated_at = now()
                WHERE source_id = %s AND status = 'draft'
                RETURNING source_id, account_code, filename, status, revision
                """,
                (str(source_id),),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail={"code": "not_found", "message": "未找到可忽略的草稿"})
        conn.commit()
    return store.norm(dict(row))


@router.post("/discard-duplicates")
def inbox_discard_duplicates(
    request: Request,
    scope: str = Query("duplicates", pattern="^(duplicates|stale)$"),
):
    """Bulk void duplicate-period drafts (or all duplicate + legacy non-file drafts) while keeping sha256."""
    access.same_origin(request)
    discarded: list[dict[str, Any]] = []
    with isolation.account_import_scope(), store.get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            drafts = _pending_drafts(cur, limit=200)
            target_ids = [
                str(d["source_id"])
                for d in drafts
                if d.get("duplicate_period_hint")
                or (scope == "stale" and d.get("acquisition_method") != "file")
            ]
            if target_ids:
                cur.execute(
                    """
                    UPDATE public.source_records
                    SET status = 'voided', revision = revision + 1, updated_at = now()
                    WHERE source_id = ANY(%s::uuid[]) AND status = 'draft'
                    RETURNING source_id, account_code, filename, status
                    """,
                    (target_ids,),
                )
                discarded = [store.norm(dict(r)) for r in cur.fetchall()]
        conn.commit()
    return {"scope": scope, "discarded_count": len(discarded), "discarded": discarded}


@router.post("/confirm-ready")
def inbox_confirm_ready(request: Request):
    """Batch confirm all zero-issue, non-duplicate-period file drafts in the inbox."""
    access.same_origin(request)
    confirmed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    with isolation.account_import_scope(), store.get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            drafts = _pending_drafts(cur, limit=50)
            for draft in drafts:
                if not draft.get("auto_publishable"):
                    continue
                sid = UUID(str(draft["source_id"]))
                cur.execute("SAVEPOINT inbox_confirm_one")
                try:
                    confirmed.append(_confirm_single_draft(cur, sid))
                    cur.execute("RELEASE SAVEPOINT inbox_confirm_one")
                except (HTTPException, ValueError) as exc:
                    cur.execute("ROLLBACK TO SAVEPOINT inbox_confirm_one")
                    detail = exc.detail.get("message") if isinstance(exc, HTTPException) and isinstance(exc.detail, dict) else str(exc)
                    skipped.append({"source_id": str(sid), "filename": draft.get("filename"), "reason": detail})
        conn.commit()
    return {"confirmed_count": len(confirmed), "confirmed": confirmed, "skipped": skipped}


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
