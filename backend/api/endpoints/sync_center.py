"""Unified sync center endpoints.

The sync center is a read-only status layer. It summarizes the state of
valuation refreshes, trade confirmations, account reconciliation, and data
health without changing portfolio data.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras
from fastapi import APIRouter
from services.sync_health import scheduler_health

router = APIRouter(prefix="/sync", tags=["sync"])


def get_conn():
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "pams-postgres"),
        port=int(os.environ.get("PGPORT", "5432")),
        dbname=os.environ.get("PGDATABASE", "pams"),
        user=os.environ.get("PGUSER", "pams"),
        password=os.environ.get("PGPASSWORD", ""),
    )


def _norm(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, list):
        return [_norm(v) for v in value]
    if isinstance(value, dict):
        return {k: _norm(v) for k, v in value.items()}
    return value


def _now_local() -> datetime:
    tz_name = os.environ.get("PAMS_TIMEZONE", os.environ.get("TZ", "Asia/Shanghai"))
    try:
        return datetime.now(ZoneInfo(tz_name))
    except Exception:
        return datetime.now(ZoneInfo("Asia/Shanghai"))


def _latest_sync_run(cur, sync_key: str, asset_scope: str | None = None) -> dict[str, Any] | None:
    where = ["job_key = %s"]
    params: list[Any] = [sync_key]
    if asset_scope:
        where.append("asset_scope = %s")
        params.append(asset_scope)
    cur.execute(
        f"""
        SELECT job_key AS sync_key,
               asset_scope,
               status,
               started_at,
               finished_at,
               target_date AS target_trade_date,
               data_as_of AS latest_trade_date_after,
               rows_affected AS rows_upserted,
               summary_json, error_text
        FROM pams_data_job_run
        WHERE {' AND '.join(where)}
        ORDER BY started_at DESC
        LIMIT 1
        """,
        params,
    )
    row = cur.fetchone()
    return dict(row) if row else None


def _holding_summary(
    cur,
    *,
    major_category: str | None = None,
    minor_category: str | None = None,
    account_code: str | None = None,
) -> dict[str, Any]:
    where = ["1=1"]
    params: list[Any] = []
    if major_category:
        where.append("major_category = %s")
        params.append(major_category)
    if minor_category:
        where.append("minor_category = %s")
        params.append(minor_category)
    if account_code:
        where.append("account_code = %s")
        params.append(account_code)
    where_sql = " AND ".join(where)
    cur.execute(
        f"""
        WITH latest AS (
            SELECT MAX(as_of) AS as_of
            FROM pams_holdings_snapshot
            WHERE {where_sql}
        )
        SELECT l.as_of,
               MAX(h.updated_at) AS updated_at,
               COUNT(*) AS rows,
               COALESCE(SUM(h.market_value), 0) AS market_value
        FROM latest l
        LEFT JOIN pams_holdings_snapshot h
          ON h.as_of = l.as_of
         AND {where_sql}
        GROUP BY l.as_of
        """,
        [*params, *params],
    )
    row = cur.fetchone()
    if not row:
        return {"as_of": None, "updated_at": None, "rows": 0, "market_value": 0}
    return dict(row)


def _count_query(cur, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any]:
    cur.execute(sql, params)
    row = cur.fetchone()
    return dict(row) if row else {}


def _fresh_status(as_of: Any, *, needs_review: bool = False, failed: bool = False) -> tuple[str, str]:
    if failed:
        return "error", "异常"
    if needs_review:
        return "review", "需核准"
    if not as_of:
        return "missing", "缺数据"
    try:
        observed = date.fromisoformat(str(as_of)[:10])
        current = _now_local().date()
        if observed > current:
            return "review", "日期异常"
        # Weekends are excluded; exchange-specific holidays remain a review signal.
        weekdays = sum((current - timedelta(days=i)).weekday() < 5 for i in range(min((current - observed).days, 10)))
        if weekdays > 3:
            return "stale", "数据过期"
    except (TypeError, ValueError):
        return "review", "日期异常"
    return "ok", "正常"


def _row(
    *,
    id: str,
    source: str,
    sync_type: str,
    latest_date: Any,
    latest_time: Any = None,
    rows: int | float | None = None,
    status: str = "ok",
    status_label: str = "正常",
    action: str,
    href: str,
    details: str = "",
    trigger_endpoint: str | None = None,
    trigger_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if status == "ok" and sync_type == "行情刷新":
        status, status_label = _fresh_status(latest_date)
    if status == "stale":
        status = "review"
    return {
        "id": id,
        "source": source,
        "sync_type": sync_type,
        "latest_date": _norm(latest_date),
        "latest_time": _norm(latest_time),
        "rows": int(rows or 0),
        "status": status,
        "status_label": status_label,
        "action": action,
        "href": href,
        "details": details,
        "trigger_endpoint": trigger_endpoint,
        "trigger_body": trigger_body or {},
    }


@router.get("/center")
def sync_center() -> dict[str, Any]:
    """Return a normalized sync matrix for the mobile sync center."""
    now = _now_local()
    with get_conn() as conn:
        from services.ibkr_performance import published_report_status

        ibkr_quality = published_report_status(conn)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            ibkr_run = _latest_sync_run(cur, "ibkr_flex", "ibkr")
            cn_run = _latest_sync_run(cur, "cn_a_ohlcv_daily", "cn_a_equity")
            a_stock = _holding_summary(cur, major_category="A股", minor_category="A股")
            a_fund = _holding_summary(cur, major_category="A股", minor_category="A股基金")
            us_stock = _holding_summary(cur, major_category="美股")
            metal = _holding_summary(cur, major_category="贵金属")
            ibkr_cash = _holding_summary(cur, account_code="ibkr")
            fx = _count_query(
                cur,
                """
                SELECT MAX(as_of) AS as_of, MAX(fetched_at) AS updated_at, COUNT(*) AS rows
                FROM pams_fx_rates_daily
                """,
            )
            fund_review = _count_query(
                cur,
                """
                WITH latest AS (
                  SELECT MAX(as_of) AS as_of
                  FROM pams_asset_market_daily
                  WHERE source IN (
                    'tushare_fund_nav',
                    'eastmoney_f10',
                    'tiantian_fundgz_estimate',
                    'pams_fund_nav_estimates'
                  )
                )
                SELECT l.as_of, MAX(e.updated_at) AS updated_at, COUNT(*) AS rows,
                       COUNT(*) FILTER (WHERE e.confidence = 'needs_review') AS review_rows
                FROM latest l
                LEFT JOIN pams_asset_market_daily e
                  ON e.as_of = l.as_of
                 AND e.source IN (
                    'tushare_fund_nav',
                    'eastmoney_f10',
                    'tiantian_fundgz_estimate',
                    'pams_fund_nav_estimates'
                 )
                GROUP BY l.as_of
                """,
            )
            investment_tx = _count_query(
                cur,
                """
                SELECT MAX(trade_date) AS as_of, MAX(updated_at) AS updated_at, COUNT(*) AS rows
                FROM pams_investment_trade_fact
                """,
            )
            bank_rows = _count_query(
                cur,
                """
                SELECT MAX(event_date) AS as_of, MAX(created_at) AS updated_at, COUNT(*) AS rows
                FROM pams_source_row
                WHERE source_table = 'pams_bank_statement_rows'
                """,
            )
            cash_tx = _count_query(
                cur,
                """
                SELECT MAX(event_date) AS as_of, MAX(updated_at) AS updated_at, COUNT(*) AS rows
                FROM pams_cashflow_fact
                """,
            )

    ibkr_status, ibkr_label = ibkr_quality["status"], ibkr_quality["label"]
    fund_needs_review = int(fund_review.get("review_rows") or 0) > 0
    fund_status, fund_label = _fresh_status(
        fund_review.get("as_of") or a_fund.get("as_of"),
        needs_review=fund_needs_review,
    )
    cn_failed = bool(cn_run and cn_run.get("status") not in {"success", "ok"})
    cn_status, cn_label = _fresh_status(
        cn_run.get("latest_trade_date_after") if cn_run else None,
        failed=cn_failed,
    )

    rows = [
        _row(
            id="market-a-stock",
            source="A股个股行情",
            sync_type="行情刷新",
            latest_date=a_stock.get("as_of"),
            latest_time=a_stock.get("updated_at"),
            rows=a_stock.get("rows"),
            status="ok" if a_stock.get("as_of") else "missing",
            status_label="正常" if a_stock.get("as_of") else "缺数据",
            action="无交易则刷新行情；有交易先导入同花顺底表",
            href="investments.html#holdings-smart-table",
            details="公开行情只负责价格和市值。买入、卖出、股息、现金变化仍以同花顺底表或你的确认信息为准。",
            trigger_endpoint="/api/prices/refresh/investments",
            trigger_body={"scopes": ["a_stock"]},
        ),
        _row(
            id="market-us-stock",
            source="美股/海外行情",
            sync_type="行情刷新",
            latest_date=us_stock.get("as_of"),
            latest_time=us_stock.get("updated_at"),
            rows=us_stock.get("rows"),
            status="ok" if us_stock.get("as_of") else "missing",
            status_label="正常" if us_stock.get("as_of") else "缺数据",
            action="刷新公开行情；IBKR授权后同步真实现金和交易",
            href="investments.html#holdings-smart-table",
            details="公开行情用于更新持仓市值和今日盈亏；IBKR Flex 用于更新真实持仓、现金、交易和历史净值。",
            trigger_endpoint="/api/prices/refresh/investments",
            trigger_body={"scopes": ["us_stock"]},
        ),
        _row(
            id="market-fund",
            source="A股基金净值",
            sync_type="行情刷新",
            latest_date=fund_review.get("as_of") or a_fund.get("as_of"),
            latest_time=fund_review.get("updated_at") or a_fund.get("updated_at"),
            rows=fund_review.get("rows") or a_fund.get("rows"),
            status=fund_status,
            status_label=fund_label,
            action="估算净值刷新；大偏差进入人工核准",
            href="investments.html#holdings-smart-table",
            details=f"当日估算净值和上一交易日确认净值会同时写入。需核准项目 {int(fund_review.get('review_rows') or 0)} 项。",
            trigger_endpoint="/api/prices/refresh/investments",
            trigger_body={"scopes": ["a_fund"]},
        ),
        _row(
            id="market-metal",
            source="贵金属行情",
            sync_type="行情刷新",
            latest_date=metal.get("as_of"),
            latest_time=metal.get("updated_at"),
            rows=metal.get("rows"),
            status="ok" if metal.get("as_of") else "missing",
            status_label="正常" if metal.get("as_of") else "缺数据",
            action="刷新黄金代理价格；定期核准建行余额",
            href="investments.html#holdings-smart-table",
            details="贵金属当前主要使用本地黄金 ETF 代理价格，建行积存金余额仍需按账户截图或账单核准。",
            trigger_endpoint="/api/prices/refresh/investments",
            trigger_body={"scopes": ["precious_metal"]},
        ),
        _row(
            id="ibkr-flex",
            source="IBKR Flex",
            sync_type="账户真值同步",
            latest_date=ibkr_quality["statement_as_of"],
            latest_time=ibkr_run.get("finished_at") if ibkr_run else None,
            rows=ibkr_quality["active_row_count"],
            status=ibkr_status,
            status_label=ibkr_label,
            action="有当日授权则同步 Flex",
            href="data_sources.html#syncMatrix",
            details=(f"{ibkr_label}；应有报告日期 {ibkr_quality['expected_statement_as_of'] or '待核准'}；"
                     f"最近尝试 {ibkr_quality['last_attempt_status'] or '无'}。"
                     "失败、过期或未提交的原始文件不替代有效报告。"),
            trigger_endpoint="/api/ibkr/flex/sync",
            trigger_body={"align_to_latest_snapshot": True},
        ),
        _row(
            id="investment-ledger",
            source="投资交易确认",
            sync_type="交易确认",
            latest_date=investment_tx.get("as_of"),
            latest_time=investment_tx.get("updated_at"),
            rows=investment_tx.get("rows"),
            status="ok" if investment_tx.get("as_of") else "missing",
            status_label="正常" if investment_tx.get("as_of") else "缺数据",
            action="有买卖/申购确认时先更新交易",
            href="investments.html#investment-ledger",
            details="交易确认会影响数量、成本、手续费和在途资金，不应和行情刷新混用。",
        ),
        _row(
            id="account-reconcile",
            source="银行/钱包账单",
            sync_type="账户核准",
            latest_date=bank_rows.get("as_of"),
            latest_time=bank_rows.get("updated_at"),
            rows=bank_rows.get("rows"),
            status="ok" if bank_rows.get("as_of") else "missing",
            status_label="正常" if bank_rows.get("as_of") else "缺数据",
            action="导入账单后做流水匹配和余额核准",
            href="ledger.html#accountIssueStrip",
            details="银行、微信、支付宝、Santander、工行等账单负责现金余额、负债、内部转账和投资入出金核准。",
        ),
        _row(
            id="cash-ledger",
            source="现金流水",
            sync_type="账户核准",
            latest_date=cash_tx.get("as_of"),
            latest_time=cash_tx.get("updated_at"),
            rows=cash_tx.get("rows"),
            status="ok" if cash_tx.get("as_of") else "missing",
            status_label="正常" if cash_tx.get("as_of") else "缺数据",
            action="对账后更新真实收支分类",
            href="ledger.html",
            details="现金流水用于消费、收入和转账匹配；投资交易不放在这里计算收益。",
        ),
        _row(
            id="fx-rates",
            source="汇率",
            sync_type="行情刷新",
            latest_date=fx.get("as_of"),
            latest_time=fx.get("updated_at"),
            rows=fx.get("rows"),
            status="ok" if fx.get("as_of") else "missing",
            status_label="正常" if fx.get("as_of") else "缺数据",
            action="刷新 CNY 折算汇率",
            href="data_sources.html#syncMatrix",
            details="所有非 CNY 资产按最新汇率折算到 CNY，总览和风控依赖该口径。",
        ),
        _row(
            id="datahub-cn-a",
            source="A股研究数据",
            sync_type="数据健康",
            latest_date=cn_run.get("latest_trade_date_after") if cn_run else None,
            latest_time=cn_run.get("finished_at") if cn_run else None,
            rows=cn_run.get("rows_upserted") if cn_run else 0,
            status=cn_status,
            status_label=cn_label,
            action="修复 DataHub 入库任务",
            href="research.html",
            details=cn_run.get("error_text") if cn_run else "研究数据入库状态缺失。",
        ),
    ]

    health = scheduler_health(Path(os.environ.get("PAMS_REFRESH_STATE_PATH", "/scheduler-state/refresh.json")))
    rows.append(_row(
        id="automatic-refresh", source="自动收益刷新", sync_type="同步健康",
        latest_date=None, latest_time=health.get("latest_time"), rows=len(health["jobs"]),
        status=health["status"], status_label=health["label"],
        action="查看阻断步骤及历史缺口", href="data_sources.html#syncMatrix",
        details="；".join(
            f"{key}: {job.get('status')}，最近成功 {job.get('last_success_date') or '无'}，"
            f"历史缺口起点 {job.get('history_gap_since') or '尚未核验'}，"
            f"问题 {job.get('issues', [])}，"
            f"检查 {job.get('steps', [])}"
            for key, job in health["jobs"].items()
        ) or "尚无持久化自动刷新记录。",
    ))
    summary = {
        "total": len(rows),
        "ok": sum(1 for row in rows if row["status"] == "ok"),
        "manual_required": sum(1 for row in rows if row["status"] in {"review", "missing", "error", "stale"}),
        "errors": sum(1 for row in rows if row["status"] == "error"),
        "reviews": sum(1 for row in rows if row["status"] == "review"),
        "last_ibkr_sync": _norm(ibkr_run.get("finished_at")) if ibkr_run else None,
        "last_investment_date": _norm(max(
            [v for v in [a_stock.get("as_of"), a_fund.get("as_of"), us_stock.get("as_of"), metal.get("as_of")] if v],
            default=None,
        )),
    }
    workflows = [
        {"label": "行情刷新", "description": "只更新价格、市值、今日盈亏。"},
        {"label": "交易确认", "description": "更新买卖、申购确认、份额、成本、手续费和在途资金。"},
        {"label": "账户核准", "description": "用银行/钱包/券商真值核准现金、负债、入出金和内部转账。"},
        {"label": "数据健康", "description": "检查研究数据、接口失败、缺失字段和过期数据。"},
    ]
    return {
        "as_of": now.date().isoformat(),
        "updated_at": now.isoformat(timespec="seconds"),
        "summary": summary,
        "rows": rows,
        "workflows": workflows,
        "scheduler": health,
    }
