"""API orchestration for account, holdings and FX compatibility routes."""
from datetime import date, datetime, timezone
from contextlib import closing
from hmac import compare_digest
import os
from typing import Optional
import psycopg2.extras
from fastapi import APIRouter, Header, HTTPException, Query

from collectors.open_exchange_rates import fetch_usd_rates
from domain.market.fx_rates import (
    OPEN_EXCHANGE_RATES_SOURCE, REFRESH_CURRENCIES, has_finalized_daily_fx, has_official_usd_cny,
    store_daily_fx_rates, store_official_fx_rate,
)


def create_core_router(
    get_conn,
    json,
    _build_accounts_payload,
    _build_account_detail_payload,
    _build_categories_payload,
    _build_api_holdings_latest_payload,
    _holdings_market_view_as_of,
    _owner_ratio_case_sql,
    _build_holdings_summary_payload,
    _build_holdings_by_account_payload,
    _build_latest_fx_rates_payload,
    _account_owner_ratio,
    _view_as_of,
):
    router = APIRouter()

    @router.get("/api/accounts")
    def get_accounts(active: bool = Query(True)):
        """获取账户列表"""
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                return json(_build_accounts_payload(cur, active=active))


    @router.get("/api/accounts/{account_code}")
    def get_account_detail(account_code: str):
        """获取单个账户详情"""
        with get_conn() as conn:
            conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                account_data = _build_account_detail_payload(cur, account_code=account_code)
                if account_data is None:
                    raise HTTPException(status_code=404, detail=f"账户 {account_code} 不存在")
                return json(account_data)


    @router.get("/api/categories")
    def get_categories():
        """获取所有资产分类"""
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                return json(_build_categories_payload(cur))


    @router.get("/api/holdings/latest")
    def holdings_latest(
        account_code: Optional[str] = None,
        major_category: Optional[str] = None,
        limit: int = Query(500, ge=1, le=5000),
    ):
        """获取最新持仓"""
        with get_conn() as conn:
            conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
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


    @router.get("/api/holdings/summary")
    def holdings_summary(base_currency: str = Query("CNY")):
        """持仓汇总统计（按大类分组，含汇率换算）"""
        owner_ratio_sql, owner_ratio_params = _owner_ratio_case_sql("h")
        with get_conn() as conn:
            conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
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


    @router.get("/api/holdings/by-account")
    def holdings_by_account(base_currency: str = Query("CNY"), include_portfolio_cash: bool = Query(False)):
        """按账户汇总持仓（含汇率换算）"""
        owner_ratio_sql, owner_ratio_params = _owner_ratio_case_sql("h")
        with get_conn() as conn:
            conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                return json(
                    _build_holdings_by_account_payload(
                        cur,
                        base_currency=base_currency,
                        view_as_of=_holdings_market_view_as_of(cur),
                        owner_ratio_sql=owner_ratio_sql,
                        owner_ratio_params=owner_ratio_params,
                        portfolio_owner_ratio=_account_owner_ratio if include_portfolio_cash else None,
                        cash_view_as_of=_view_as_of() if include_portfolio_cash else None,
                    )
                )


    @router.get("/api/fx-rates/latest")
    def get_latest_fx_rates(base_currency: str = Query("CNY")):
        """获取最新汇率"""
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                return json(_build_latest_fx_rates_payload(cur, base_currency=base_currency))


    @router.post("/api/fx-rates/refresh")
    def refresh_fx_rates(as_of: date | None = None, finalized: bool = False):
        """Refresh the requested UTC business date from Open Exchange Rates."""
        from domain.imports.isolation import source_mode

        if source_mode() == "development":
            raise HTTPException(status_code=409, detail="External synchronization is disabled in development mode")
        today = datetime.now(timezone.utc).date()
        day = as_of or today
        if day > today:
            raise HTTPException(status_code=400, detail="FX date is in the future")
        if finalized:
            if day >= today:
                raise HTTPException(status_code=400, detail="Finalized FX requires a completed UTC date")
            with closing(get_conn()) as conn, conn:
                conn.set_session(readonly=True)
                with conn.cursor() as cur:
                    if has_finalized_daily_fx(cur, day):
                        return json({"success": True, "as_of": day.isoformat(), "source": OPEN_EXCHANGE_RATES_SOURCE,
                                     "stored": len(REFRESH_CURRENCIES) ** 2})
        app_id = os.environ.get("PAMS_OPEN_EXCHANGE_RATES_APP_ID", "").strip()
        if not app_id:
            raise HTTPException(status_code=503, detail="Open Exchange Rates is not configured")
        try:
            usd_rates = fetch_usd_rates(day, today=today, app_id=app_id, currencies=REFRESH_CURRENCIES)
            with get_conn() as conn:
                with conn.cursor() as cur:
                    rates = store_daily_fx_rates(cur, day=day, usd_rates=usd_rates, historical=finalized)
                conn.commit()
        except (ValueError, ArithmeticError) as exc:
            raise HTTPException(status_code=502, detail="Open Exchange Rates returned unusable rates") from exc
        return json({
            "success": True,
            "as_of": day.isoformat(),
            "source": OPEN_EXCHANGE_RATES_SOURCE,
            "stored": len(rates),
            "rates_preview": {
                "USD/CNY": float(rates["CNY", "USD"]),
                "MXN/CNY": float(rates["CNY", "MXN"]),
                "CNY/USD": float(rates["USD", "CNY"]),
                "CNY/MXN": float(rates["MXN", "CNY"]),
            },
        })


    @router.post("/api/fx-rates/historical-usd-cny")
    def backfill_historical_usd_cny(as_of: date, x_pams_holdings_token: str = Header(default="")):
        from domain.imports.isolation import source_mode

        if source_mode() == "development":
            raise HTTPException(status_code=409, detail="External synchronization is disabled in development mode")
        token = os.environ.get("PAMS_IBKR_HOLDINGS_SYNC_TOKEN", "").strip()
        if len(token) < 32 or not compare_digest(token.encode(), x_pams_holdings_token.encode()):
            raise HTTPException(status_code=403, detail="Historical USD/CNY synchronization is unavailable")
        today = datetime.now(timezone.utc).date()
        if as_of >= today:
            return json({"status": "waiting", "reason": "historical_quote_not_due", "as_of": as_of.isoformat()})
        result = {"status": "ok", "as_of": as_of.isoformat(), "source": OPEN_EXCHANGE_RATES_SOURCE}
        with closing(get_conn()) as conn, conn:
            conn.set_session(readonly=True)
            with conn.cursor() as cur:
                if has_official_usd_cny(cur, as_of):
                    return json(result)
        app_id = os.environ.get("PAMS_OPEN_EXCHANGE_RATES_APP_ID", "").strip()
        if not app_id:
            raise HTTPException(status_code=503, detail="Open Exchange Rates is not configured")
        try:
            rates = fetch_usd_rates(as_of, today=today, app_id=app_id, currencies=("USD", "CNY"))
            with closing(get_conn()) as conn, conn:
                with conn.cursor() as cur:
                    store_official_fx_rate(cur, day=as_of, base="CNY", quote="USD", rate=rates["CNY"], historical=True)
        except (ValueError, ArithmeticError) as exc:
            raise HTTPException(status_code=502, detail="Open Exchange Rates returned unusable rates") from exc
        return json(result)

    return router, (get_accounts, get_account_detail, get_categories, holdings_latest, holdings_summary, holdings_by_account, get_latest_fx_rates, refresh_fx_rates)
