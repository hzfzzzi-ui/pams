"""
通知推送 API - 供外部 Agent 和定时任务推送消息到 Telegram
"""
import os
import logging
from typing import Optional
from decimal import Decimal

import psycopg2
import psycopg2.extras
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

router = APIRouter()
logger = logging.getLogger(__name__)


def get_conn():
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "pams-postgres"),
        port=int(os.environ.get("PGPORT", "5432")),
        dbname=os.environ.get("PGDATABASE", "pams"),
        user=os.environ.get("PGUSER", "pams"),
        password=os.environ.get("PGPASSWORD", ""),
    )


def _norm(v):
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return v


class NotificationCreate(BaseModel):
    chat_id: int
    title: Optional[str] = None
    body: str
    source: str = "system"


@router.post("/notifications")
def create_notification(notif: NotificationCreate):
    """创建推送通知（任何 Agent 均可调用）"""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                INSERT INTO pams_notifications (chat_id, title, body, source)
                VALUES (%s, %s, %s, %s)
                RETURNING id, status, created_at
            """, (notif.chat_id, notif.title, notif.body, notif.source))
            row = cur.fetchone()
        conn.commit()
        result = {k: _norm(v) for k, v in row.items()}
        return {"success": True, "notification": result}
    except Exception as e:
        conn.rollback()
        logger.error("Create notification failed: %s", e)
        raise HTTPException(500, f"创建通知失败: {e}")
    finally:
        conn.close()


@router.get("/notifications/pending")
def get_pending_notifications(limit: int = Query(default=20, ge=1, le=100)):
    """获取待发送通知（Bot 轮询调用）"""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, chat_id, title, body, source, created_at
                FROM pams_notifications
                WHERE status = 'pending'
                ORDER BY created_at ASC
                LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
        return {"notifications": [{k: _norm(v) for k, v in r.items()} for r in rows]}
    finally:
        conn.close()


@router.patch("/notifications/{notif_id}/sent")
def mark_notification_sent(notif_id: int):
    """标记通知已发送"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE pams_notifications
                SET status = 'sent', sent_at = NOW()
                WHERE id = %s AND status = 'pending'
            """, (notif_id,))
            if cur.rowcount == 0:
                raise HTTPException(404, f"通知 {notif_id} 不存在或已处理")
        conn.commit()
        return {"success": True, "message": f"通知 {notif_id} 已标记发送"}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        logger.error("Mark notification sent failed: %s", e)
        raise HTTPException(500, f"标记失败: {e}")
    finally:
        conn.close()
