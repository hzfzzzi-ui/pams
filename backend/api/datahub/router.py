"""
DataHub API 路由 — Tushare 数据 ingest 触发端点。
"""
import logging
import os
import subprocess
from datetime import date
from typing import Any, Dict, List

from fastapi import APIRouter
import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/datahub", tags=["datahub"])


def _get_conn():
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "pams-postgres"),
        port=int(os.environ.get("PGPORT", "5432")),
        dbname=os.environ.get("PGDATABASE", "pams"),
        user=os.environ.get("PGUSER", "pams"),
        password=os.environ.get("PGPASSWORD", ""),
    )


@router.post("/ingest/daily")
def trigger_daily_ingest(target_date: str = ""):
    """
    触发 Tushare 每日增量数据入库。

    由 scheduler 在每交易日 16:30 CST 调用。
    如果 TUSHARE_TOKEN 未设置，返回跳过状态。

    Args:
        target_date: 目标日期 (YYYY-MM-DD)，默认今天
    """
    token = os.environ.get("TUSHARE_TOKEN", "")
    if not token:
        logger.warning("TUSHARE_TOKEN 未设置，跳过 daily ingest")
        return {"status": "skipped", "reason": "TUSHARE_TOKEN not set"}

    if not target_date:
        target_date = date.today().isoformat()

    logger.info("触发 daily ingest: %s", target_date)

    try:
        result = subprocess.run(
            [
                "python", "-m", "datahub.ingest.tushare_ingest",
                "daily", "--date", target_date,
            ],
            capture_output=True,
            text=True,
            timeout=600,
        )

        if result.returncode == 0:
            logger.info("daily ingest 完成: %s", target_date)
            return {
                "status": "ok",
                "date": target_date,
                "stdout": result.stdout[-500:] if result.stdout else "",
            }
        else:
            logger.error("daily ingest 失败: %s", result.stderr[-500:])
            return {
                "status": "error",
                "date": target_date,
                "stderr": result.stderr[-500:] if result.stderr else "",
            }

    except subprocess.TimeoutExpired:
        logger.error("daily ingest 超时 (600s)")
        return {"status": "timeout", "date": target_date}
    except Exception as e:
        logger.error("daily ingest 异常: %s", e)
        return {"status": "error", "date": target_date, "error": str(e)}


@router.get("/ingest/status")
def ingest_status():
    """
    查询 PIT 数据版本和状态。
    """
    from datahub import pit_reader

    version = pit_reader.get_data_version()
    return {
        "status": "ok",
        "data_version": version,
    }


@router.get("/health/panel")
def data_health_panel() -> Dict[str, Any]:
    """
    数据健康面板：评估 A 股日级回测可用性。
    """
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE market='CN_A') AS cn_a_rows,
                    COUNT(*) FILTER (WHERE market='CN_INDEX') AS cn_index_rows,
                    COUNT(*) FILTER (WHERE market='CN_A' AND limit_up IS NOT NULL AND limit_down IS NOT NULL) AS cn_a_rows_with_limits,
                    COUNT(*) FILTER (WHERE market='CN_A' AND is_suspended = TRUE) AS cn_a_suspended_rows,
                    MIN(trade_date) FILTER (WHERE market='CN_A') AS cn_a_min_date,
                    MAX(trade_date) FILTER (WHERE market='CN_A') AS cn_a_max_date,
                    MIN(trade_date) FILTER (WHERE market='CN_INDEX') AS cn_index_min_date,
                    MAX(trade_date) FILTER (WHERE market='CN_INDEX') AS cn_index_max_date
                FROM pams_ohlcv_daily
                """
            )
            s = cur.fetchone()

            cur.execute(
                """
                SELECT COUNT(*) AS symbols_cn_a
                FROM (SELECT DISTINCT symbol FROM pams_ohlcv_daily WHERE market='CN_A') t
                """
            )
            symbols_cn_a = cur.fetchone()["symbols_cn_a"]

            cur.execute(
                """
                SELECT COUNT(*) AS symbols_cn_index
                FROM (SELECT DISTINCT symbol FROM pams_ohlcv_daily WHERE market='CN_INDEX') t
                """
            )
            symbols_cn_index = cur.fetchone()["symbols_cn_index"]

            cur.execute(
                """
                SELECT COUNT(*) AS pit_index_rows, COUNT(DISTINCT index_code) AS pit_index_codes
                FROM pit_index_daily
                """
            )
            pit_idx = cur.fetchone()

            cur.execute(
                """
                SELECT COUNT(*) AS sec_rows,
                       COUNT(*) FILTER (WHERE delist_date IS NOT NULL) AS sec_with_delist
                FROM pit_security_master
                WHERE market_type='CN_A'
                """
            )
            sec = cur.fetchone()

            cur.execute(
                """
                SELECT COUNT(*) AS dup_rows
                FROM (
                    SELECT symbol, market, trade_date
                    FROM pams_ohlcv_daily
                    GROUP BY symbol, market, trade_date
                    HAVING COUNT(*) > 1
                ) t
                """
            )
            dup_rows = cur.fetchone()["dup_rows"]

    cn_a_rows = int(s["cn_a_rows"] or 0)
    cn_index_rows = int(s["cn_index_rows"] or 0)
    limits_rows = int(s["cn_a_rows_with_limits"] or 0)
    suspended_rows = int(s["cn_a_suspended_rows"] or 0)
    symbols_index = int(symbols_cn_index or 0)
    sec_rows = int(sec["sec_rows"] or 0)
    sec_with_delist = int(sec["sec_with_delist"] or 0)

    limits_cov = (100.0 * limits_rows / cn_a_rows) if cn_a_rows else 0.0
    delist_cov = (100.0 * sec_with_delist / sec_rows) if sec_rows else 0.0

    checks: List[Dict[str, Any]] = [
        {"key": "cn_a_rows", "ok": cn_a_rows > 1_000_000, "value": cn_a_rows, "target": "> 1,000,000"},
        {"key": "cn_a_max_date", "ok": s["cn_a_max_date"] is not None, "value": s["cn_a_max_date"]},
        {"key": "cn_index_symbols", "ok": symbols_index >= 3, "value": symbols_index, "target": ">= 3"},
        {"key": "limits_coverage_pct", "ok": limits_cov >= 90.0, "value": round(limits_cov, 2), "target": ">= 90%"},
        {"key": "duplicate_rows", "ok": int(dup_rows or 0) == 0, "value": int(dup_rows or 0), "target": "0"},
    ]

    ready = all(c["ok"] for c in checks)
    warnings: List[str] = []
    if suspended_rows == 0:
        warnings.append("suspend_d 无权限或无数据，停牌标记覆盖为 0。")
    if delist_cov < 1.0:
        warnings.append("pit_security_master 的 delist_date 覆盖偏低，PIT universe 可能有生存者偏差。")

    return {
        "status": "ok",
        "ready_for_cn_daily_backtest": ready,
        "checks": checks,
        "warnings": warnings,
        "metrics": {
            "pams_ohlcv_daily": {
                "cn_a_rows": cn_a_rows,
                "cn_a_symbols": int(symbols_cn_a or 0),
                "cn_a_min_date": s["cn_a_min_date"],
                "cn_a_max_date": s["cn_a_max_date"],
                "cn_index_rows": cn_index_rows,
                "cn_index_symbols": symbols_index,
                "cn_index_min_date": s["cn_index_min_date"],
                "cn_index_max_date": s["cn_index_max_date"],
                "duplicate_rows": int(dup_rows or 0),
            },
            "quality": {
                "limits_rows": limits_rows,
                "limits_coverage_pct": round(limits_cov, 2),
                "suspended_rows": suspended_rows,
            },
            "pit": {
                "pit_index_rows": int(pit_idx["pit_index_rows"] or 0),
                "pit_index_codes": int(pit_idx["pit_index_codes"] or 0),
                "security_rows": sec_rows,
                "security_with_delist": sec_with_delist,
                "delist_coverage_pct": round(delist_cov, 2),
            },
        },
    }
