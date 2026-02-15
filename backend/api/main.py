"""
PAMS API V2 - 基于重构后的数据库结构
- 使用account_code替代venue+account
- 使用major_category/minor_category替代asset_class_map
- 支持多币种汇率换算
"""
import os
from typing import Any, Dict, List, Optional
from datetime import date as Date

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# 导入端点路由
from endpoints import transactions
from endpoints import tasks

app = FastAPI(title="pams-api-v2", version="2.0.0")

# 注册路由
app.include_router(transactions.router, prefix="/api")
app.include_router(tasks.router, prefix="/api")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==================== 数据库连接 ====================

def get_conn():
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "pams-postgres"),
        port=int(os.environ.get("PGPORT", "5432")),
        dbname=os.environ.get("PGDATABASE", "pams"),
        user=os.environ.get("PGUSER", "pams"),
        password=os.environ.get("PGPASSWORD", ""),
    )

def json(data: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        content=data,
        status_code=status_code,
        media_type="application/json; charset=utf-8"
    )

def _norm(v: Any):
    """标准化输出值"""
    if hasattr(v, "isoformat"):
        return v.isoformat()
    from decimal import Decimal
    if isinstance(v, Decimal):
        return str(v)
    return v

# ==================== 账户管理 ====================

@app.get("/api/accounts")
def get_accounts(active: bool = Query(True)):
    """获取账户列表"""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if active:
                cur.execute("""
                    SELECT account_code, display_name, base_currency, is_asset
                    FROM pams_accounts
                    WHERE active = true
                    ORDER BY display_name
                """)
            else:
                cur.execute("""
                    SELECT account_code, display_name, base_currency, is_asset, active
                    FROM pams_accounts
                    ORDER BY display_name
                """)
            rows = cur.fetchall()

    return json({"accounts": [{k: _norm(v) for k, v in dict(r).items()} for r in rows]})

@app.get("/api/accounts/{account_code}")
def get_account_detail(account_code: str):
    """获取单个账户详情"""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT account_code, display_name, base_currency, is_asset, active, created_at
                FROM pams_accounts
                WHERE account_code = %s
            """, (account_code,))
            row = cur.fetchone()

            if not row:
                raise HTTPException(status_code=404, detail=f"账户 {account_code} 不存在")

            # 获取该账户的最新持仓
            cur.execute("""
                SELECT COUNT(*) as holdings_count, ROUND(SUM(market_value)::numeric, 2) as total_value
                FROM pams_holdings_snapshot
                WHERE account_code = %s AND as_of = (SELECT MAX(as_of) FROM pams_holdings_snapshot)
            """, (account_code,))
            stats = cur.fetchone()

            account_data = {k: _norm(v) for k, v in dict(row).items()}
            account_data['holdings_count'] = stats['holdings_count']
            account_data['total_value'] = str(stats['total_value']) if stats['total_value'] else '0'

    return json(account_data)

# ==================== 资产分类 ====================

@app.get("/api/categories")
def get_categories():
    """获取所有资产分类"""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT major_category, minor_category
                FROM pams_asset_categories
                ORDER BY major_category, minor_category
            """)
            rows = cur.fetchall()

    # 按大类分组
    categories_dict = {}
    for r in rows:
        major = r['major_category']
        minor = r['minor_category']
        if major not in categories_dict:
            categories_dict[major] = []
        categories_dict[major].append(minor)

    return json({"categories": categories_dict})

# ==================== 持仓查询 ====================

@app.get("/api/holdings/latest")
def holdings_latest(
    account_code: Optional[str] = None,
    major_category: Optional[str] = None,
    limit: int = Query(500, ge=1, le=5000),
):
    """获取最新持仓"""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # 构建筛选条件
            where_clauses = []
            params = []

            if account_code:
                where_clauses.append("account_code = %s")
                params.append(account_code)

            if major_category:
                where_clauses.append("major_category = %s")
                params.append(major_category)

            where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

            # 获取最新日期
            cur.execute(f"SELECT MAX(as_of) as as_of FROM pams_holdings_snapshot WHERE {where_sql}", params)
            row = cur.fetchone()
            as_of = row["as_of"] if row else None

            if as_of is None:
                return json({"as_of": None, "rows": []})

            # 获取持仓数据
            params_final = params + [as_of, limit]
            cur.execute(f"""
                SELECT
                    id, symbol, name, asset_liability, currency, account_code,
                    major_category, minor_category, market_value, cost, holding_pnl,
                    as_of, created_at, updated_at
                FROM pams_holdings_snapshot
                WHERE {where_sql} AND as_of = %s
                ORDER BY market_value DESC
                LIMIT %s
            """, params_final)
            rows = cur.fetchall()

    out_rows = [{k: _norm(v) for k, v in dict(r).items()} for r in rows]
    return json({"as_of": as_of.isoformat(), "rows": out_rows})

@app.get("/api/holdings/summary")
def holdings_summary(base_currency: str = Query("CNY")):
    """持仓汇总统计（按大类分组，含汇率换算）"""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # 获取最新日期
            cur.execute("SELECT MAX(as_of) as as_of FROM pams_holdings_snapshot")
            as_of = cur.fetchone()["as_of"]

            if not as_of:
                return json({"as_of": None, "base_currency": base_currency, "total": {}, "by_category": []})

            # 汇总统计（含汇率换算）
            cur.execute("""
                WITH h AS (
                    SELECT * FROM pams_holdings_snapshot WHERE as_of = %s
                ), fx AS (
                    SELECT quote_currency, rate
                    FROM pams_fx_rates_daily
                    WHERE as_of = %s AND base_currency = %s
                )
                SELECT
                    major_category,
                    COUNT(*) as holdings_count,
                    ROUND(SUM(h.market_value * fx.rate)::numeric, 2) as total_value_base,
                    ROUND(SUM(COALESCE(h.cost, 0) * fx.rate)::numeric, 2) as total_cost_base,
                    ROUND(SUM((h.market_value - COALESCE(h.cost, 0)) * fx.rate)::numeric, 2) as total_pnl_base
                FROM h
                JOIN fx ON fx.quote_currency = h.currency
                GROUP BY major_category
                ORDER BY total_value_base DESC
            """, (as_of, as_of, base_currency))
            by_category = cur.fetchall()

            # 总计
            total_value = sum(float(r['total_value_base']) for r in by_category)
            total_cost = sum(float(r['total_cost_base']) for r in by_category)
            total_pnl = sum(float(r['total_pnl_base']) for r in by_category)

    return json({
        "as_of": as_of.isoformat(),
        "base_currency": base_currency,
        "total": {
            "value": str(total_value),
            "cost": str(total_cost),
            "pnl": str(total_pnl)
        },
        "by_category": [{k: _norm(v) for k, v in dict(r).items()} for r in by_category]
    })

@app.get("/api/holdings/by-account")
def holdings_by_account(base_currency: str = Query("CNY")):
    """按账户汇总持仓（含汇率换算）"""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT MAX(as_of) as as_of FROM pams_holdings_snapshot")
            as_of = cur.fetchone()["as_of"]

            if not as_of:
                return json({"as_of": None, "by_account": []})

            cur.execute("""
                WITH h AS (
                    SELECT * FROM pams_holdings_snapshot WHERE as_of = %s
                ), fx AS (
                    SELECT quote_currency, rate
                    FROM pams_fx_rates_daily
                    WHERE as_of = %s AND base_currency = %s
                )
                SELECT
                    h.account_code,
                    a.display_name,
                    a.base_currency as account_currency,
                    COUNT(*) as holdings_count,
                    ROUND(SUM(h.market_value * fx.rate)::numeric, 2) as total_value_base
                FROM h
                JOIN fx ON fx.quote_currency = h.currency
                JOIN pams_accounts a ON a.account_code = h.account_code
                GROUP BY h.account_code, a.display_name, a.base_currency
                ORDER BY total_value_base DESC
            """, (as_of, as_of, base_currency))
            rows = cur.fetchall()

    return json({
        "as_of": as_of.isoformat(),
        "base_currency": base_currency,
        "by_account": [{k: _norm(v) for k, v in dict(r).items()} for r in rows]
    })

# ==================== 汇率查询 ====================

@app.get("/api/fx-rates/latest")
def get_latest_fx_rates(base_currency: str = Query("CNY")):
    """获取最新汇率"""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT as_of, base_currency, quote_currency, rate, source
                FROM pams_fx_rates_daily
                WHERE base_currency = %s AND as_of = (
                    SELECT MAX(as_of) FROM pams_fx_rates_daily WHERE base_currency = %s
                )
                ORDER BY quote_currency
            """, (base_currency, base_currency))
            rows = cur.fetchall()

    if not rows:
        return json({"as_of": None, "rates": []})

    return json({
        "as_of": rows[0]['as_of'].isoformat() if rows else None,
        "base_currency": base_currency,
        "rates": [{k: _norm(v) for k, v in dict(r).items()} for r in rows]
    })

@app.post("/api/fx-rates/refresh")
def refresh_fx_rates():
    """从公开API获取最新汇率并存储双向汇率"""
    import urllib.request
    import json as json_mod
    from datetime import date as d_date

    today = d_date.today()
    currencies = ['CNY', 'USD', 'MXN']
    all_rates = {}

    # 分别从公开API获取每个币种为基准的汇率
    for base in currencies:
        url = f"https://open.er-api.com/v6/latest/{base}"
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'PAMS/2.0'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json_mod.loads(resp.read().decode())
                if data.get('result') == 'success':
                    all_rates[base] = data['rates']
        except Exception as e:
            return json({"error": f"获取{base}汇率失败: {str(e)}"}, status_code=502)

    # 存储到数据库
    # API convention: "1 [api_base] = [rate] [target]"
    # PAMS convention: "1 [quote_currency] = [rate] [base_currency]"
    # So: pams_base = target, pams_quote = api_base
    with get_conn() as conn:
        with conn.cursor() as cur:
            stored = 0
            for api_base in currencies:
                rates = all_rates.get(api_base, {})
                for target_cur in currencies:
                    if target_cur == api_base:
                        rate_val = 1.0
                    else:
                        rate_val = rates.get(target_cur)
                        if rate_val is None:
                            continue

                    pams_base = target_cur   # PAMS base = API target
                    pams_quote = api_base    # PAMS quote = API base
                    cur.execute("""
                        INSERT INTO pams_fx_rates_daily (as_of, base_currency, quote_currency, rate, source)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (as_of, base_currency, quote_currency)
                        DO UPDATE SET rate = EXCLUDED.rate, source = EXCLUDED.source
                    """, (today, pams_base, pams_quote, rate_val, 'exchangerate-api'))
                    stored += 1

            conn.commit()

    return json({
        "success": True,
        "as_of": today.isoformat(),
        "stored": stored,
        "rates_preview": {
            "USD/CNY": all_rates.get('USD', {}).get('CNY'),
            "MXN/CNY": all_rates.get('MXN', {}).get('CNY'),
            "CNY/USD": all_rates.get('CNY', {}).get('USD'),
            "CNY/MXN": all_rates.get('CNY', {}).get('MXN'),
        }
    })


# ==================== 前端兼容端点 ====================

def _get_base_currency(cur) -> str:
    """获取基准币种"""
    try:
        cur.execute("SELECT value->>'base_currency' AS base_currency FROM pams_policy WHERE key='ips_v1'")
        r = cur.fetchone()
        return (r.get('base_currency') if r else None) or 'CNY'
    except Exception:
        return 'CNY'


@app.get("/summary2/latest")
def summary2_latest():
    """总览页汇总（前端兼容）- 使用major_category替代asset_class_map"""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT MAX(as_of) AS as_of FROM pams_holdings_snapshot")
            row = cur.fetchone()
            as_of = row["as_of"] if row else None
            if as_of is None:
                return json({"as_of": None, "base_currency": "CNY", "totals": {}, "by_asset_class": [], "children": []})

            base_currency = _get_base_currency(cur)

            # by_asset_class: 按major_category分组，负债归为LIABILITY
            cur.execute("""
                WITH h AS (
                    SELECT * FROM pams_holdings_snapshot WHERE as_of = %s
                ), fx AS (
                    SELECT quote_currency, rate
                    FROM pams_fx_rates_daily
                    WHERE as_of = %s AND base_currency = %s
                )
                SELECT
                    CASE WHEN h.asset_liability = '负债' THEN 'LIABILITY'
                         ELSE h.major_category END AS asset_class,
                    COALESCE(SUM(h.market_value * fx.rate), 0) AS market_value,
                    COALESCE(SUM(COALESCE(h.holding_pnl, 0) * fx.rate), 0) AS holding_pnl,
                    0 AS daily_pnl
                FROM h
                JOIN fx ON fx.quote_currency = h.currency
                GROUP BY CASE WHEN h.asset_liability = '负债' THEN 'LIABILITY'
                              ELSE h.major_category END
                ORDER BY market_value DESC
            """, (as_of, as_of, base_currency))
            by_asset_class = cur.fetchall()

            # children: 每条持仓明细
            cur.execute("""
                WITH h AS (
                    SELECT * FROM pams_holdings_snapshot WHERE as_of = %s
                ), fx AS (
                    SELECT quote_currency, rate
                    FROM pams_fx_rates_daily
                    WHERE as_of = %s AND base_currency = %s
                )
                SELECT
                    CASE WHEN h.asset_liability = '负债' THEN 'LIABILITY'
                         ELSE h.major_category END AS asset_class,
                    h.account_code AS venue,
                    h.account_code AS account,
                    h.minor_category AS type,
                    h.symbol, h.name, h.currency,
                    h.market_value * fx.rate AS market_value,
                    COALESCE(h.holding_pnl, 0) * fx.rate AS holding_pnl,
                    0 AS daily_pnl
                FROM h
                JOIN fx ON fx.quote_currency = h.currency
                ORDER BY CASE WHEN h.asset_liability = '负债' THEN 'LIABILITY'
                              ELSE h.major_category END,
                         market_value DESC
            """, (as_of, as_of, base_currency))
            children = cur.fetchall()

            totals = {'market_value': 0, 'holding_pnl': 0, 'daily_pnl': 0}
            for r in by_asset_class:
                totals['market_value'] += float(r['market_value'] or 0)
                totals['holding_pnl'] += float(r['holding_pnl'] or 0)
                totals['daily_pnl'] += float(r['daily_pnl'] or 0)

    return json({
        'as_of': as_of.isoformat(),
        'base_currency': base_currency,
        'totals': {k: str(v) for k, v in totals.items()},
        'by_asset_class': [{k: _norm(v) for k, v in dict(r).items()} for r in by_asset_class],
        'children': [{k: _norm(v) for k, v in dict(r).items()} for r in children],
    })


@app.get("/holdings/latest")
def holdings_latest_compat(limit: int = Query(500, ge=1, le=5000)):
    """持仓页（前端兼容格式：account, type, name, currency, market_value, holding_pnl, daily_pnl）"""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT MAX(as_of) AS as_of FROM pams_holdings_snapshot")
            row = cur.fetchone()
            as_of = row["as_of"] if row else None
            if as_of is None:
                return json({"as_of": None, "rows": []})

            cur.execute("""
                SELECT
                    account_code AS account,
                    minor_category AS type,
                    symbol, name, currency, market_value,
                    COALESCE(holding_pnl, 0) AS holding_pnl,
                    0 AS daily_pnl,
                    major_category, minor_category, as_of
                FROM pams_holdings_snapshot
                WHERE as_of = %s
                ORDER BY market_value DESC NULLS LAST
                LIMIT %s
            """, (as_of, limit))
            rows = cur.fetchall()

    return json({
        "as_of": as_of.isoformat(),
        "rows": [{k: _norm(v) for k, v in dict(r).items()} for r in rows]
    })


@app.get("/holdings/by-category/{category}")
def holdings_by_category(category: str, limit: int = Query(500, ge=1, le=5000)):
    """按大类查询持仓（A股/美股/加密/其他）"""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT MAX(as_of) AS as_of FROM pams_holdings_snapshot")
            row = cur.fetchone()
            as_of = row["as_of"] if row else None
            if as_of is None:
                return json({"as_of": None, "rows": []})

            if category == '其他':
                cur.execute("""
                    SELECT account_code AS account, minor_category, symbol, name,
                           currency, market_value,
                           COALESCE(holding_pnl, 0) AS holding_pnl,
                           0 AS daily_pnl, major_category
                    FROM pams_holdings_snapshot
                    WHERE as_of = %s AND asset_liability = '资产'
                      AND major_category NOT IN ('A股', '美股', '加密')
                    ORDER BY market_value DESC NULLS LAST
                    LIMIT %s
                """, (as_of, limit))
            else:
                cur.execute("""
                    SELECT account_code AS account, minor_category, symbol, name,
                           currency, market_value,
                           COALESCE(holding_pnl, 0) AS holding_pnl,
                           0 AS daily_pnl, major_category
                    FROM pams_holdings_snapshot
                    WHERE as_of = %s AND major_category = %s
                    ORDER BY market_value DESC NULLS LAST
                    LIMIT %s
                """, (as_of, category, limit))
            rows = cur.fetchall()

    return json({
        "as_of": as_of.isoformat(),
        "rows": [{k: _norm(v) for k, v in dict(r).items()} for r in rows]
    })


@app.get("/holdings/history/{category}")
def holdings_history_by_category(
    category: str,
    days: int = Query(90, ge=1, le=365),
    interval: str = Query("daily"),
):
    """按大类查询历史市值趋势"""
    from datetime import timedelta
    start_date = Date.today() - timedelta(days=days)

    date_expr = "DATE_TRUNC('week', as_of)::date" if interval == 'weekly' else "as_of"

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if category == '其他':
                cur.execute(f"""
                    SELECT {date_expr} AS date, SUM(market_value) AS value
                    FROM pams_holdings_snapshot
                    WHERE as_of >= %s AND asset_liability = '资产'
                      AND major_category NOT IN ('A股', '美股', '加密')
                    GROUP BY {date_expr}
                    ORDER BY date
                """, (start_date,))
            else:
                cur.execute(f"""
                    SELECT {date_expr} AS date, SUM(market_value) AS value
                    FROM pams_holdings_snapshot
                    WHERE as_of >= %s AND major_category = %s
                    GROUP BY {date_expr}
                    ORDER BY date
                """, (start_date, category))
            rows = cur.fetchall()

    return json({
        "series": [{"date": _norm(r['date']), "value": float(r['value'] or 0)} for r in rows]
    })


@app.get("/nav/series")
def nav_series(limit: int = Query(90, ge=1, le=365), baseCurrency: str = "CNY"):
    """NAV时间序列"""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT MAX(as_of) AS as_of FROM pams_holdings_snapshot")
            row = cur.fetchone()
            latest_as_of = row["as_of"] if row else None

            cur.execute("""
                SELECT as_of, total_market_value, holdings_count, venues_count, completeness
                FROM pams_nav_daily
                ORDER BY as_of DESC
                LIMIT %s
            """, (limit,))
            rows = cur.fetchall()

    rows = list(reversed(rows))
    result = [
        {
            "asOfDate": r["as_of"].isoformat() if hasattr(r["as_of"], "isoformat") else str(r["as_of"]),
            "netWorth": float(r["total_market_value"] or 0),
        }
        for r in rows
    ]

    if latest_as_of and (not result or result[-1]["asOfDate"] != latest_as_of.isoformat()):
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    WITH h AS (
                        SELECT * FROM pams_holdings_snapshot WHERE as_of = %s
                    ), fx AS (
                        SELECT quote_currency, rate
                        FROM pams_fx_rates_daily
                        WHERE as_of = %s AND base_currency = %s
                    )
                    SELECT COALESCE(SUM(h.market_value * fx.rate), 0) AS total_mv
                    FROM h JOIN fx ON fx.quote_currency = h.currency
                """, (latest_as_of, latest_as_of, baseCurrency))
                row = cur.fetchone()
                total_mv = float(row["total_mv"] or 0) if row else 0.0
        result.append({"asOfDate": latest_as_of.isoformat(), "netWorth": total_mv})

    return json({"rows": result})


@app.get("/nav/drawdown")
def nav_drawdown(limit: int = Query(365, ge=2, le=5000)):
    """NAV回撤"""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                WITH r AS (
                    SELECT as_of, total_market_value, completeness
                    FROM pams_nav_daily ORDER BY as_of DESC LIMIT %s
                ), x AS (
                    SELECT * FROM r ORDER BY as_of
                ), s AS (
                    SELECT as_of, total_market_value, completeness,
                           MAX(total_market_value) OVER (ORDER BY as_of) AS peak
                    FROM x
                )
                SELECT as_of, total_market_value, completeness, peak,
                       CASE WHEN peak=0 THEN 0 ELSE (total_market_value/peak - 1) END AS drawdown
                FROM s ORDER BY as_of
            """, (limit,))
            rows = cur.fetchall()

    mdd = None
    for r in rows:
        try:
            dd = float(r['drawdown'])
            mdd = dd if mdd is None else min(mdd, dd)
        except Exception:
            continue

    return json({
        'max_drawdown': None if mdd is None else str(mdd),
        'rows': [{k: _norm(v) for k, v in dict(r).items()} for r in rows]
    })


@app.get("/cashflow/series")
def cashflow_series(limit: int = Query(30, ge=1, le=365), currency: str = "CNY"):
    """现金流序列（占位）"""
    return json({"rows": []})


@app.get("/risk/latest")
def risk_latest(limit: int = Query(100, ge=1, le=1000)):
    """风险事件"""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT MAX(as_of) AS as_of FROM pams_holdings_snapshot")
            row = cur.fetchone()
            as_of = row["as_of"] if row else None
            if as_of is None:
                return json({"as_of": None, "events": [], "rows": []})

            cur.execute("""
                SELECT as_of, severity, code, title, details, created_at
                FROM pams_risk_events
                WHERE as_of = %s
                ORDER BY CASE severity WHEN 'crit' THEN 3 WHEN 'warn' THEN 2 ELSE 1 END DESC,
                         created_at DESC
                LIMIT %s
            """, (as_of, limit))
            rows = cur.fetchall()

    out = [{k: _norm(v) for k, v in dict(r).items()} for r in rows]
    return json({"as_of": as_of.isoformat(), "events": out, "rows": out})


@app.get("/policy/ips")
def policy_ips():
    """投资策略"""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT value, updated_at FROM pams_policy WHERE key='ips_v1'")
            row = cur.fetchone()
    if not row:
        return json({"key": "ips_v1", "value": None})
    return json({"key": "ips_v1", "value": row["value"], "updated_at": _norm(row["updated_at"])})


@app.get("/watchlist")
def watchlist(limit: int = Query(200, ge=1, le=2000)):
    """观察列表"""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT market, symbol, name, thesis, status, created_at, updated_at
                FROM pams_watchlist
                ORDER BY updated_at DESC LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
    return json({'rows': [{k: _norm(v) for k, v in dict(r).items()} for r in rows]})


# ==================== 健康检查 ====================

@app.get("/health")
def health():
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return json({"ok": True, "version": "2.0.0"})
    except Exception as e:
        return json({"ok": False, "error": str(e)}, status_code=500)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
