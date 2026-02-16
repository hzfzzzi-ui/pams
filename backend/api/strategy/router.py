"""
策略系统 FastAPI 路由端点。
提供候选查询、订单管理、黑名单、风控快照、手动扫描等 API。
"""
import json
import logging
import os
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

import psycopg2
import psycopg2.extras
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from .config import load_strategy_config
from .macro_trend import update_trend_state
from .cooldown import check_cooldown, expire_cooldowns, record_stop_loss
from .event_monitor import refresh_events_batch
from .fundamental import refresh_fundamental
from .intraday import run_intraday_refresh
from .models import BlacklistAdd, OrderAction, OrderExecute, StopLossRecord
from .order_lifecycle import execute_order, expire_stale_orders
from .risk_snapshot import refresh_risk_snapshot
from .scanner import run_eod_scan
from .sentiment import refresh_sentiment
from .watchlist_manager import (
    run_watchlist_scan, run_exit_check, get_exit_history, get_scan_log,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["strategy"])


def _get_conn():
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "pams-postgres"),
        port=int(os.environ.get("PGPORT", "5432")),
        dbname=os.environ.get("PGDATABASE", "pams"),
        user=os.environ.get("PGUSER", "pams"),
        password=os.environ.get("PGPASSWORD", ""),
    )


def _json(data: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        content=data,
        status_code=status_code,
        media_type="application/json; charset=utf-8",
    )


def _norm(v: Any):
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if isinstance(v, Decimal):
        return str(v)
    return v


def _rows_to_list(rows):
    return [{k: _norm(v) for k, v in dict(r).items()} for r in rows]


# ==================== 候选标的 ====================

@router.get("/strategy/candidates")
def list_candidates(
    scan_date: Optional[str] = None,
    stage: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
):
    """获取策略候选列表。"""
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            where = []
            params = []

            if scan_date:
                where.append("scan_date = %s")
                params.append(scan_date)
            else:
                where.append("scan_date = (SELECT MAX(scan_date) FROM pams_strategy_candidates)")

            if stage:
                where.append("stage = %s")
                params.append(stage)

            where_sql = " AND ".join(where)
            params.append(limit)

            cur.execute(f"""
                SELECT * FROM pams_strategy_candidates
                WHERE {where_sql}
                ORDER BY
                    CASE stage
                        WHEN 'TRIGGERED' THEN 1
                        WHEN 'PULLBACK_ZONE' THEN 2
                        WHEN 'TREND_PASS' THEN 3
                        ELSE 4
                    END,
                    confirm_score DESC NULLS LAST
                LIMIT %s
            """, params)
            rows = cur.fetchall()

    return _json({"rows": _rows_to_list(rows), "count": len(rows)})


@router.get("/strategy/candidates/{candidate_id}")
def get_candidate(candidate_id: int):
    """获取候选详情（含完整 reason_codes 链）。"""
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM pams_strategy_candidates WHERE id = %s",
                (candidate_id,),
            )
            row = cur.fetchone()

    if not row:
        raise HTTPException(404, f"候选 {candidate_id} 不存在")

    return _json({k: _norm(v) for k, v in dict(row).items()})


# ==================== 订单草稿 ====================

@router.get("/strategy/orders")
def list_orders(
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
):
    """获取订单草稿列表。"""
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            where = []
            params = []

            if status:
                where.append("status = %s")
                params.append(status)

            where_sql = " AND ".join(where) if where else "1=1"
            params.append(limit)

            cur.execute(f"""
                SELECT d.*, c.name, c.stage
                FROM pams_order_drafts d
                LEFT JOIN pams_strategy_candidates c ON c.id = d.candidate_id
                WHERE {where_sql}
                ORDER BY d.created_at DESC
                LIMIT %s
            """, params)
            rows = cur.fetchall()

    return _json({"rows": _rows_to_list(rows), "count": len(rows)})


@router.get("/strategy/orders/{order_id}")
def get_order(order_id: int):
    """获取订单草稿详情。"""
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT d.*, c.name, c.stage, c.scan_date
                FROM pams_order_drafts d
                LEFT JOIN pams_strategy_candidates c ON c.id = d.candidate_id
                WHERE d.id = %s
            """, (order_id,))
            row = cur.fetchone()

    if not row:
        raise HTTPException(404, f"订单 {order_id} 不存在")

    return _json({k: _norm(v) for k, v in dict(row).items()})


@router.post("/strategy/orders/{order_id}/action")
def order_action(order_id: int, body: OrderAction):
    """确认、取消订单。"""
    action = body.action.lower()
    if action not in ("confirm", "cancel"):
        raise HTTPException(400, "action 必须为 confirm 或 cancel")

    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, status FROM pams_order_drafts WHERE id = %s", (order_id,))
            row = cur.fetchone()

            if not row:
                raise HTTPException(404, f"订单 {order_id} 不存在")
            if row["status"] != "pending":
                raise HTTPException(400, f"订单状态为 {row['status']}，无法操作")

            if action == "confirm":
                cur.execute("""
                    UPDATE pams_order_drafts
                    SET status = 'confirmed', confirmed_at = NOW()
                    WHERE id = %s
                """, (order_id,))
            else:
                cur.execute("""
                    UPDATE pams_order_drafts
                    SET status = 'cancelled'
                    WHERE id = %s
                """, (order_id,))

            conn.commit()

    return _json({"success": True, "order_id": order_id, "action": action})


@router.post("/strategy/orders/{order_id}/execute")
def order_execute(order_id: int, body: OrderExecute = None):
    """标记订单为已执行（可选录入实际成交价和数量）。"""
    with _get_conn() as conn:
        actual_price = body.actual_price if body else None
        actual_size = body.actual_size if body else None
        result = execute_order(conn, order_id, actual_price, actual_size)

    if "error" in result:
        raise HTTPException(400, result["error"])

    return _json(result)


@router.post("/strategy/orders/expire")
def orders_expire():
    """批量过期已超过 valid_until 的订单。"""
    with _get_conn() as conn:
        result = expire_stale_orders(conn)

    return _json(result)


# ==================== 黑名单 ====================

@router.get("/strategy/blacklist")
def list_blacklist():
    """获取当前黑名单。"""
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM pams_strategy_blacklist
                WHERE active = true
                ORDER BY created_at DESC
            """)
            rows = cur.fetchall()

    return _json({"rows": _rows_to_list(rows)})


@router.post("/strategy/blacklist")
def add_blacklist(body: BlacklistAdd):
    """手动添加黑名单。"""
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                INSERT INTO pams_strategy_blacklist (symbol, market, reason, expires_at, active)
                VALUES (%s, %s, %s, %s, true)
                RETURNING id
            """, (body.symbol, body.market, body.reason, body.expires_at))
            row = cur.fetchone()
            conn.commit()

    return _json({"success": True, "id": row["id"]})


@router.delete("/strategy/blacklist/{blacklist_id}")
def remove_blacklist(blacklist_id: int):
    """移除黑名单（设为 inactive）。"""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE pams_strategy_blacklist SET active = false WHERE id = %s
            """, (blacklist_id,))
            conn.commit()

    return _json({"success": True, "id": blacklist_id})


# ==================== 风控快照 ====================

@router.post("/strategy/risk-snapshot/refresh")
def refresh_risk():
    """刷新风控快照（从持仓和订单数据重新计算）。"""
    try:
        with _get_conn() as conn:
            config = load_strategy_config(conn)
            if not config:
                raise HTTPException(500, "策略配置未找到")
            result = refresh_risk_snapshot(conn, config)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("风控快照刷新失败: %s", e, exc_info=True)
        raise HTTPException(500, f"刷新失败: {str(e)}")

    return _json(result)


@router.get("/strategy/risk-snapshot")
def get_risk_snapshot():
    """获取最新风控快照。"""
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM pams_risk_snapshot
                ORDER BY as_of DESC LIMIT 1
            """)
            row = cur.fetchone()

    if not row:
        return _json({
            "as_of": None,
            "b_nav": 0,
            "open_positions": 0,
            "concurrent_risk_pct": 0,
            "monthly_drawdown_pct": 0,
            "circuit_breaker": "normal",
        })

    return _json({k: _norm(v) for k, v in dict(row).items()})


# ==================== 策略状态 ====================

@router.post("/strategy/state/refresh")
def refresh_state():
    """刷新宏观趋势状态（SPX 10月MA）。"""
    try:
        with _get_conn() as conn:
            result = update_trend_state(conn)
    except Exception as e:
        logger.error("宏观趋势刷新失败: %s", e, exc_info=True)
        raise HTTPException(500, f"刷新失败: {str(e)}")

    if "error" in result:
        return _json(result, status_code=500)

    return _json(result)


@router.get("/strategy/state")
def get_strategy_state():
    """获取当前策略趋势状态。"""
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM pams_strategy_state
                ORDER BY as_of DESC LIMIT 1
            """)
            row = cur.fetchone()

    if not row:
        return _json({
            "as_of": None,
            "trend_state": "RISK_NEUTRAL",
            "spx_close": None,
            "spx_ma10": None,
        })

    return _json({k: _norm(v) for k, v in dict(row).items()})


# ==================== 扫描 ====================

@router.post("/strategy/scan/eod")
def trigger_eod_scan():
    """手动触发 A 股 EOD 扫描。"""
    try:
        with _get_conn() as conn:
            summary = run_eod_scan(conn)
    except Exception as e:
        logger.error("EOD 扫描失败: %s", e, exc_info=True)
        raise HTTPException(500, f"扫描失败: {str(e)}")

    # 移除 candidates 中的大量详情，只返回摘要
    result = {
        "scan_date": summary.get("scan_date"),
        "total_scanned": summary.get("total_scanned", 0),
        "universe_pass": summary.get("universe_pass", 0),
        "trend_pass": summary.get("trend_pass", 0),
        "pullback_zone": summary.get("pullback_zone", 0),
        "triggered": summary.get("triggered", 0),
        "orders_created": summary.get("orders_created", 0),
    }

    if "error" in summary:
        result["error"] = summary["error"]

    return _json(result)


@router.get("/strategy/scan/latest")
def get_latest_scan():
    """获取最近扫描结果摘要。"""
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT scan_date,
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE stage = 'TREND_PASS') AS trend_pass,
                    COUNT(*) FILTER (WHERE stage = 'PULLBACK_ZONE') AS pullback_zone,
                    COUNT(*) FILTER (WHERE stage = 'TRIGGERED') AS triggered
                FROM pams_strategy_candidates
                WHERE scan_date = (SELECT MAX(scan_date) FROM pams_strategy_candidates)
                GROUP BY scan_date
            """)
            row = cur.fetchone()

    if not row:
        return _json({"scan_date": None, "total": 0})

    return _json({k: _norm(v) for k, v in dict(row).items()})


@router.get("/strategy/scan/history")
def get_scan_history(limit: int = Query(10, ge=1, le=50)):
    """获取最近 N 次扫描的摘要统计。"""
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT scan_date,
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE stage = 'TREND_PASS') AS trend_pass,
                    COUNT(*) FILTER (WHERE stage = 'PULLBACK_ZONE') AS pullback_zone,
                    COUNT(*) FILTER (WHERE stage = 'TRIGGERED') AS triggered
                FROM pams_strategy_candidates
                GROUP BY scan_date
                ORDER BY scan_date DESC
                LIMIT %s
            """, (limit,))
            rows = cur.fetchall()

    return _json({"rows": _rows_to_list(rows)})


# ==================== 基本面 ====================

@router.get("/strategy/fundamentals/{symbol}")
def get_fundamental_snapshot(symbol: str):
    """获取标的最新基本面快照。"""
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM pams_fundamental_snapshot
                WHERE symbol = %s AND market = 'CN_A'
                ORDER BY as_of DESC LIMIT 1
            """, (symbol,))
            row = cur.fetchone()

    if not row:
        try:
            with _get_conn() as conn:
                result = refresh_fundamental(conn, symbol)
            return _json(result)
        except Exception as e:
            logger.error("基本面刷新失败 %s: %s", symbol, e)
            raise HTTPException(500, f"基本面数据获取失败: {str(e)}")

    return _json({k: _norm(v) for k, v in dict(row).items()})


# ==================== 事件监控 ====================

@router.post("/strategy/events/refresh")
def trigger_events_refresh():
    """手动刷新全部持仓/观察池的事件监控。"""
    try:
        with _get_conn() as conn:
            from .universe_filter import get_universe_symbols
            symbols = get_universe_symbols(conn)
            result = refresh_events_batch(conn, symbols)
    except Exception as e:
        logger.error("事件刷新失败: %s", e, exc_info=True)
        raise HTTPException(500, f"事件刷新失败: {str(e)}")

    return _json(result)


@router.get("/strategy/events")
def list_events(
    symbol: Optional[str] = None,
    severity: Optional[str] = None,
    limit: int = Query(50, ge=1, le=500),
):
    """查询事件列表。"""
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            where = ["is_active = true"]
            params: list = []

            if symbol:
                where.append("symbol = %s")
                params.append(symbol)
            if severity:
                where.append("severity = %s")
                params.append(severity)

            where_sql = " AND ".join(where)
            params.append(limit)

            cur.execute(f"""
                SELECT * FROM pams_event_monitor
                WHERE {where_sql}
                ORDER BY event_date DESC
                LIMIT %s
            """, params)
            rows = cur.fetchall()

    return _json({"rows": _rows_to_list(rows), "count": len(rows)})


# ==================== 情绪 ====================

@router.post("/strategy/sentiment/refresh")
def trigger_sentiment_refresh():
    """手动刷新市场情绪状态。"""
    try:
        with _get_conn() as conn:
            result = refresh_sentiment(conn)
    except Exception as e:
        logger.error("情绪刷新失败: %s", e, exc_info=True)
        raise HTTPException(500, f"情绪刷新失败: {str(e)}")

    return _json(result)


@router.get("/strategy/sentiment")
def get_sentiment():
    """获取最新市场情绪状态。"""
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM pams_sentiment_state
                ORDER BY as_of DESC LIMIT 1
            """)
            row = cur.fetchone()

    if not row:
        return _json({
            "as_of": None,
            "sent_state": "NEUTRAL",
            "sent_score": 70.0,
            "advancers": None,
            "decliners": None,
            "limit_up": None,
            "limit_down": None,
            "big_red_7d": None,
        })

    return _json({k: _norm(v) for k, v in dict(row).items()})


# ==================== 盘中刷新 ====================

@router.post("/strategy/intraday/refresh")
def trigger_intraday_refresh():
    """手动触发盘中 15 分钟刷新。"""
    try:
        with _get_conn() as conn:
            result = run_intraday_refresh(conn)
    except Exception as e:
        logger.error("盘中刷新失败: %s", e, exc_info=True)
        raise HTTPException(500, f"盘中刷新失败: {str(e)}")

    return _json(result)


# ==================== 冷却期 ====================

@router.get("/strategy/cooldowns")
def list_cooldowns():
    """查询当前活跃的冷却期。"""
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM pams_position_cooldown
                WHERE is_active = true
                ORDER BY stop_date DESC
            """)
            rows = cur.fetchall()

    return _json({"rows": _rows_to_list(rows), "count": len(rows)})


@router.post("/strategy/cooldowns/expire")
def trigger_cooldowns_expire():
    """批量过期已到期的冷却期。"""
    try:
        with _get_conn() as conn:
            expired = expire_cooldowns(conn)
    except Exception as e:
        logger.error("冷却期过期失败: %s", e, exc_info=True)
        raise HTTPException(500, f"冷却期过期失败: {str(e)}")

    return _json({"expired": expired})


# ==================== 观察池管理 ====================

@router.post("/strategy/watchlist/scan")
def trigger_watchlist_scan():
    """手动触发观察池板块扫描入池。"""
    try:
        with _get_conn() as conn:
            summary = run_watchlist_scan(conn)
    except Exception as e:
        logger.error("观察池扫描失败: %s", e, exc_info=True)
        raise HTTPException(500, f"扫描失败: {str(e)}")

    return _json(summary)


@router.post("/strategy/watchlist/exit-check")
def trigger_watchlist_exit_check():
    """手动触发观察池退出条件检查。"""
    try:
        with _get_conn() as conn:
            summary = run_exit_check(conn)
    except Exception as e:
        logger.error("观察池退出检查失败: %s", e, exc_info=True)
        raise HTTPException(500, f"退出检查失败: {str(e)}")

    return _json(summary)


@router.get("/strategy/watchlist/exit-history")
def list_exit_history(limit: int = Query(20, ge=1, le=100)):
    """获取观察池退出历史记录。"""
    with _get_conn() as conn:
        rows = get_exit_history(conn, limit)

    return _json({"rows": _rows_to_list(rows), "count": len(rows)})


@router.get("/strategy/watchlist/scan-log")
def list_scan_log(limit: int = Query(20, ge=1, le=100)):
    """获取观察池入池记录。"""
    with _get_conn() as conn:
        rows = get_scan_log(conn, limit)

    return _json({"rows": _rows_to_list(rows), "count": len(rows)})


@router.post("/strategy/cooldowns/{symbol}")
def trigger_stop_loss(symbol: str, body: StopLossRecord = None):
    """手动记录止损，触发冷却期。"""
    try:
        stop_date = body.stop_date if body else None
        with _get_conn() as conn:
            result = record_stop_loss(conn, symbol, stop_date)
    except Exception as e:
        logger.error("止损记录失败 %s: %s", symbol, e, exc_info=True)
        raise HTTPException(500, f"止损记录失败: {str(e)}")

    return _json(result)
