"""
PAMS API V2 - 基于重构后的数据库结构
- 使用account_code替代venue+account
- 使用major_category/minor_category替代asset_class_map
- 支持多币种汇率换算
"""
import os
from pathlib import Path
from typing import Any, Dict, List
from datetime import date as Date, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# 导入端点路由
from endpoints import prices
from endpoints import holdings_trade
from endpoints import ibkr
from endpoints import investment_transactions
from endpoints import ledger
from endpoints import bank_reconcile
from endpoints import accounts as accounts_endpoint
from endpoints import portfolio_state as portfolio_state_endpoint
from endpoints import sync_center
from endpoints import analysis
from endpoints import account_import
from domain.accounts.accounts_view import (
    build_account_detail_payload as _build_account_detail_payload,
    build_accounts_payload as _build_accounts_payload,
)
from domain.ledger.cashflow_view import build_cashflow_series_payload as _build_cashflow_series_payload
from domain.market.fx_rates import build_latest_fx_rates_payload as _build_latest_fx_rates_payload
from domain.investments.holdings_view import (
    advance_overseas_view_date as _advance_overseas_view_date,
    build_api_holdings_latest_payload as _build_api_holdings_latest_payload,
    build_holdings_by_category_payload as _build_holdings_by_category_payload,
    build_holdings_history_payload as _build_holdings_history_payload,
    build_holdings_latest_payload as _build_holdings_latest_payload,
    latest_complete_snapshot_date as _latest_complete_snapshot_date,
)
from domain.investments.holding_return_display import attach_recent_market_returns
from domain.investments.holding_return_snapshot import (
    build_investment_return_series_payload_from_snapshot as _build_investment_return_series_payload_from_snapshot,
    build_section_return_series_payload_from_snapshot as _build_section_return_series_payload_from_snapshot,
    upsert_investment_return_series_snapshot as _upsert_investment_return_series_snapshot,
)
from domain.investments.investment_returns import (
    apply_reported_display_anchor as _apply_reported_display_anchor,
    build_account_performance_payload as _build_account_performance_payload,
    build_investment_return_series_payload as _build_investment_return_series_payload,
    build_section_return_series_payload as _build_section_return_series_payload,
    section_return_summary as _section_return_summary,
)
from domain.investments.investment_view_date import (
    clamp_to_latest_data_date as _clamp_to_latest_data_date,
    daily_pnl_window_date as _daily_pnl_window_date,
    market_data_candidate_date as _market_data_candidate_date,
)
from domain.investments.portfolio_summary import (
    apply_return_attribution_pnl_to_summary as _apply_return_attribution_pnl_to_summary,
    apply_published_valuation_state_to_summary as _apply_published_valuation_state_to_summary,
    build_holdings_by_account_payload as _build_holdings_by_account_payload,
    build_holdings_summary_payload as _build_holdings_summary_payload,
    build_summary2_from_holdings_latest_payload as _build_summary2_from_holdings_latest_payload,
)
from domain.investments.return_attribution import build_return_attribution_payload as _build_return_attribution_payload
from domain.investments.portfolio_state import (
    _load_accounts_config as _load_portfolio_accounts_config,
    load_account_balances as _load_account_balances,
    load_investment_positions as _load_investment_positions,
)
from domain.investments.portfolio_valuation_state import (
    apply_holdings_category_pnl_to_section_return_series_payload as _apply_holdings_category_pnl_to_section_return_series_payload,
    apply_summary_pnl_to_return_series_payload as _apply_summary_pnl_to_return_series_payload,
    apply_published_valuation_state_to_return_series_payload as _apply_published_valuation_state_to_return_series_payload,
    is_published_valuation_fresh_for_date as _is_published_valuation_fresh_for_date,
    latest_published_portfolio_valuation_state as _latest_published_portfolio_valuation_state,
    published_nav_bucket_from_row as _published_nav_bucket_from_row,
    published_daily_close_compatible as _published_daily_close_compatible,
)
from domain.assets.reference_data import build_categories_payload as _build_categories_payload
from domain.risk.risk_state import build_risk_latest_payload as _build_risk_latest_payload
from domain.investments.valuation_nav import (
    ANCHOR_NAV_DATE,
    account_anchor_amount as _account_anchor_amount,
    account_anchor_value_cny as _account_anchor_value_cny,
    add_nav_amount as _add_nav_amount,
    build_nav_drawdown_payload as _build_nav_drawdown_payload,
    build_nav_series_payload as _build_nav_series_payload,
    build_nav_weekly_payload as _build_nav_weekly_payload,
    build_weekly_nav_rows as _build_weekly_nav_rows,
    current_nav_account_values_from_parts as _current_nav_account_values_from_parts,
    current_nav_bucket_from_parts as _current_nav_bucket_from_parts,
    format_weekly_nav_payload as _format_weekly_nav_payload,
    fx_rate_at as _fx_rate_at,
    load_fx_timelines as _load_fx_timelines,
    manual_effective_date as _manual_effective_date,
    scale_series_to_terminal_anchor as _scale_series_to_terminal_anchor,
    select_weekly_nav_accounts as _select_weekly_nav_accounts,
    series_dates as _series_dates,
)
from domain.risk.core.config import load_risk_limits
from domain.configuration import load_base_currency

app = FastAPI(title="pams-api-v2", version="2.0.0")

# 注册路由
app.include_router(prices.router, prefix="/api")
app.include_router(holdings_trade.router, prefix="/api")
app.include_router(ibkr.router, prefix="/api")
app.include_router(investment_transactions.router, prefix="/api")
app.include_router(ledger.router, prefix="/api")
app.include_router(bank_reconcile.router, prefix="/api")
app.include_router(accounts_endpoint.router, prefix="/api")
app.include_router(portfolio_state_endpoint.router, prefix="/api")
app.include_router(sync_center.router, prefix="/api")
app.include_router(analysis.router, prefix="/api")
app.include_router(account_import.router, prefix="/api")

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
    return _advance_overseas_view_date(cur, _clamp_to_latest_data_date(candidate, latest))


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


def _apply_reported_account_performance(payload: dict[str, Any], account_code: str) -> dict[str, Any]:
    """Overlay platform-confirmed account PnL while preserving economic audit fields."""
    cfg = ((_reported_returns_cfg().get("account_performance") or {}).get(account_code)) or {}
    if not isinstance(cfg, dict) or not cfg.get("use_reported_anchor"):
        return payload

    reported_total: Decimal | None = None
    if cfg.get("total_return") not in (None, ""):
        reported_total = Decimal(str(cfg["total_return"]))
    elif cfg.get("total_return_gross") not in (None, ""):
        reported_total = Decimal(str(cfg["total_return_gross"])) * _account_owner_ratio(account_code)
    elif cfg.get("total_ytd_pnl") not in (None, ""):
        reported_total = Decimal(str(cfg["total_ytd_pnl"])) * _account_owner_ratio(account_code)
    if reported_total is None:
        return payload

    out = dict(payload)
    owner_ratio = _account_owner_ratio(account_code)
    cash_balance_gross: Decimal | None = None
    if cfg.get("cash_balance_gross") not in (None, ""):
        cash_balance_gross = Decimal(str(cfg["cash_balance_gross"]))
        position_gross = Decimal(str(out.get("position_market_value_gross_cny") or "0"))
        expected_net_gross = position_gross + cash_balance_gross
        current_net_gross = Decimal(str(out.get("net_liquidation_gross_cny") or "0"))
        if expected_net_gross and abs(current_net_gross - expected_net_gross) > Decimal("0.005"):
            out["net_liquidation_gross_cny"] = str(expected_net_gross)
            out["net_liquidation_cny"] = str(expected_net_gross * owner_ratio)
            out["cash_or_uninvested_cny"] = str(cash_balance_gross * owner_ratio)
            out["net_liquidation_source"] = "reported_platform"
    initial_personal = Decimal(str(out.get("initial_capital_cny") or "0"))
    net_personal = Decimal(str(out.get("net_liquidation_cny") or "0"))
    economic_total = net_personal - initial_personal if initial_personal else Decimal(str(out.get("total_pnl_cny") or "0"))
    out["economic_total_pnl_cny"] = str(economic_total)
    out["total_pnl_cny"] = str(reported_total)
    holding_pnl = Decimal(str(out.get("holding_pnl_cny") or "0"))
    out["realized_pnl_cny"] = str(reported_total - holding_pnl)
    out["return_ratio"] = str(reported_total / initial_personal) if initial_personal > 0 else out.get("return_ratio")
    out["return_basis"] = "reported_platform"
    out["reported_performance"] = {
        "as_of": str(cfg.get("as_of") or ""),
        "basis": cfg.get("basis"),
        "note": cfg.get("note"),
        "cash_balance_gross": str(cash_balance_gross) if cash_balance_gross is not None else None,
        "total_return_gross": str(cfg.get("total_return_gross")) if cfg.get("total_return_gross") not in (None, "") else None,
    }
    return out









SECTION_RETURN_HOLDINGS_OVERLAY = {
    "a_fund": ("\u0041\u80a1", "\u0041\u80a1\u57fa\u91d1"),
}


def _apply_section_holdings_pnl_overlay(
    cur,
    *,
    section: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    spec = SECTION_RETURN_HOLDINGS_OVERLAY.get(section)
    if not spec:
        return payload
    category, minor_category = spec
    holdings_payload = _build_holdings_by_category_payload(
        cur,
        category=category,
        limit=5000,
        minor_category=minor_category,
        include_closed=True,
        view_as_of=_holdings_market_view_as_of(cur),
        excluded_accounts=_excluded_holding_accounts(),
        owner_ratios=_account_owner_ratios(),
        default_owner_ratio=_default_owner_ratio(),
        daily_pnl_start_as_of=_daily_pnl_view_as_of(),
    )
    return _apply_holdings_category_pnl_to_section_return_series_payload(
        payload,
        holdings_payload,
        source=f"holdings.by_category.{section}",
    )


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

from endpoints.core_routes import create_core_router

_core_router, (
    get_accounts, get_account_detail, get_categories, holdings_latest,
    holdings_summary, holdings_by_account, get_latest_fx_rates, refresh_fx_rates,
) = create_core_router(
    get_conn=lambda *args, **kwargs: get_conn(*args, **kwargs),
    json=lambda *args, **kwargs: json(*args, **kwargs),
    _build_accounts_payload=lambda *args, **kwargs: _build_accounts_payload(*args, **kwargs),
    _build_account_detail_payload=lambda *args, **kwargs: _build_account_detail_payload(*args, **kwargs),
    _build_categories_payload=lambda *args, **kwargs: _build_categories_payload(*args, **kwargs),
    _build_api_holdings_latest_payload=lambda *args, **kwargs: _build_api_holdings_latest_payload(*args, **kwargs),
    _holdings_market_view_as_of=lambda *args, **kwargs: _holdings_market_view_as_of(*args, **kwargs),
    _owner_ratio_case_sql=lambda *args, **kwargs: _owner_ratio_case_sql(*args, **kwargs),
    _build_holdings_summary_payload=lambda *args, **kwargs: _build_holdings_summary_payload(*args, **kwargs),
    _build_holdings_by_account_payload=lambda *args, **kwargs: _build_holdings_by_account_payload(*args, **kwargs),
    _build_latest_fx_rates_payload=lambda *args, **kwargs: _build_latest_fx_rates_payload(*args, **kwargs),
)
app.include_router(_core_router)



# ==================== 前端兼容端点 ====================

def _get_base_currency(cur) -> str:
    """Use the asset application's configured base currency."""
    try:
        return load_base_currency()
    except Exception:
        # Match the existing application's CNY fallback when its source fails.
        return "CNY"


def _build_summary2_latest_payload_for_request(cur) -> dict[str, Any]:
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
    return _apply_published_valuation_state_to_summary(summary_payload, valuation_state,
                                                     account_balances=account_balances)


from endpoints.compatibility_routes import create_compatibility_router

_compatibility_router, (
    summary2_latest,
    holdings_latest_compat,
    holdings_by_category,
    account_performance,
    holdings_history_by_category,
    nav_series,
    investment_section_return_series,
    refresh_investment_section_return_series_snapshot,
    nav_weekly,
    investment_return_series,
    refresh_investment_return_series_snapshot,
    nav_drawdown,
    cashflow_series,
    risk_latest,
) = create_compatibility_router(
    ANCHOR_NAV_DATE=ANCHOR_NAV_DATE,
    _account_initial_capital_cny=lambda *args, **kwargs: _account_initial_capital_cny(*args, **kwargs),
    _account_owner_ratio=lambda *args, **kwargs: _account_owner_ratio(*args, **kwargs),
    _account_owner_ratios=lambda *args, **kwargs: _account_owner_ratios(*args, **kwargs),
    _apply_published_valuation_state_to_return_series_payload=lambda *args, **kwargs: _apply_published_valuation_state_to_return_series_payload(*args, **kwargs),
    _apply_reported_account_performance=lambda *args, **kwargs: _apply_reported_account_performance(*args, **kwargs),
    _apply_section_holdings_pnl_overlay=lambda *args, **kwargs: _apply_section_holdings_pnl_overlay(*args, **kwargs),
    _apply_summary_pnl_to_return_series_payload=lambda *args, **kwargs: _apply_summary_pnl_to_return_series_payload(*args, **kwargs),
    _build_account_performance_payload=lambda *args, **kwargs: _build_account_performance_payload(*args, **kwargs),
    _build_cashflow_series_payload=lambda *args, **kwargs: _build_cashflow_series_payload(*args, **kwargs),
    _build_holdings_by_category_payload=lambda *args, **kwargs: _build_holdings_by_category_payload(*args, **kwargs),
    _build_holdings_history_payload=lambda *args, **kwargs: _build_holdings_history_payload(*args, **kwargs),
    _build_holdings_latest_payload=lambda *args, **kwargs: _build_holdings_latest_payload(*args, **kwargs),
    _build_investment_return_series_payload=lambda *args, **kwargs: _build_investment_return_series_payload(*args, **kwargs),
    _build_investment_return_series_payload_from_snapshot=lambda *args, **kwargs: _build_investment_return_series_payload_from_snapshot(*args, **kwargs),
    _build_nav_drawdown_payload=lambda *args, **kwargs: _build_nav_drawdown_payload(*args, **kwargs),
    _build_nav_series_payload=lambda *args, **kwargs: _build_nav_series_payload(*args, **kwargs),
    _build_nav_weekly_payload=lambda *args, **kwargs: _build_nav_weekly_payload(*args, **kwargs),
    _build_risk_latest_payload=lambda *args, **kwargs: _build_risk_latest_payload(*args, **kwargs),
    _build_section_return_series_payload=lambda *args, **kwargs: _build_section_return_series_payload(*args, **kwargs),
    _build_section_return_series_payload_from_snapshot=lambda *args, **kwargs: _build_section_return_series_payload_from_snapshot(*args, **kwargs),
    _build_summary2_latest_payload_for_request=lambda *args, **kwargs: _build_summary2_latest_payload_for_request(*args, **kwargs),
    _current_nav_account_values_from_parts=lambda *args, **kwargs: _current_nav_account_values_from_parts(*args, **kwargs),
    _current_nav_bucket_from_parts=lambda *args, **kwargs: _current_nav_bucket_from_parts(*args, **kwargs),
    _daily_pnl_view_as_of=lambda *args, **kwargs: _daily_pnl_view_as_of(*args, **kwargs),
    _default_owner_ratio=lambda *args, **kwargs: _default_owner_ratio(*args, **kwargs),
    _excluded_holding_accounts=lambda *args, **kwargs: _excluded_holding_accounts(*args, **kwargs),
    _holdings_market_view_as_of=lambda *args, **kwargs: _holdings_market_view_as_of(*args, **kwargs),
    _investment_return_view_as_of=lambda *args, **kwargs: _investment_return_view_as_of(*args, **kwargs),
    _investment_today_label_date=lambda *args, **kwargs: _investment_today_label_date(*args, **kwargs),
    _is_published_valuation_fresh_for_date=lambda *args, **kwargs: _is_published_valuation_fresh_for_date(*args, **kwargs),
    _latest_published_portfolio_valuation_state=lambda *args, **kwargs: _latest_published_portfolio_valuation_state(*args, **kwargs),
    _load_account_balances=lambda *args, **kwargs: _load_account_balances(*args, **kwargs),
    _load_investment_positions=lambda *args, **kwargs: _load_investment_positions(*args, **kwargs),
    _load_portfolio_accounts_config=lambda *args, **kwargs: _load_portfolio_accounts_config(*args, **kwargs),
    _owner_ratio_case_sql=lambda *args, **kwargs: _owner_ratio_case_sql(*args, **kwargs),
    _published_daily_close_compatible=lambda *args, **kwargs: _published_daily_close_compatible(*args, **kwargs),
    _published_nav_bucket_from_row=lambda *args, **kwargs: _published_nav_bucket_from_row(*args, **kwargs),
    _reported_returns_cfg=lambda *args, **kwargs: _reported_returns_cfg(*args, **kwargs),
    _upsert_investment_return_series_snapshot=lambda *args, **kwargs: _upsert_investment_return_series_snapshot(*args, **kwargs),
    _view_as_of=lambda *args, **kwargs: _view_as_of(*args, **kwargs),
    accounts_endpoint=accounts_endpoint,
    attach_recent_market_returns=lambda *args, **kwargs: attach_recent_market_returns(*args, **kwargs),
    get_conn=lambda *args, **kwargs: get_conn(*args, **kwargs),
    json=lambda *args, **kwargs: json(*args, **kwargs),
)
app.include_router(_compatibility_router)







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
