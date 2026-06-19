"""
PAMS API V2 - 基于重构后的数据库结构
- 使用account_code替代venue+account
- 使用major_category/minor_category替代asset_class_map
- 支持多币种汇率换算
"""
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
from datetime import date as Date, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras
import yaml
from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# 导入端点路由
from endpoints import transactions
from endpoints import tasks
from endpoints import prices
from endpoints import holdings_trade
from endpoints import notifications
from endpoints import backtest_v2
from endpoints import live_signals
from endpoints import risk
from endpoints import ibkr
from endpoints import investment_transactions
from endpoints import ledger
from endpoints import bank_reconcile
from endpoints import accounts as accounts_endpoint
from endpoints import portfolio_state as portfolio_state_endpoint
from endpoints import sync_center
from endpoints import analysis
from strategy.router import router as strategy_router
from backtest.router import router as backtest_router
from datahub.router import router as datahub_router
from services.accounts_view import (
    build_account_detail_payload as _build_account_detail_payload,
    build_accounts_payload as _build_accounts_payload,
)
from services.cashflow_view import build_cashflow_series_payload as _build_cashflow_series_payload
from services.fx_rates import build_latest_fx_rates_payload as _build_latest_fx_rates_payload
from services.holdings_view import (
    build_api_holdings_latest_payload as _build_api_holdings_latest_payload,
    build_holdings_by_category_payload as _build_holdings_by_category_payload,
    build_holdings_history_payload as _build_holdings_history_payload,
    build_holdings_latest_payload as _build_holdings_latest_payload,
    latest_complete_snapshot_date as _latest_complete_snapshot_date,
)
from services.holding_return_snapshot import (
    build_investment_return_series_payload_from_snapshot as _build_investment_return_series_payload_from_snapshot,
    build_section_return_series_payload_from_snapshot as _build_section_return_series_payload_from_snapshot,
    upsert_investment_return_series_snapshot as _upsert_investment_return_series_snapshot,
)
from services.investment_returns import (
    apply_reported_display_anchor as _apply_reported_display_anchor,
    build_account_performance_payload as _build_account_performance_payload,
    build_investment_return_series_payload as _build_investment_return_series_payload,
    build_section_return_series_payload as _build_section_return_series_payload,
    section_return_summary as _section_return_summary,
)
from services.investment_view_date import (
    clamp_to_latest_data_date as _clamp_to_latest_data_date,
    daily_pnl_window_date as _daily_pnl_window_date,
    market_data_candidate_date as _market_data_candidate_date,
)
from services.portfolio_summary import (
    apply_return_attribution_pnl_to_summary as _apply_return_attribution_pnl_to_summary,
    apply_published_valuation_state_to_summary as _apply_published_valuation_state_to_summary,
    build_holdings_by_account_payload as _build_holdings_by_account_payload,
    build_holdings_summary_payload as _build_holdings_summary_payload,
    build_summary2_from_holdings_latest_payload as _build_summary2_from_holdings_latest_payload,
)
from services.return_attribution import build_return_attribution_payload as _build_return_attribution_payload
from services.portfolio_state import (
    _load_accounts_config as _load_portfolio_accounts_config,
    load_account_balances as _load_account_balances,
    load_investment_positions as _load_investment_positions,
)
from services.portfolio_valuation_state import (
    apply_published_valuation_state_to_return_series_payload as _apply_published_valuation_state_to_return_series_payload,
    is_published_valuation_fresh_for_date as _is_published_valuation_fresh_for_date,
    latest_published_portfolio_valuation_state as _latest_published_portfolio_valuation_state,
    published_nav_bucket_from_row as _published_nav_bucket_from_row,
)
from services.research_view import (
    build_policy_ips_payload as _build_policy_ips_payload,
    build_watchlist_payload as _build_watchlist_payload,
)
from services.reference_data import build_categories_payload as _build_categories_payload
from services.risk_state import build_risk_latest_payload as _build_risk_latest_payload
from services.valuation_nav import (
    ANCHOR_NAV_DATE,
    account_anchor_amount as _account_anchor_amount,
    account_anchor_value_cny as _account_anchor_value_cny,
    add_nav_amount as _add_nav_amount,
    build_nav_drawdown_payload as _build_nav_drawdown_payload,
    build_nav_series_payload as _build_nav_series_payload,
    build_nav_weekly_payload as _build_nav_weekly_payload,
    build_weekly_nav_rows as _build_weekly_nav_rows,
    current_nav_bucket_from_parts as _current_nav_bucket_from_parts,
    format_weekly_nav_payload as _format_weekly_nav_payload,
    fx_rate_at as _fx_rate_at,
    load_fx_timelines as _load_fx_timelines,
    manual_effective_date as _manual_effective_date,
    scale_series_to_terminal_anchor as _scale_series_to_terminal_anchor,
    select_weekly_nav_accounts as _select_weekly_nav_accounts,
    series_dates as _series_dates,
)
from src.core.config import load_risk_limits

app = FastAPI(title="pams-api-v2", version="2.0.0")

# 注册路由
app.include_router(transactions.router, prefix="/api")
app.include_router(tasks.router, prefix="/api")
app.include_router(prices.router, prefix="/api")
app.include_router(holdings_trade.router, prefix="/api")
app.include_router(notifications.router, prefix="/api")
app.include_router(live_signals.router, prefix="/api")
app.include_router(risk.router, prefix="/api")
app.include_router(ibkr.router, prefix="/api")
app.include_router(investment_transactions.router, prefix="/api")
app.include_router(ledger.router, prefix="/api")
app.include_router(bank_reconcile.router, prefix="/api")
app.include_router(accounts_endpoint.router, prefix="/api")
app.include_router(portfolio_state_endpoint.router, prefix="/api")
app.include_router(sync_center.router, prefix="/api")
app.include_router(analysis.router, prefix="/api")
app.include_router(backtest_v2.router)
app.include_router(strategy_router, prefix="/api")
app.include_router(backtest_router, prefix="/api")
app.include_router(datahub_router, prefix="/api")

def _allowed_web_origins() -> list[str]:
    raw = os.environ.get("PAMS_ALLOWED_ORIGINS", "")
    configured = [item.strip().rstrip("/") for item in raw.split(",") if item.strip()]
    defaults = [
        "http://pams.local",
        "http://localhost",
        "http://localhost:8080",
        "http://localhost:8090",
        "http://127.0.0.1",
        "http://127.0.0.1:8080",
        "http://127.0.0.1:8090",
    ]
    return sorted(set(configured or defaults))


ALLOWED_WEB_ORIGINS = _allowed_web_origins()
LOCAL_WRITE_HOSTS = {"pams.local", "localhost", "127.0.0.1", "::1"}
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _is_allowed_write_origin(value: str | None) -> bool:
    if not value:
        return True
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    origin = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    return host in LOCAL_WRITE_HOSTS or origin in ALLOWED_WEB_ORIGINS


@app.middleware("http")
async def local_write_origin_guard(request: Request, call_next):
    if request.method.upper() in MUTATING_METHODS:
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        if not _is_allowed_write_origin(origin) or (not origin and not _is_allowed_write_origin(referer)):
            return JSONResponse({"detail": "write request origin is not allowed"}, status_code=403)
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_WEB_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

ACCOUNTS_CONFIG_PATH = Path(os.environ.get("PAMS_ACCOUNTS_CONFIG", "/app/config/accounts.yaml"))
LOCAL_ACCOUNTS_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "accounts.yaml"
REPORTED_RETURNS_CONFIG_PATH = Path(os.environ.get("PAMS_INVESTMENT_RETURNS_CONFIG", "/app/config/investment_returns.yaml"))
LOCAL_REPORTED_RETURNS_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "investment_returns.yaml"

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


def _view_as_of() -> Date:
    """Return the portfolio view date in the user's local operating timezone."""
    tz_name = os.environ.get("PAMS_TIMEZONE", os.environ.get("TZ", "Asia/Shanghai"))
    try:
        return datetime.now(ZoneInfo(tz_name)).date()
    except Exception:
        return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def _daily_pnl_view_as_of() -> Date:
    """Return the portfolio daily-PnL view date, rolling at 07:00 Beijing time."""
    tz_name = os.environ.get("PAMS_TIMEZONE", os.environ.get("TZ", "Asia/Shanghai"))
    return _daily_pnl_window_date(tz_name=tz_name)


def _holdings_market_view_as_of(cur) -> Date:
    """Return the holdings display date for public-market position snapshots."""
    tz_name = os.environ.get("PAMS_TIMEZONE", os.environ.get("TZ", "Asia/Shanghai"))
    candidate = _market_data_candidate_date(tz_name=tz_name)
    cur.execute(
        """
        SELECT MAX(as_of) AS latest_cutoff_data_date
        FROM pams_holdings_snapshot
        WHERE as_of <= %s
          AND major_category = ANY(%s::text[])
        """,
        (candidate, ["A股", "美股", "贵金属"]),
    )
    row = cur.fetchone() or {}
    latest = row.get("latest_cutoff_data_date")
    complete_latest = _latest_complete_snapshot_date(
        cur,
        view_as_of=candidate,
        categories=["\u0041\u80a1", "\u7f8e\u80a1", "\u8d35\u91d1\u5c5e"],
    )
    if complete_latest is not None:
        latest = complete_latest
    if isinstance(latest, datetime):
        latest = latest.date()
    elif latest is not None and not isinstance(latest, Date) and hasattr(latest, "date"):
        latest = latest.date()
    return _clamp_to_latest_data_date(candidate, latest)


def _investment_return_view_as_of(cur) -> Date:
    """Return the latest effective date for investment return curves.

    Return charts use the same daily-PnL label as the holdings page.  US close
    data arrives after Beijing midnight, so using the 15:05 market-data date
    would split APAC and US returns across different visible dates.
    """
    tz_name = os.environ.get("PAMS_TIMEZONE", os.environ.get("TZ", "Asia/Shanghai"))
    candidate = _daily_pnl_window_date(tz_name=tz_name)
    cur.execute(
        """
        SELECT MAX(as_of) AS latest_cutoff_data_date
        FROM pams_holdings_snapshot
        WHERE as_of <= %s
          AND major_category = ANY(%s::text[])
        """,
        (candidate, ["A股", "美股", "贵金属", "ESOP"]),
    )
    row = cur.fetchone() or {}
    cutoff_latest = row.get("latest_cutoff_data_date")
    complete_latest = _latest_complete_snapshot_date(
        cur,
        view_as_of=candidate,
        categories=["\u0041\u80a1", "\u7f8e\u80a1", "\u8d35\u91d1\u5c5e", "ESOP"],
    )
    if complete_latest is not None:
        cutoff_latest = complete_latest
    if isinstance(cutoff_latest, datetime):
        cutoff_latest = cutoff_latest.date()
    elif cutoff_latest is not None and not isinstance(cutoff_latest, Date) and hasattr(cutoff_latest, "date"):
        cutoff_latest = cutoff_latest.date()
    return _clamp_to_latest_data_date(candidate, cutoff_latest)


def _investment_today_label_date() -> Date:
    """Return the active daily PnL bucket label, rolling at 07:00 Beijing time."""
    tz_name = os.environ.get("PAMS_TIMEZONE", os.environ.get("TZ", "Asia/Shanghai"))
    return _daily_pnl_window_date(tz_name=tz_name)


def _risk_data_cfg() -> dict[str, Any]:
    try:
        return load_risk_limits().get("data", {}) or {}
    except Exception:
        return {}


def _accounts_cfg() -> dict[str, Any]:
    path = ACCOUNTS_CONFIG_PATH if ACCOUNTS_CONFIG_PATH.exists() else LOCAL_ACCOUNTS_CONFIG_PATH
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _reported_returns_cfg() -> dict[str, Any]:
    path = REPORTED_RETURNS_CONFIG_PATH if REPORTED_RETURNS_CONFIG_PATH.exists() else LOCAL_REPORTED_RETURNS_CONFIG_PATH
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _excluded_holding_accounts() -> set[str]:
    cfg = _accounts_cfg()
    values = cfg.get("excluded_accounts") or []
    defaults = cfg.get("defaults") or {}
    values.extend(defaults.get("excluded_accounts") or [])
    return {str(v).strip() for v in values if str(v).strip()}


def _account_owner_ratios() -> dict[str, Decimal]:
    accounts_cfg = _accounts_cfg()
    defaults = accounts_cfg.get("defaults") or {}
    default_owner_ratio = Decimal(str(defaults.get("owner_ratio", _risk_data_cfg().get("default_owner_ratio", 1.0))))
    ratios = {
        str(code): Decimal(str((cfg or {}).get("owner_ratio", default_owner_ratio)))
        for code, cfg in (accounts_cfg.get("accounts") or {}).items()
        if (cfg or {}).get("owner_ratio") is not None
    }
    data_cfg = _risk_data_cfg()
    for key, value in (data_cfg.get("account_owner_ratios", {}) or {}).items():
        ratios.setdefault(str(key), Decimal(str(value)))
    return ratios


def _default_owner_ratio() -> Decimal:
    accounts_cfg = _accounts_cfg()
    defaults = accounts_cfg.get("defaults") or {}
    return Decimal(str(defaults.get("owner_ratio", _risk_data_cfg().get("default_owner_ratio", 1.0))))


def _account_owner_ratio(account_code: str) -> Decimal:
    return _account_owner_ratios().get(str(account_code), _default_owner_ratio())


def _account_initial_capital_cny(account_code: str) -> Decimal | None:
    account_key = str(account_code)
    account_cfg = (_accounts_cfg().get("accounts") or {}).get(account_key) or {}
    if account_cfg.get("capital_base") not in (None, ""):
        return Decimal(str(account_cfg["capital_base"]))
    if account_cfg.get("personal_capital") not in (None, ""):
        owner_ratio = _account_owner_ratio(account_key)
        personal_capital = Decimal(str(account_cfg["personal_capital"]))
        return personal_capital / owner_ratio if owner_ratio else personal_capital
    data_cfg = _risk_data_cfg()
    values = data_cfg.get("account_initial_capital_cny", {}) or {}
    if account_key not in values:
        return None
    return Decimal(str(values[account_key]))









def _owner_ratio_case_sql(alias: str = "h") -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    for account_code, ratio in _account_owner_ratios().items():
        clauses.append(f"WHEN {alias}.account_code = %s THEN %s")
        params.extend([account_code, float(ratio)])
    if not clauses:
        return "%s", [float(_default_owner_ratio())]
    return f"(CASE {' '.join(clauses)} ELSE %s END)", [*params, float(_default_owner_ratio())]



# ==================== 账户管理 ====================

@app.get("/api/accounts")
def get_accounts(active: bool = Query(True)):
    """获取账户列表"""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            return json(_build_accounts_payload(cur, active=active))

@app.get("/api/accounts/{account_code}")
def get_account_detail(account_code: str):
    """获取单个账户详情"""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            account_data = _build_account_detail_payload(cur, account_code=account_code)
            if account_data is None:
                raise HTTPException(status_code=404, detail=f"账户 {account_code} 不存在")
            return json(account_data)

# ==================== 资产分类 ====================

@app.get("/api/categories")
def get_categories():
    """获取所有资产分类"""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            return json(_build_categories_payload(cur))

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
            return json(
                _build_api_holdings_latest_payload(
                    cur,
                    account_code=account_code,
                    major_category=major_category,
                    limit=limit,
                    view_as_of=_holdings_market_view_as_of(cur),
                )
            )

@app.get("/api/holdings/summary")
def holdings_summary(base_currency: str = Query("CNY")):
    """持仓汇总统计（按大类分组，含汇率换算）"""
    owner_ratio_sql, owner_ratio_params = _owner_ratio_case_sql("h")
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            return json(
                _build_holdings_summary_payload(
                    cur,
                    base_currency=base_currency,
                    view_as_of=_holdings_market_view_as_of(cur),
                    owner_ratio_sql=owner_ratio_sql,
                    owner_ratio_params=owner_ratio_params,
                )
            )

@app.get("/api/holdings/by-account")
def holdings_by_account(base_currency: str = Query("CNY")):
    """按账户汇总持仓（含汇率换算）"""
    owner_ratio_sql, owner_ratio_params = _owner_ratio_case_sql("h")
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            return json(
                _build_holdings_by_account_payload(
                    cur,
                    base_currency=base_currency,
                    view_as_of=_holdings_market_view_as_of(cur),
                    owner_ratio_sql=owner_ratio_sql,
                    owner_ratio_params=owner_ratio_params,
                )
            )

# ==================== 汇率查询 ====================

@app.get("/api/fx-rates/latest")
def get_latest_fx_rates(base_currency: str = Query("CNY")):
    """获取最新汇率"""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            return json(_build_latest_fx_rates_payload(cur, base_currency=base_currency))

@app.post("/api/fx-rates/refresh")
def refresh_fx_rates():
    """从公开API获取最新汇率并存储双向汇率"""
    import urllib.request
    import json as json_mod
    from datetime import date as d_date

    today = d_date.today()
    currencies = ['CNY', 'USD', 'MXN', 'HKD', 'KRW']
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
            base_currency = _get_base_currency(cur)
            holdings_payload = _build_holdings_latest_payload(
                cur,
                limit=5000,
                include_closed=False,
                view_as_of=_holdings_market_view_as_of(cur),
                excluded_accounts=_excluded_holding_accounts(),
                owner_ratios=_account_owner_ratios(),
                default_owner_ratio=_default_owner_ratio(),
                daily_pnl_start_as_of=_daily_pnl_view_as_of(),
            )
            account_balances = _load_account_balances(
                cur,
                base_currency,
                _load_portfolio_accounts_config(),
            )
            summary_payload = _build_summary2_from_holdings_latest_payload(
                holdings_payload,
                base_currency=base_currency,
                account_balances=account_balances,
            )
            attribution_payload = _build_return_attribution_payload(cur, base_currency=base_currency)
            summary_payload = _apply_return_attribution_pnl_to_summary(summary_payload, attribution_payload)
            valuation_state = _latest_published_portfolio_valuation_state(
                cur,
                base_currency=base_currency,
            )
            return json(_apply_published_valuation_state_to_summary(summary_payload, valuation_state))

@app.get("/holdings/latest")
def holdings_latest_compat(
    limit: int = Query(500, ge=1, le=5000),
    include_closed: bool = Query(False, alias="includeClosed"),
):
    """持仓页（前端兼容格式：account, type, name, currency, market_value, holding_pnl, daily_pnl）"""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            return json(
                _build_holdings_latest_payload(
                    cur,
                    limit=limit,
                    include_closed=include_closed,
                    view_as_of=_holdings_market_view_as_of(cur),
                    excluded_accounts=_excluded_holding_accounts(),
                    owner_ratios=_account_owner_ratios(),
                    default_owner_ratio=_default_owner_ratio(),
                    daily_pnl_start_as_of=_daily_pnl_view_as_of(),
                )
            )


@app.get("/holdings/by-category/{category}")
def holdings_by_category(
    category: str,
    limit: int = Query(500, ge=1, le=5000),
    minor_category: Optional[str] = None,
    include_closed: bool = Query(False, alias="includeClosed"),
    include_closed_legacy: Optional[bool] = Query(None, alias="include_closed"),
):
    """Return holdings by major category."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            return json(
                _build_holdings_by_category_payload(
                    cur,
                    category=category,
                    limit=limit,
                    minor_category=minor_category,
                    include_closed=include_closed if include_closed_legacy is None else include_closed_legacy,
                    view_as_of=_holdings_market_view_as_of(cur),
                    excluded_accounts=_excluded_holding_accounts(),
                    owner_ratios=_account_owner_ratios(),
                    default_owner_ratio=_default_owner_ratio(),
                    daily_pnl_start_as_of=_daily_pnl_view_as_of(),
                )
            )

@app.get("/api/account-performance/{account_code}")
def account_performance(account_code: str):
    """Return account-level personal performance using configured owner ratio and initial capital."""
    owner_ratio = _account_owner_ratio(account_code)
    initial_capital = _account_initial_capital_cny(account_code)
    overview_payload = accounts_endpoint._overview_payload()
    account_overview = next(
        (item for item in overview_payload.get("accounts", []) if str(item.get("account_code") or "") == account_code),
        {},
    )
    primary_balance = account_overview.get("primary_balance") or {}
    gross_balance = primary_balance.get("gross_amount")
    net_balance = primary_balance.get("amount")
    balance_as_of = primary_balance.get("as_of")
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            payload = _build_account_performance_payload(
                cur,
                account_code=account_code,
                owner_ratio=owner_ratio,
                initial_capital=initial_capital,
                net_liquidation_gross=Decimal(str(gross_balance)) if gross_balance is not None else None,
                net_liquidation=Decimal(str(net_balance)) if net_balance is not None else None,
                net_liquidation_as_of=balance_as_of,
                daily_pnl_label_date=_daily_pnl_view_as_of(),
            )
            if payload is None:
                raise HTTPException(status_code=404, detail=f"Account {account_code} has no holdings snapshot")
            return json(payload)
@app.get("/holdings/history/{category}")
def holdings_history_by_category(
    category: str,
    days: int = Query(90, ge=1, le=365),
    interval: str = Query("daily"),
    minor_category: Optional[str] = None,
    baseCurrency: str = "CNY",
):
    """Return holdings history by category."""
    owner_ratio_sql, owner_ratio_params = _owner_ratio_case_sql("h")
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            return json(
                _build_holdings_history_payload(
                    cur,
                    category=category,
                    days=days,
                    interval=interval,
                    minor_category=minor_category,
                    view_as_of=_view_as_of(),
                    tonghuashun_owner_ratio=_account_owner_ratio("tonghuashun"),
                    owner_ratio_sql=owner_ratio_sql,
                    owner_ratio_params=owner_ratio_params,
                    excluded_accounts=sorted(_excluded_holding_accounts()),
                    base_currency=baseCurrency,
                )
            )


@app.get("/nav/series")
def nav_series(limit: int = Query(90, ge=1, le=365), baseCurrency: str = "CNY"):
    """NAV时间序列"""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            return json(_build_nav_series_payload(cur, limit=limit, base_currency=baseCurrency))






















@app.get("/investment/section-return-series")
def investment_section_return_series(
    section: str = Query(..., pattern="^(a_stock|a_fund|us_stock|precious_metal)$"),
    start: str = Query("2026-01-01"),
    interval: str = Query("daily", pattern="^(daily|weekly)$"),
    baseCurrency: str = "CNY",
    refreshSnapshot: bool = Query(False),
):
    """Return section return rates after removing deposits, subscriptions, and redemptions."""
    try:
        start_date = Date.fromisoformat(start)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="start must be YYYY-MM-DD") from exc

    payload = accounts_endpoint._overview_payload()
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            view_as_of = _investment_return_view_as_of(cur)
            if not refreshSnapshot:
                snapshot_payload = _build_section_return_series_payload_from_snapshot(
                    cur,
                    section=section,
                    start_date=start_date,
                    interval=interval,
                    base_currency=baseCurrency,
                    view_as_of=view_as_of,
                    today_label_date=_investment_today_label_date(),
                    require_data_completeness=True,
                )
                if snapshot_payload is not None:
                    return json(snapshot_payload)
            payload = _build_section_return_series_payload(
                cur,
                section=section,
                start_date=start_date,
                interval=interval,
                base_currency=baseCurrency,
                view_as_of=view_as_of,
                reported_returns_cfg=_reported_returns_cfg(),
                overview_payload=payload,
                owner_ratio_case_sql=_owner_ratio_case_sql,
                account_owner_ratio=_account_owner_ratio,
                excluded_holding_accounts=_excluded_holding_accounts(),
                today_label_date=_investment_today_label_date(),
            )
            if refreshSnapshot:
                try:
                    _upsert_investment_return_series_snapshot(
                        cur,
                        payload=payload,
                        base_currency=baseCurrency,
                        section=section,
                    )
                except Exception:
                    conn.rollback()
            return json(payload)


@app.get("/nav/weekly")
def nav_weekly(
    start: str = Query("2026-01-01"),
    baseCurrency: str = "CNY",
    latest: bool = Query(False),
    interval: str = Query("weekly", pattern="^(daily|weekly)$"),
):
    """Return weekly NAV using account anchors and back-cast statement flows."""
    try:
        start_date = Date.fromisoformat(start)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="start must be YYYY-MM-DD") from exc

    payload = accounts_endpoint._overview_payload()

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            current_view_as_of = _investment_return_view_as_of(cur) if latest else ANCHOR_NAV_DATE
            published_state = (
                _latest_published_portfolio_valuation_state(cur, base_currency=baseCurrency)
                if latest
                else None
            )
            published_state_fresh = bool(
                published_state
                and _is_published_valuation_fresh_for_date(published_state, current_view_as_of)
            )
            terminal_date = (
                published_state["valuation_date"]
                if published_state_fresh and published_state and published_state.get("valuation_date")
                else current_view_as_of
            )
            terminal_nav_bucket = None
            if published_state_fresh and published_state:
                terminal_nav_bucket = _published_nav_bucket_from_row(published_state)
            elif latest:
                account_cfg = _load_portfolio_accounts_config()
                account_balances = _load_account_balances(cur, baseCurrency, account_cfg)
                investment_positions = _load_investment_positions(
                    cur,
                    baseCurrency,
                    account_cfg,
                    account_balances=account_balances,
                )
                terminal_nav_bucket = _current_nav_bucket_from_parts(
                    account_balances=account_balances,
                    investment_positions=investment_positions,
                )
            return json(
                _build_nav_weekly_payload(
                    cur,
                    start_date=start_date,
                    base_currency=baseCurrency,
                    overview_payload=payload,
                    owner_ratio_case_sql=_owner_ratio_case_sql,
                    account_owner_ratio=_account_owner_ratio,
                    terminal_date=terminal_date,
                    terminal_nav_bucket=terminal_nav_bucket,
                    interval=interval,
                )
            )


@app.get("/investment/return-series")
def investment_return_series(
    start: str = Query("2026-01-01"),
    interval: str = Query("daily", pattern="^(daily|weekly)$"),
    baseCurrency: str = "CNY",
    refreshSnapshot: bool = Query(False),
):
    """Return sampled investment return rate based on market value, cost, and confirmed income."""
    try:
        start_date = Date.fromisoformat(start)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="start must be YYYY-MM-DD") from exc

    payload = accounts_endpoint._overview_payload()

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            view_as_of = _investment_return_view_as_of(cur)
            if start_date > view_as_of:
                start_date = view_as_of
            if not refreshSnapshot:
                snapshot_payload = _build_investment_return_series_payload_from_snapshot(
                    cur,
                    start_date=start_date,
                    interval=interval,
                    base_currency=baseCurrency,
                    view_as_of=view_as_of,
                    today_label_date=_investment_today_label_date(),
                    require_data_completeness=True,
                )
                if snapshot_payload is not None:
                    valuation_state = _latest_published_portfolio_valuation_state(
                        cur,
                        base_currency=baseCurrency,
                    )
                    return json(
                        _apply_published_valuation_state_to_return_series_payload(
                            snapshot_payload,
                            valuation_state,
                        )
                    )
            payload = _build_investment_return_series_payload(
                cur,
                start_date=start_date,
                interval=interval,
                base_currency=baseCurrency,
                view_as_of=view_as_of,
                overview_payload=payload,
                reported_returns_cfg=_reported_returns_cfg(),
                owner_ratio_case_sql=_owner_ratio_case_sql,
                account_owner_ratio=_account_owner_ratio,
                account_initial_capital_cny=_account_initial_capital_cny,
                excluded_holding_accounts=_excluded_holding_accounts(),
                today_label_date=_investment_today_label_date(),
            )
            if refreshSnapshot:
                try:
                    _upsert_investment_return_series_snapshot(
                        cur,
                        payload=payload,
                        base_currency=baseCurrency,
                    )
                except Exception:
                    conn.rollback()
            valuation_state = _latest_published_portfolio_valuation_state(
                cur,
                base_currency=baseCurrency,
            )
            return json(
                _apply_published_valuation_state_to_return_series_payload(
                    payload,
                    valuation_state,
                )
            )


@app.get("/nav/drawdown")
def nav_drawdown(limit: int = Query(365, ge=2, le=5000)):
    """NAV回撤"""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            return json(_build_nav_drawdown_payload(cur, limit=limit))
@app.get("/cashflow/series")
def cashflow_series(limit: int = Query(30, ge=1, le=365), currency: str = "CNY"):
    """现金流序列（占位）"""
    return json(_build_cashflow_series_payload(limit=limit, currency=currency))


@app.get("/risk/latest")
def risk_latest(limit: int = Query(100, ge=1, le=1000)):
    """风险事件"""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            return json(_build_risk_latest_payload(cur, limit=limit))


@app.get("/policy/ips")
def policy_ips():
    """投资策略"""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            return json(_build_policy_ips_payload(cur))


@app.get("/watchlist")
def watchlist(limit: int = Query(200, ge=1, le=2000)):
    """Return research watchlist rows."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            return json(_build_watchlist_payload(cur, limit=limit))


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
