"""
订单生命周期管理模块。
处理订单从 confirmed → executed 和过期逻辑。
"""
import logging
from typing import Any, Dict, Optional

import psycopg2.extras

logger = logging.getLogger(__name__)


def execute_order(
    conn,
    order_id: int,
    actual_price: Optional[float] = None,
    actual_size: Optional[int] = None,
) -> Dict[str, Any]:
    """
    标记订单为已执行。

    Args:
        order_id: 订单 ID
        actual_price: 实际成交价 (可选)
        actual_size: 实际成交量 (可选)

    Returns:
        {success, order_id, status} 或 {error}
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT id, status FROM pams_order_drafts WHERE id = %s",
            (order_id,),
        )
        row = cur.fetchone()

        if not row:
            return {"error": f"订单 {order_id} 不存在"}

        if row["status"] != "confirmed":
            return {"error": f"订单状态为 {row['status']}，仅 confirmed 可标记执行"}

        cur.execute("""
            UPDATE pams_order_drafts
            SET status = 'executed',
                executed_at = NOW(),
                actual_price = COALESCE(%s, entry_price),
                actual_size = COALESCE(%s, position_size)
            WHERE id = %s
        """, (actual_price, actual_size, order_id))

        conn.commit()

    logger.info("订单 %d 已标记执行 (price=%s, size=%s)", order_id, actual_price, actual_size)
    return {"success": True, "order_id": order_id, "status": "executed"}


def expire_stale_orders(conn) -> Dict[str, Any]:
    """
    过期所有 valid_until < today 且 status 为 pending 或 confirmed 的订单。

    Returns:
        {expired_count, order_ids}
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            UPDATE pams_order_drafts
            SET status = 'expired'
            WHERE valid_until < CURRENT_DATE
              AND status IN ('pending', 'confirmed')
            RETURNING id
        """)
        rows = cur.fetchall()
        conn.commit()

    expired_ids = [row["id"] for row in rows]
    count = len(expired_ids)

    if count > 0:
        logger.info("已过期 %d 笔订单: %s", count, expired_ids)
    else:
        logger.info("无需过期的订单")

    return {"expired_count": count, "order_ids": expired_ids}
