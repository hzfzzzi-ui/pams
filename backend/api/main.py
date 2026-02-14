import os
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# 导入交易端点路由
from endpoints import transactions
# 导入账户映射模块
from account_mapper import standardize_account
# 导入类型映射模块
from type_mapper import get_major_category, get_minor_category, filter_by_page, get_page_categories

app = FastAPI(title="pams-api", version="0.4.0")

# 注册交易路由
app.include_router(transactions.router, prefix="/api")

# Allow the static dashboard (8080) to call the API (8090) from browsers
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"] ,
    allow_headers=["*"],
)


def json(data: Any, status_code: int = 200) -> JSONResponse:
    # Force UTF-8 JSON output for clients that mis-handle encoding (e.g. some consoles)
    return JSONResponse(content=data, status_code=status_code, media_type="application/json; charset=utf-8")


def get_conn():
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "postgres"),
        port=int(os.environ.get("PGPORT", "5432")),
        dbname=os.environ.get("PGDATABASE", "pams"),
        user=os.environ.get("PGUSER", "pams"),
        password=os.environ.get("PGPASSWORD", ""),
    )


@app.get("/health")
def health():
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("select 1")
                cur.fetchone()
        return json({"ok": True})
    except Exception as e:
        return json({"ok": False, "error": str(e)}, status_code=500)


def _norm(v: Any):
    # dates/times
    if hasattr(v, "isoformat"):
        return v.isoformat()
    # psycopg2 numerics
    try:
        from decimal import Decimal
        if isinstance(v, Decimal):
            # keep precision in JSON
            return str(v)
    except Exception:
        pass
    return v


def _filters(venue: Optional[str], account: Optional[str]):
    where = []
    params: List[Any] = []
    if venue:
        where.append("venue = %s")
        params.append(venue)
    if account:
        where.append("account = %s")
        params.append(account)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    return where_sql, params


def _get_base_currency(cur) -> str:
    cur.execute("SELECT value->>'base_currency' AS base_currency FROM pams_policy WHERE key='ips_v1'")
    r = cur.fetchone()
    return (r.get('base_currency') if r else None) or 'CNY'


@app.get("/holdings/latest")
def holdings_latest(
    venue: Optional[str] = None,
    account: Optional[str] = None,
    limit: int = Query(500, ge=1, le=5000),
):
    where_sql, params = _filters(venue, account)

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT max(as_of) AS as_of FROM pams_holdings_snapshot {where_sql}", params)
            row = cur.fetchone()
            as_of = row["as_of"] if row else None
            if as_of is None:
                return json({"as_of": None, "rows": []})

            params2 = params + [as_of, limit]
            cur.execute(
                f"""
                SELECT venue, account, type, symbol, name, currency,
                       market_value, holding_pnl, daily_pnl, as_of, created_at
                  FROM pams_holdings_snapshot
                  {where_sql} {'AND' if where_sql else 'WHERE'} as_of = %s
                 ORDER BY market_value DESC NULLS LAST
                 LIMIT %s
                """,
                params2,
            )
            rows = cur.fetchall()

    out_rows: List[Dict[str, Any]] = []
    for r in rows:
        row_dict = {k: _norm(v) for k, v in dict(r).items()}
        # 标准化账户名称
        if 'account' in row_dict:
            row_dict['account'] = standardize_account(row_dict['account'])
        # 添加大类和小类分类
        if 'type' in row_dict:
            row_dict['major_category'] = get_major_category(row_dict['type'])
            row_dict['minor_category'] = get_minor_category(row_dict['type'])
        out_rows.append(row_dict)

    return json({"as_of": as_of.isoformat(), "rows": out_rows})


@app.get("/holdings/as-of")
def holdings_as_of(
    date: str = Query(..., description="YYYY-MM-DD"),
    venue: Optional[str] = None,
    account: Optional[str] = None,
    limit: int = Query(500, ge=1, le=5000),
):
    where_sql, params = _filters(venue, account)

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            params2 = params + [date, limit]
            cur.execute(
                f"""
                SELECT venue, account, type, symbol, name, currency,
                       market_value, holding_pnl, daily_pnl, as_of, created_at
                  FROM pams_holdings_snapshot
                  {where_sql} {'AND' if where_sql else 'WHERE'} as_of = %s
                 ORDER BY market_value DESC NULLS LAST
                 LIMIT %s
                """,
                params2,
            )
            rows = cur.fetchall()

    out_rows = []
    for r in rows:
        row_dict = {k: _norm(v) for k, v in dict(r).items()}
        # 标准化账户名称
        if 'account' in row_dict:
            row_dict['account'] = standardize_account(row_dict['account'])
        # 添加大类和小类分类
        if 'type' in row_dict:
            row_dict['major_category'] = get_major_category(row_dict['type'])
            row_dict['minor_category'] = get_minor_category(row_dict['type'])
        out_rows.append(row_dict)

    return json({"as_of": date, "rows": out_rows})


@app.get("/summary/latest")
def summary_latest(
    venue: Optional[str] = None,
    account: Optional[str] = None,
):
    """Legacy summary by `type` (kept for compatibility)."""
    where_sql, params = _filters(venue, account)

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT max(as_of) AS as_of FROM pams_holdings_snapshot {where_sql}", params)
            row = cur.fetchone()
            as_of = row["as_of"] if row else None
            if as_of is None:
                return json({"as_of": None, "base_currency": "CNY", "totals": {}, "by_type": []})

            base_currency = _get_base_currency(cur)

            # totals converted to base currency
            cur.execute(
                f"""
                WITH h AS (
                  SELECT * FROM pams_holdings_snapshot
                  {where_sql} {'AND' if where_sql else 'WHERE'} as_of = %s
                ), fx AS (
                  SELECT quote_currency, rate
                    FROM pams_fx_rates_daily
                   WHERE as_of = %s AND base_currency = %s
                )
                SELECT
                  COALESCE(sum(h.market_value * fx.rate),0) AS market_value,
                  COALESCE(sum(COALESCE(h.holding_pnl,0) * fx.rate),0) AS holding_pnl,
                  COALESCE(sum(COALESCE(h.daily_pnl,0) * fx.rate),0) AS daily_pnl
                FROM h
                JOIN fx ON fx.quote_currency = h.currency
                """,
                params + [as_of, as_of, base_currency],
            )
            totals = cur.fetchone()

            # by type converted to base currency
            cur.execute(
                f"""
                WITH h AS (
                  SELECT * FROM pams_holdings_snapshot
                  {where_sql} {'AND' if where_sql else 'WHERE'} as_of = %s
                ), fx AS (
                  SELECT quote_currency, rate
                    FROM pams_fx_rates_daily
                   WHERE as_of = %s AND base_currency = %s
                )
                SELECT h.type,
                       COALESCE(sum(h.market_value * fx.rate),0) AS market_value,
                       COALESCE(sum(COALESCE(h.holding_pnl,0) * fx.rate),0)  AS holding_pnl,
                       COALESCE(sum(COALESCE(h.daily_pnl,0) * fx.rate),0)    AS daily_pnl
                  FROM h
                  JOIN fx ON fx.quote_currency = h.currency
                 GROUP BY h.type
                 ORDER BY market_value DESC
                """,
                params + [as_of, as_of, base_currency],
            )
            by_type = cur.fetchall()

    return json({
        "as_of": as_of.isoformat(),
        "base_currency": base_currency,
        "totals": {k: str(v) if v is not None else None for k, v in dict(totals).items()},
        "by_type": [{k: _norm(v) for k, v in dict(r).items()} for r in by_type],
    })


@app.get('/summary2/latest')
def summary2_latest(venue: Optional[str] = None, account: Optional[str] = None):
    """New summary by `asset_class` with children rows for dashboard tree views."""
    where_sql, params = _filters(venue, account)

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT max(as_of) AS as_of FROM pams_holdings_snapshot {where_sql}", params)
            row = cur.fetchone()
            as_of = row["as_of"] if row else None
            if as_of is None:
                return json({"as_of": None, "base_currency": "CNY", "totals": {}, "by_asset_class": []})

            base_currency = _get_base_currency(cur)

            # all holdings (latest)
            cur.execute(
                f"""
                WITH h AS (
                  SELECT * FROM pams_holdings_snapshot
                  {where_sql} {'AND' if where_sql else 'WHERE'} as_of = %s
                ), fx AS (
                  SELECT quote_currency, rate
                    FROM pams_fx_rates_daily
                   WHERE as_of = %s AND base_currency = %s
                ), mapped AS (
                  SELECT h.*, fx.rate,
                    (
                      SELECT m.asset_class
                        FROM pams_asset_class_map m
                       WHERE m.active = true
                         AND (m.venue   IS NULL OR m.venue   = h.venue)
                         AND (m.account IS NULL OR m.account = h.account)
                         AND (m.type    IS NULL OR m.type    = h.type)
                         AND (m.symbol  IS NULL OR m.symbol  = h.symbol)
                         AND (m.name    IS NULL OR h.name ILIKE '%%' || m.name || '%%')
                       ORDER BY m.priority ASC, m.id ASC
                       LIMIT 1
                    ) AS asset_class
                  FROM h
                  JOIN fx ON fx.quote_currency = h.currency
                )
                SELECT
                  COALESCE(asset_class,'UNMAPPED') AS asset_class,
                  COALESCE(sum(market_value * rate),0) AS market_value,
                  COALESCE(sum(COALESCE(holding_pnl,0) * rate),0) AS holding_pnl,
                  COALESCE(sum(COALESCE(daily_pnl,0) * rate),0) AS daily_pnl
                FROM mapped
                WHERE COALESCE(asset_class,'UNMAPPED') <> 'IGNORE'
                GROUP BY COALESCE(asset_class,'UNMAPPED')
                ORDER BY market_value DESC
                """,
                params + [as_of, as_of, base_currency],
            )
            by_asset_class = cur.fetchall()

            cur.execute(
                f"""
                WITH h AS (
                  SELECT * FROM pams_holdings_snapshot
                  {where_sql} {'AND' if where_sql else 'WHERE'} as_of = %s
                ), fx AS (
                  SELECT quote_currency, rate
                    FROM pams_fx_rates_daily
                   WHERE as_of = %s AND base_currency = %s
                ), mapped AS (
                  SELECT h.*, fx.rate,
                    (
                      SELECT m.asset_class
                        FROM pams_asset_class_map m
                       WHERE m.active = true
                         AND (m.venue   IS NULL OR m.venue   = h.venue)
                         AND (m.account IS NULL OR m.account = h.account)
                         AND (m.type    IS NULL OR m.type    = h.type)
                         AND (m.symbol  IS NULL OR m.symbol  = h.symbol)
                         AND (m.name    IS NULL OR h.name ILIKE '%%' || m.name || '%%')
                       ORDER BY m.priority ASC, m.id ASC
                       LIMIT 1
                    ) AS asset_class
                  FROM h
                  JOIN fx ON fx.quote_currency = h.currency
                )
                SELECT
                  COALESCE(asset_class,'UNMAPPED') AS asset_class,
                  venue, account, type, symbol, name, currency,
                  market_value * rate AS market_value,
                  COALESCE(holding_pnl,0) * rate AS holding_pnl,
                  COALESCE(daily_pnl,0) * rate AS daily_pnl
                FROM mapped
                WHERE COALESCE(asset_class,'UNMAPPED') <> 'IGNORE'
                ORDER BY COALESCE(asset_class,'UNMAPPED'), market_value DESC
                """,
                params + [as_of, as_of, base_currency],
            )
            children = cur.fetchall()

            totals = {
                'market_value': 0,
                'holding_pnl': 0,
                'daily_pnl': 0,
            }
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


@app.get("/assets/search")
def assets_search(q: str = Query(..., min_length=1, max_length=100), limit: int = Query(50, ge=1, le=500)):
    # MVP: search distinct names/symbols in holdings snapshots
    like = f"%{q}%"
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT DISTINCT COALESCE(symbol,'') AS symbol, name
                  FROM pams_holdings_snapshot
                 WHERE name ILIKE %s OR symbol ILIKE %s
                 ORDER BY name
                 LIMIT %s
                """,
                [like, like, limit],
            )
            rows = cur.fetchall()
    return json({"q": q, "rows": [{k: _norm(v) for k, v in dict(r).items()} for r in rows]})


@app.get("/policy/ips")
def policy_ips():
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT value, updated_at FROM pams_policy WHERE key='ips_v1'")
            row = cur.fetchone()
    if not row:
        return json({"key": "ips_v1", "value": None})
    return json({"key": "ips_v1", "value": row["value"], "updated_at": _norm(row["updated_at"])})


@app.get("/risk/latest")
def risk_latest(venue: Optional[str] = None, account: Optional[str] = None, limit: int = Query(100, ge=1, le=1000)):
    # For now risk events are computed globally on latest snapshot; filter support will come after we tag events by venue/account.
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT max(as_of) AS as_of FROM pams_holdings_snapshot")
            row = cur.fetchone()
            as_of = row["as_of"] if row else None
            if as_of is None:
                return json({"as_of": None, "events": []})
            cur.execute(
                """
                SELECT as_of, severity, code, title, details, created_at
                  FROM pams_risk_events
                 WHERE as_of = %s
                 ORDER BY
                   CASE severity WHEN 'crit' THEN 3 WHEN 'warn' THEN 2 ELSE 1 END DESC,
                   created_at DESC
                 LIMIT %s
                """,
                [as_of, limit],
            )
            rows = cur.fetchall()
    return json({"as_of": as_of.isoformat(), "events": [{k: _norm(v) for k, v in dict(r).items()} for r in rows]})


@app.get('/nav/recent')
def nav_recent(limit: int = Query(60, ge=1, le=365)):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT as_of, total_market_value, holdings_count, venues_count, completeness
                  FROM pams_nav_daily
                 ORDER BY as_of DESC
                 LIMIT %s
                """,
                [limit],
            )
            rows = cur.fetchall()
    rows = list(reversed(rows))
    return json({'rows': [{k:_norm(v) for k,v in dict(r).items()} for r in rows]})


@app.get('/nav/series')
def nav_series(limit: int = Query(90, ge=1, le=365), baseCurrency: str = "CNY"):
    """Return NAV series in format expected by dashboard (asOfDate, netWorth)."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Get latest snapshot to compute net worth
            cur.execute("SELECT max(as_of) AS as_of FROM pams_holdings_snapshot")
            row = cur.fetchone()
            latest_as_of = row["as_of"] if row else None

            # Fetch NAV series
            cur.execute(
                """
                SELECT as_of, total_market_value, holdings_count, venues_count, completeness
                  FROM pams_nav_daily
                 ORDER BY as_of DESC
                 LIMIT %s
                """,
                [limit],
            )
            rows = cur.fetchall()

    # Compute net worth: total market value - total liabilities
    # For now, use total_market_value as net worth (simplified)
    # A proper implementation would subtract liabilities for each date
    rows = list(reversed(rows))

    # Convert to dashboard format
    result = []
    for r in rows:
        result.append({
            "asOfDate": r["as_of"].isoformat() if hasattr(r["as_of"], "isoformat") else str(r["as_of"]),
            "netWorth": float(r["total_market_value"] or 0),
        })

    # Add latest point if not present (from holdings snapshot)
    if latest_as_of and (not result or result[-1]["asOfDate"] != latest_as_of.isoformat()):
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    WITH h AS (
                      SELECT * FROM pams_holdings_snapshot WHERE as_of = %s
                    ), fx AS (
                      SELECT quote_currency, rate
                        FROM pams_fx_rates_daily
                       WHERE as_of = %s AND base_currency = %s
                    )
                    SELECT COALESCE(sum(h.market_value * fx.rate), 0) AS total_mv
                      FROM h
                      JOIN fx ON fx.quote_currency = h.currency
                    """,
                    [latest_as_of, latest_as_of, baseCurrency],
                )
                row = cur.fetchone()
                total_mv = float(row["total_mv"] or 0) if row else 0.0
        result.append({
            "asOfDate": latest_as_of.isoformat(),
            "netWorth": total_mv,
        })

    return json({"rows": result})


@app.get('/cashflow/series')
def cashflow_series(limit: int = Query(30, ge=1, le=365), currency: str = "CNY"):
    """Return cashflow series from Firefly III (placeholder for now)."""
    # TODO: Integrate with Firefly III transaction API
    # For now, return empty series
    return json({"rows": []})


@app.get('/nav/drawdown')
def nav_drawdown(limit: int = Query(365, ge=2, le=5000)):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                WITH r AS (
                  SELECT as_of, total_market_value, completeness
                    FROM pams_nav_daily
                   ORDER BY as_of DESC
                   LIMIT %s
                ), x AS (
                  SELECT * FROM r ORDER BY as_of
                ), s AS (
                  SELECT as_of, total_market_value, completeness,
                         max(total_market_value) OVER (ORDER BY as_of) AS peak
                    FROM x
                )
                SELECT as_of, total_market_value, completeness, peak,
                       CASE WHEN peak=0 THEN 0 ELSE (total_market_value/peak - 1) END AS drawdown
                  FROM s
                 ORDER BY as_of;
                """,
                [limit],
            )
            rows = cur.fetchall()

    # max drawdown
    mdd = None
    for r in rows:
        try:
            dd = float(r['drawdown'])
            mdd = dd if mdd is None else min(mdd, dd)
        except Exception:
            continue

    return json({'max_drawdown': None if mdd is None else str(mdd), 'rows': [{k:_norm(v) for k,v in dict(r).items()} for r in rows]})


@app.get('/watchlist')
def watchlist(market: Optional[str] = Query(None, description='CN|US'), status: Optional[str] = None, limit: int = Query(200, ge=1, le=2000)):
    where = []
    params: List[Any] = []
    if market:
        where.append('market = %s')
        params.append(market)
    if status:
        where.append('status = %s')
        params.append(status)
    where_sql = ('WHERE ' + ' AND '.join(where)) if where else ''
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT market, symbol, name, thesis, status, created_at, updated_at
                  FROM pams_watchlist
                  {where_sql}
                 ORDER BY updated_at DESC
                 LIMIT %s
                """,
                params + [limit],
            )
            rows = cur.fetchall()
    return json({'rows': [{k:_norm(v) for k,v in dict(r).items()} for r in rows]})


@app.get('/assets/map')
def assets_map_list(venue: Optional[str] = None, market: Optional[str] = None, limit: int = Query(500, ge=1, le=5000)):
    where = []
    params: List[Any] = []
    if venue:
        where.append('venue = %s')
        params.append(venue)
    if market:
        where.append('market = %s')
        params.append(market)
    where_sql = ('WHERE ' + ' AND '.join(where)) if where else ''
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT venue, market, name, symbol, currency, updated_at
                  FROM pams_asset_map
                  {where_sql}
                 ORDER BY updated_at DESC
                 LIMIT %s
                """,
                params + [limit],
            )
            rows = cur.fetchall()
    return json({'rows': [{k:_norm(v) for k,v in dict(r).items()} for r in rows]})


@app.get('/news/latest')
def news_latest(market: Optional[str] = None, symbol: Optional[str] = None, limit: int = Query(50, ge=1, le=500)):
    where = []
    params: List[Any] = []
    if market:
        where.append('market = %s')
        params.append(market)
    if symbol:
        where.append('symbol = %s')
        params.append(symbol)
    where_sql = ('WHERE ' + ' AND '.join(where)) if where else ''

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT as_of, market, symbol, name, title, url, source, fetched_at
                  FROM pams_news_items
                  {where_sql}
                 ORDER BY fetched_at DESC
                 LIMIT %s
                """,
                params + [limit],
            )
            rows = cur.fetchall()
    return json({'rows': [{k:_norm(v) for k,v in dict(r).items()} for r in rows]})


@app.get('/delta/latest')
def delta_latest(limit: int = Query(20, ge=1, le=200), mode: str = Query('abs', pattern='^(abs|up|down)$')):
    """Asset value change vs previous snapshot day (base currency)."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT DISTINCT as_of FROM pams_holdings_snapshot ORDER BY as_of DESC LIMIT 2")
            days = cur.fetchall()
            if len(days) < 2:
                return json({'as_of': None, 'prev_as_of': None, 'base_currency': 'CNY', 'rows': []})
            as_of = days[0]['as_of']
            prev_as_of = days[1]['as_of']
            base_currency = _get_base_currency(cur)

            cur.execute(
                """
                WITH fx AS (
                  SELECT as_of, quote_currency, rate
                    FROM pams_fx_rates_daily
                   WHERE base_currency = %s
                     AND as_of IN (%s,%s)
                ), h AS (
                  SELECT * FROM pams_holdings_snapshot WHERE as_of IN (%s,%s)
                ), hh AS (
                  SELECT h.as_of, h.venue, h.account, h.type, h.symbol, h.name, h.currency,
                         (h.market_value * fx.rate) AS mv
                    FROM h
                    JOIN fx ON fx.as_of=h.as_of AND fx.quote_currency=h.currency
                ), agg AS (
                  SELECT venue, account, type, symbol, name,
                         sum(CASE WHEN as_of=%s THEN mv ELSE 0 END) AS mv_today,
                         sum(CASE WHEN as_of=%s THEN mv ELSE 0 END) AS mv_prev
                    FROM hh
                   GROUP BY venue, account, type, symbol, name
                )
                SELECT venue, account, type, symbol, name,
                       mv_today, mv_prev,
                       (mv_today - mv_prev) AS delta
                  FROM agg
                """,
                [base_currency, as_of, prev_as_of, as_of, prev_as_of, as_of, prev_as_of],
            )
            rows = cur.fetchall()

    # sort in python (fast enough for MVP)
    def key_abs(r):
        return abs(float(r['delta'] or 0))
    def key_up(r):
        return float(r['delta'] or 0)
    def key_down(r):
        return -float(r['delta'] or 0)

    if mode == 'up':
        rows.sort(key=key_up, reverse=True)
    elif mode == 'down':
        rows.sort(key=key_down, reverse=True)
    else:
        rows.sort(key=key_abs, reverse=True)

    out = []
    for r in rows[:limit]:
        out.append({k: _norm(v) for k, v in dict(r).items()})

    return json({'as_of': as_of.isoformat(), 'prev_as_of': prev_as_of.isoformat(), 'base_currency': base_currency, 'rows': out})


@app.post('/assets/map')
def assets_map_upsert(item: Dict[str, Any]):
    venue = item.get('venue')
    name = item.get('name')
    market = item.get('market')
    symbol = item.get('symbol')
    currency = item.get('currency')
    if not venue or not name or not market or not symbol:
        return json({'ok': False, 'error': 'venue,name,market,symbol required'}, status_code=400)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO pams_asset_map(venue,name,market,symbol,currency)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (venue,name,market)
                DO UPDATE SET symbol=EXCLUDED.symbol, currency=EXCLUDED.currency, updated_at=now();
                """,
                [venue, name, market, symbol, currency],
            )
    return json({'ok': True})


@app.post('/watchlist')
def watchlist_add(item: Dict[str, Any]):
    # MVP: minimal insert/upsert
    market = item.get('market')
    symbol = item.get('symbol')
    if not market or not symbol:
        return json({'ok': False, 'error': 'market and symbol required'}, status_code=400)
    name = item.get('name')
    thesis = item.get('thesis')
    status = item.get('status', 'candidate')
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO pams_watchlist(market, symbol, name, thesis, status)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (market, symbol)
                DO UPDATE SET name=EXCLUDED.name, thesis=EXCLUDED.thesis, status=EXCLUDED.status, updated_at=now()
                """,
                [market, symbol, name, thesis, status],
            )
    return json({'ok': True})


@app.post('/admin/refresh')
def admin_refresh():
    """Recompute derived tables and risk events from latest holdings.

    Safe, read-only with respect to broker accounts; only updates analytics tables.
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # nav_daily upsert from holdings snapshots
            cur.execute(
                """
                INSERT INTO pams_nav_daily(as_of,total_market_value)
                SELECT as_of, sum(market_value)
                  FROM pams_holdings_snapshot
                 GROUP BY as_of
                ON CONFLICT (as_of) DO UPDATE
                  SET total_market_value=EXCLUDED.total_market_value,
                      updated_at=now();
                """
            )

            # enrich nav stats
            cur.execute(
                """
                WITH stats AS (
                  SELECT as_of, count(*) AS holdings_count, count(distinct venue) AS venues_count
                    FROM pams_holdings_snapshot
                   GROUP BY as_of
                )
                UPDATE pams_nav_daily n
                   SET holdings_count=s.holdings_count,
                       venues_count=s.venues_count,
                       updated_at=now()
                  FROM stats s
                 WHERE n.as_of=s.as_of;
                """
            )

            # completeness heuristic
            cur.execute(
                """
                WITH x AS (
                  SELECT as_of, total_market_value, holdings_count, venues_count,
                         lag(total_market_value) OVER (ORDER BY as_of) AS prev_mv,
                         lag(holdings_count) OVER (ORDER BY as_of) AS prev_hc
                    FROM pams_nav_daily
                ), y AS (
                  SELECT *,
                         CASE
                           WHEN prev_mv IS NULL THEN 'unknown'
                           WHEN total_market_value < prev_mv*0.7 AND holdings_count < prev_hc*0.7 THEN 'incomplete'
                           ELSE 'ok'
                         END AS c
                    FROM x
                )
                UPDATE pams_nav_daily n
                   SET completeness=y.c,
                       updated_at=now()
                  FROM y
                 WHERE n.as_of=y.as_of;
                """
            )

            # latest as_of
            cur.execute("SELECT max(as_of) AS as_of FROM pams_holdings_snapshot")
            row = cur.fetchone()
            as_of = row['as_of'] if row else None
            if as_of is None:
                return json({'ok': True, 'note': 'no holdings snapshots found'})

            # wipe and rebuild risk events for latest day
            cur.execute("DELETE FROM pams_risk_events WHERE as_of=%s", [as_of])

            # totals
            cur.execute(
                """
                SELECT sum(market_value) AS total_mv,
                       count(distinct venue) AS venues_count,
                       count(*) AS holdings_count
                  FROM pams_holdings_snapshot
                 WHERE as_of=%s
                """,
                [as_of],
            )
            t = cur.fetchone()
            total_mv = t['total_mv']
            venues_count = int(t['venues_count'] or 0)
            holdings_count = int(t['holdings_count'] or 0)

            if not total_mv or float(total_mv) <= 0:
                return json({'ok': True, 'note': 'total market value is zero'})

            # data scope
            if venues_count < 2:
                cur.execute(
                    """
                    INSERT INTO pams_risk_events(as_of,severity,code,title,details)
                    VALUES (%s,'info','DATA_SCOPE_LIMITED','快照范围有限：当前仅覆盖部分平台',%s::jsonb)
                    """,
                    [as_of, '{"venues_count": %s, "holdings_count": %s}' % (venues_count, holdings_count)],
                )

            # concentration by name (exclude cash)
            cur.execute(
                """
                WITH base AS (
                  SELECT * FROM pams_holdings_snapshot WHERE as_of=%s AND type <> 'cash'
                ), total AS (
                  SELECT sum(market_value) AS total_mv FROM base
                ), by_name AS (
                  SELECT name, symbol, type, venue, account, sum(market_value) AS mv
                    FROM base
                   GROUP BY 1,2,3,4,5
                )
                SELECT b.*, (b.mv / NULLIF(t.total_mv,0)) AS w, t.total_mv AS base_total
                  FROM by_name b CROSS JOIN total t;
                """,
                [as_of],
            )
            for r in cur.fetchall():
                w = float(r['w'] or 0)
                if w < 0.30:
                    continue
                sev = 'crit' if w >= 0.50 else 'warn'
                cur.execute(
                    """
                    INSERT INTO pams_risk_events(as_of,severity,code,title,details)
                    VALUES (%s,%s,'CONCENTRATION_SINGLE','单一标的集中度过高（剔除现金）',%s::jsonb)
                    """,
                    [as_of, sev, psycopg2.extras.Json({
                        'name': r['name'],
                        'symbol': r['symbol'],
                        'type': r['type'],
                        'venue': r['venue'],
                        'account': r['account'],
                        'market_value': str(r['mv']),
                        'weight': round(w, 4),
                        'base_total_ex_cash': str(r['base_total']),
                    })],
                )

            # concentration by type (exclude cash; placeholder for sector)
            cur.execute(
                """
                WITH base AS (
                  SELECT * FROM pams_holdings_snapshot WHERE as_of=%s AND type <> 'cash'
                ), total AS (
                  SELECT sum(market_value) AS total_mv FROM base
                ), by_type AS (
                  SELECT type, sum(market_value) AS mv
                    FROM base
                   GROUP BY 1
                )
                SELECT b.*, (b.mv / NULLIF(t.total_mv,0)) AS w, t.total_mv AS base_total
                  FROM by_type b CROSS JOIN total t;
                """,
                [as_of],
            )
            for r in cur.fetchall():
                w = float(r['w'] or 0)
                if w < 0.40:
                    continue
                sev = 'crit' if w >= 0.60 else 'warn'
                cur.execute(
                    """
                    INSERT INTO pams_risk_events(as_of,severity,code,title,details)
                    VALUES (%s,%s,'CONCENTRATION_BUCKET','单一类别集中度偏高（剔除现金；type 代理行业）',%s::jsonb)
                    """,
                    [as_of, sev, psycopg2.extras.Json({
                        'type': r['type'],
                        'market_value': str(r['mv']),
                        'weight': round(w, 4),
                        'base_total_ex_cash': str(r['base_total']),
                    })],
                )

    return json({'ok': True, 'as_of': as_of.isoformat()})


@app.get("/holdings/by-category/{category}")
def holdings_by_category(
    category: str,
    limit: int = Query(500, ge=1, le=5000),
):
    """
    获取指定大类的最新持仓

    用于子页面数据展示
    """
    # 验证category
    valid_categories = get_page_categories()
    if category not in valid_categories:
        return json({"error": f"Invalid category. Must be one of: {valid_categories}"}, status_code=400)

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # 获取最新日期
            cur.execute("SELECT max(as_of) AS as_of FROM pams_holdings_snapshot")
            row = cur.fetchone()
            as_of = row["as_of"] if row else None
            if as_of is None:
                return json({"as_of": None, "rows": []})

            # 获取所有持仓
            cur.execute(
                """
                SELECT venue, account, type, symbol, name, currency,
                       market_value, holding_pnl, daily_pnl, as_of, created_at
                  FROM pams_holdings_snapshot
                 WHERE as_of = %s
                 ORDER BY market_value DESC NULLS LAST
                 LIMIT %s
                """,
                [as_of, limit * 2],  # 多取一些以便筛选
            )
            rows = cur.fetchall()

    # 筛选并添加分类字段
    out_rows: List[Dict[str, Any]] = []
    for r in rows:
        row_dict = {k: _norm(v) for k, v in dict(r).items()}

        # 添加分类
        if 'type' in row_dict:
            major = get_major_category(row_dict['type'])
            if major == category:  # 只保留匹配的大类
                row_dict['major_category'] = major
                row_dict['minor_category'] = get_minor_category(row_dict['type'])

                # 标准化账户名称
                if 'account' in row_dict:
                    row_dict['account'] = standardize_account(row_dict['account'])

                out_rows.append(row_dict)

                if len(out_rows) >= limit:
                    break

    return json({"as_of": as_of.isoformat(), "category": category, "rows": out_rows})


@app.get("/holdings/history/{category}")
def holdings_history(
    category: str,
    days: int = Query(90, ge=7, le=365),
    interval: str = Query("daily"),
):
    """
    获取指定大类的历史市值序列

    用于子页面折线图
    """
    # 验证category
    valid_categories = get_page_categories()
    if category not in valid_categories:
        return json({"error": f"Invalid category. Must be one of: {valid_categories}"}, status_code=400)

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # 获取历史数据
            if interval == "weekly":
                # 按周聚合（只取周一的数据）
                cur.execute(
                    """
                    WITH weekly_data AS (
                        SELECT DISTINCT ON (DATE_TRUNC('week', as_of))
                               as_of,
                               type,
                               market_value
                          FROM pams_holdings_snapshot
                         WHERE as_of >= CURRENT_DATE - INTERVAL '%s days'
                         ORDER BY DATE_TRUNC('week', as_of), as_of DESC
                    )
                    SELECT as_of, type, SUM(market_value) AS total_value
                      FROM weekly_data
                     GROUP BY as_of, type
                     ORDER BY as_of ASC
                    """,
                    [days],
                )
            else:
                # 按日
                cur.execute(
                    """
                    SELECT as_of, type, SUM(market_value) AS total_value
                      FROM pams_holdings_snapshot
                     WHERE as_of >= CURRENT_DATE - INTERVAL '%s days'
                     GROUP BY as_of, type
                     ORDER BY as_of ASC
                    """,
                    [days],
                )

            rows = cur.fetchall()

    # 按日期聚合指定大类的市值
    date_values = {}
    for r in rows:
        type_value = r['type']
        major = get_major_category(type_value)

        if major == category:
            date_str = r['as_of'].isoformat()
            total_value = float(r['total_value'] or 0)

            if date_str not in date_values:
                date_values[date_str] = 0
            date_values[date_str] += total_value

    # 转换为有序列表
    series = [
        {"date": date, "value": value}
        for date, value in sorted(date_values.items())
    ]

    return json({
        "category": category,
        "interval": interval,
        "days": days,
        "series": series
    })


# Import and register additional endpoints
from endpoints_overview import register_overview
register_overview(app)
