"""
任务队列API - Telegram ↔ Claude Code Bridge
"""
import os
from typing import Optional, List

import psycopg2
import psycopg2.extras
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter()


def get_conn():
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "pams-postgres"),
        port=int(os.environ.get("PGPORT", "5432")),
        dbname=os.environ.get("PGDATABASE", "pams"),
        user=os.environ.get("PGUSER", "pams"),
        password=os.environ.get("PGPASSWORD", ""),
    )


def json(data, status_code=200):
    return JSONResponse(content=data, status_code=status_code,
                        media_type="application/json; charset=utf-8")


def _norm(v):
    if hasattr(v, "isoformat"):
        return v.isoformat()
    from decimal import Decimal
    if isinstance(v, Decimal):
        return str(v)
    return v


# ==================== Models ====================

class TaskCreate(BaseModel):
    chat_id: int
    message_id: Optional[int] = None
    request: str


class PageLink(BaseModel):
    title: str
    url: str


class TaskComplete(BaseModel):
    response: str
    page_links: Optional[List[PageLink]] = None


class TaskFail(BaseModel):
    response: str


# ==================== Endpoints ====================

@router.post("/tasks")
def create_task(body: TaskCreate):
    """创建新任务（Bot调用）"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO pams_task_queue (chat_id, message_id, request)
                VALUES (%s, %s, %s)
                RETURNING id
            """, (body.chat_id, body.message_id, body.request))
            task_id = cur.fetchone()[0]
            conn.commit()

    return json({"success": True, "task_id": task_id})


@router.get("/tasks/pending")
def get_pending_tasks(limit: int = Query(10, ge=1, le=50)):
    """获取待处理任务（Claude Code调用）"""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, chat_id, request, created_at
                FROM pams_task_queue
                WHERE status = 'pending'
                ORDER BY created_at ASC
                LIMIT %s
            """, (limit,))
            rows = cur.fetchall()

    return json({
        "tasks": [{k: _norm(v) for k, v in dict(r).items()} for r in rows]
    })


@router.patch("/tasks/{task_id}/claim")
def claim_task(task_id: int):
    """认领任务（Claude Code调用）"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE pams_task_queue
                SET status = 'processing'
                WHERE id = %s AND status = 'pending'
            """, (task_id,))
            conn.commit()

            if cur.rowcount == 0:
                raise HTTPException(
                    status_code=404,
                    detail="任务不存在或已被认领"
                )

    return json({"success": True})


@router.patch("/tasks/{task_id}/complete")
def complete_task(task_id: int, body: TaskComplete):
    """提交任务响应（Claude Code调用）"""
    import json as json_mod

    links = [{"title": l.title, "url": l.url} for l in body.page_links] if body.page_links else []

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE pams_task_queue
                SET status = 'completed',
                    response = %s,
                    page_links = %s::jsonb,
                    completed_at = NOW()
                WHERE id = %s AND status IN ('pending', 'processing')
            """, (body.response, json_mod.dumps(links), task_id))
            conn.commit()

            if cur.rowcount == 0:
                raise HTTPException(
                    status_code=404,
                    detail="任务不存在或状态不允许完成"
                )

    return json({"success": True})


@router.patch("/tasks/{task_id}/fail")
def fail_task(task_id: int, body: TaskFail):
    """标记任务失败（Claude Code调用）"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE pams_task_queue
                SET status = 'failed',
                    response = %s,
                    completed_at = NOW()
                WHERE id = %s AND status IN ('pending', 'processing')
            """, (body.response, task_id))
            conn.commit()

            if cur.rowcount == 0:
                raise HTTPException(
                    status_code=404,
                    detail="任务不存在或状态不允许操作"
                )

    return json({"success": True})


@router.get("/tasks/completed")
def get_completed_tasks(since_id: int = Query(0, ge=0)):
    """获取已完成任务（Bot轮询调用）"""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, chat_id, message_id, response, page_links, completed_at
                FROM pams_task_queue
                WHERE status = 'completed' AND id > %s
                ORDER BY id ASC
            """, (since_id,))
            rows = cur.fetchall()

    return json({
        "tasks": [{k: _norm(v) for k, v in dict(r).items()} for r in rows]
    })


@router.delete("/tasks/{task_id}/ack")
def ack_task(task_id: int):
    """确认投递并删除任务记录（Bot调用）"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM pams_task_queue WHERE id = %s", (task_id,)
            )
            conn.commit()

    return json({"success": True})
