"""Compatibility API orchestration for asset, NAV and investment views."""
from datetime import date as Date, timedelta
from decimal import Decimal
from typing import Optional
import psycopg2.extras
from fastapi import APIRouter, HTTPException, Query


def create_compatibility_router(
    ANCHOR_NAV_DATE,
    _account_initial_capital_cny,
    _account_owner_ratio,
    _account_owner_ratios,
    _apply_published_valuation_state_to_return_series_payload,
    _apply_reported_account_performance,
    _apply_section_holdings_pnl_overlay,
    _apply_summary_pnl_to_return_series_payload,
    _build_account_performance_payload,
    _build_cashflow_series_payload,
    _build_holdings_by_category_payload,
    _build_holdings_history_payload,
    _build_holdings_latest_payload,
    _build_investment_return_series_payload,
    _build_investment_return_series_payload_from_snapshot,
    _build_nav_drawdown_payload,
    _build_nav_series_payload,
    _build_nav_weekly_payload,
    _build_risk_latest_payload,
    _build_section_return_series_payload,
    _build_section_return_series_payload_from_snapshot,
    _build_summary2_latest_payload_for_request,
    _current_nav_account_values_from_parts,
    _current_nav_bucket_from_parts,
    _daily_pnl_view_as_of,
    _default_owner_ratio,
    _excluded_holding_accounts,
    _holdings_market_view_as_of,
    _investment_return_view_as_of,
    _investment_today_label_date,
    _is_published_valuation_fresh_for_date,
    _latest_published_portfolio_valuation_state,
    _load_account_balances,
    _load_investment_positions,
    _load_portfolio_accounts_config,
    _owner_ratio_case_sql,
    _published_daily_close_compatible,
    _published_nav_bucket_from_row,
    _reported_returns_cfg,
    _upsert_investment_return_series_snapshot,
    _view_as_of,
    accounts_endpoint,
    attach_recent_market_returns,
    get_conn,
    json,
):
    router = APIRouter()

    @router.get("/summary2/latest")
    def summary2_latest():
        """总览页汇总（前端兼容）- 使用major_category替代asset_class_map"""
        with get_conn() as conn:
            conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                return json(_build_summary2_latest_payload_for_request(cur))


    @router.get("/holdings/latest")
    def holdings_latest_compat(
        limit: int = Query(500, ge=1, le=5000),
        include_closed: bool = Query(False, alias="includeClosed"),
    ):
        """持仓页（前端兼容格式：account, type, name, currency, market_value, holding_pnl, daily_pnl）"""
        with get_conn() as conn:
            conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                def load(view_date, daily_date):
                    return _build_holdings_latest_payload(
                        cur,
                        limit=limit,
                        include_closed=include_closed,
                        view_as_of=view_date,
                        excluded_accounts=_excluded_holding_accounts(),
                        owner_ratios=_account_owner_ratios(),
                        default_owner_ratio=_default_owner_ratio(),
                        daily_pnl_start_as_of=daily_date,
                    )
                active_date = _daily_pnl_view_as_of()
                return json(attach_recent_market_returns(
                    cur, load(_holdings_market_view_as_of(cur), active_date), active_date=active_date,
                    load_date=lambda day: load(day, day),
                ))


    @router.get("/holdings/by-category/{category}")
    def holdings_by_category(
        category: str,
        limit: int = Query(500, ge=1, le=5000),
        minor_category: Optional[str] = None,
        include_closed: bool = Query(False, alias="includeClosed"),
        include_closed_legacy: Optional[bool] = Query(None, alias="include_closed"),
    ):
        """Return holdings by major category."""
        with get_conn() as conn:
            conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                def load(view_date, daily_date):
                    return _build_holdings_by_category_payload(
                        cur,
                        category=category,
                        limit=limit,
                        minor_category=minor_category,
                        include_closed=include_closed if include_closed_legacy is None else include_closed_legacy,
                        view_as_of=view_date,
                        excluded_accounts=_excluded_holding_accounts(),
                        owner_ratios=_account_owner_ratios(),
                        default_owner_ratio=_default_owner_ratio(),
                        daily_pnl_start_as_of=daily_date,
                    )
                active_date = _daily_pnl_view_as_of()
                return json(attach_recent_market_returns(
                    cur, load(_holdings_market_view_as_of(cur), active_date), active_date=active_date,
                    load_date=lambda day: load(day, day),
                ))


    @router.get("/api/account-performance/{account_code}")
    def account_performance(account_code: str):
        """Return account-level personal performance using configured owner ratio and initial capital."""
        owner_ratio = _account_owner_ratio(account_code)
        initial_capital = _account_initial_capital_cny(account_code)
        account_config = (_load_portfolio_accounts_config().get("accounts") or {}).get(account_code) or {}
        capital_as_of = account_config.get("initial_capital_as_of") if isinstance(account_config, dict) else None
        with get_conn() as conn:
            conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                overview_payload = accounts_endpoint._overview_payload(cur=cur)
                account_overview: dict = next(
                    (item for item in overview_payload.get("accounts", []) if str(item.get("account_code") or "") == account_code),
                    {},
                )
                primary_balance = account_overview.get("primary_balance") or {}
                gross_balance = primary_balance.get("gross_amount")
                net_balance = primary_balance.get("amount")
                balance_as_of = primary_balance.get("as_of")
                payload = _build_account_performance_payload(
                    cur,
                    account_code=account_code,
                    owner_ratio=owner_ratio,
                    initial_capital=initial_capital,
                    initial_capital_as_of=capital_as_of,
                    net_liquidation_gross=Decimal(str(gross_balance)) if gross_balance is not None else None,
                    net_liquidation=Decimal(str(net_balance)) if net_balance is not None else None,
                    net_liquidation_as_of=balance_as_of,
                    daily_pnl_label_date=_daily_pnl_view_as_of(),
                )
                if payload is None:
                    from domain.accounts.accounts_view import build_account_detail_payload
                    detail = build_account_detail_payload(cur, account_code=account_code)
                    if detail is None:
                        raise HTTPException(status_code=404, detail=f"Account {account_code} does not exist")
                    return json({
                        "account_code": account_code,
                        "status": "unavailable",
                        "reason": "account_balance_or_valuation_unverified",
                        "as_of": detail.get("as_of"),
                        "base_currency": "CNY",
                        "currency_values": detail.get("currency_values", []),
                        "net_liquidation_gross_cny": None,
                        "net_liquidation_cny": None,
                        "position_market_value_cny": None,
                        "holding_pnl_cny": None,
                        "total_pnl_cny": None,
                        "daily_pnl_cny": None,
                        "return_ratio": None,
                    })
                return json(_apply_reported_account_performance(payload, account_code))


    @router.get("/holdings/history/{category}")
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
            conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
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


    @router.get("/nav/series")
    def nav_series(limit: int = Query(90, ge=1, le=365), baseCurrency: str = "CNY"):
        """NAV时间序列"""
        with get_conn() as conn:
            conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                start = _investment_return_view_as_of(cur) - timedelta(days=limit - 1)
                payload = _verified_nav_weekly(cur, start_date=start, baseCurrency=baseCurrency,
                                               latest=True, interval="daily")
                return json(_build_nav_series_payload(payload["rows"][-limit:]))


    @router.get("/investment/section-return-series")
    def investment_section_return_series(
        section: str = Query(..., pattern="^(a_stock|a_fund|us_stock|precious_metal)$"),
        start: str = Query("2026-01-01"),
        interval: str = Query("daily", pattern="^(daily|weekly)$"),
        baseCurrency: str = "CNY",
    ):
        """Return section return rates after removing deposits, subscriptions, and redemptions."""
        try:
            start_date = Date.fromisoformat(start)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="start must be YYYY-MM-DD") from exc
    
        with get_conn() as conn:
            conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                payload = accounts_endpoint._overview_payload(cur=cur)
                view_as_of = _investment_return_view_as_of(cur)
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
                    return json(_apply_section_holdings_pnl_overlay(cur, section=section, payload=snapshot_payload))
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
                payload = _apply_section_holdings_pnl_overlay(cur, section=section, payload=payload)
                return json(payload)


    @router.post("/investment/section-return-series/snapshot")
    def refresh_investment_section_return_series_snapshot(
        section: str = Query(..., pattern="^(a_stock|a_fund|us_stock|precious_metal)$"),
        start: str = Query("2026-01-01"),
        interval: str = Query("daily", pattern="^(daily|weekly)$"),
        baseCurrency: str = "CNY",
    ):
        """Explicitly rebuild and persist a section return-series snapshot."""
        try:
            start_date = Date.fromisoformat(start)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="start must be YYYY-MM-DD") from exc
    
        try:
            with get_conn() as conn:
                conn.set_session(isolation_level='REPEATABLE READ')
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    overview_payload = accounts_endpoint._overview_payload(cur=cur)
                    view_as_of = _investment_return_view_as_of(cur)
                    payload = _build_section_return_series_payload(
                        cur,
                        section=section,
                        start_date=start_date,
                        interval=interval,
                        base_currency=baseCurrency,
                        view_as_of=view_as_of,
                        reported_returns_cfg=_reported_returns_cfg(),
                        overview_payload=overview_payload,
                        owner_ratio_case_sql=_owner_ratio_case_sql,
                        account_owner_ratio=_account_owner_ratio,
                        excluded_holding_accounts=_excluded_holding_accounts(),
                        today_label_date=_investment_today_label_date(),
                    )
                    payload = _apply_section_holdings_pnl_overlay(cur, section=section, payload=payload)
                    written = _upsert_investment_return_series_snapshot(
                        cur,
                        payload=payload,
                        base_currency=baseCurrency,
                        section=section,
                    )
                conn.commit()
        except (psycopg2.errors.SerializationFailure, psycopg2.errors.DeadlockDetected) as exc:
            raise HTTPException(status_code=409, detail="资产数据并发变化，请重新读取后重试") from exc
        return json(
            {
                "status": "ok",
                "section": section,
                "base_currency": baseCurrency,
                "rows": len(payload.get("rows") or []),
                "written": written,
                "as_of": payload.get("as_of"),
            }
        )


    def _verified_nav_weekly(cur, *, start_date, baseCurrency, latest, interval):
        payload = accounts_endpoint._overview_payload(cur=cur)
        current_view_as_of = _investment_return_view_as_of(cur) if latest else ANCHOR_NAV_DATE
        account_cfg = _load_portfolio_accounts_config()
        account_balances = _load_account_balances(cur, baseCurrency, account_cfg)
        investment_positions = _load_investment_positions(
            cur, baseCurrency, account_cfg, account_balances=account_balances)
        incomplete_snapshot_accounts = {str(row.get("account_code")) for row in [*account_balances, *investment_positions]
            if row.get("account_code") and row.get("include_in_nav", True)
            and row.get("snapshot_scope_status") in {"partial", "unknown"}}
        published_state = (
            _latest_published_portfolio_valuation_state(cur, base_currency=baseCurrency)
            if latest
            else None
        )
        published_state_fresh = bool(
            published_state
            and _is_published_valuation_fresh_for_date(published_state, current_view_as_of)
            and _published_daily_close_compatible(published_state, account_balances=account_balances)
        )
        terminal_date = (
            published_state["valuation_date"]
            if published_state_fresh and published_state and published_state.get("valuation_date")
            else current_view_as_of
        )
        terminal_nav_bucket = None
        terminal_account_values = None
        if published_state_fresh and published_state:
            terminal_nav_bucket = _published_nav_bucket_from_row(published_state)
        elif latest:
            current_as_of_dates = [
                str(item.get("as_of") or "")[:10]
                for item in [*account_balances, *investment_positions]
                if item.get("as_of")
            ]
            if current_as_of_dates:
                terminal_date = Date.fromisoformat(max(current_as_of_dates))
            terminal_nav_bucket = _current_nav_bucket_from_parts(
                account_balances=account_balances,
                investment_positions=investment_positions,
            )
            terminal_account_values = _current_nav_account_values_from_parts(
                account_balances=account_balances,
                investment_positions=investment_positions,
            )
        return _build_nav_weekly_payload(
                cur,
                start_date=start_date,
                base_currency=baseCurrency,
                overview_payload=payload,
                owner_ratio_case_sql=_owner_ratio_case_sql,
                account_owner_ratio=_account_owner_ratio,
                terminal_date=terminal_date,
                terminal_nav_bucket=terminal_nav_bucket,
                terminal_account_values_cny=terminal_account_values,
                snapshot_scope_incomplete_accounts=incomplete_snapshot_accounts,
                include_ibkr=(account_cfg.get("accounts", {}).get("ibkr", {}).get("include_in_nav") is not False),
                interval=interval,
            )

    @router.get("/nav/weekly")
    def nav_weekly(
        start: str = Query("2026-01-01"),
        baseCurrency: str = "CNY",
        latest: bool = Query(False),
        interval: str = Query("weekly", pattern="^(daily|weekly)$"),
    ):
        """Return NAV using account anchors and evidence-backed statement flows."""
        try:
            start_date = Date.fromisoformat(start)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="start must be YYYY-MM-DD") from exc
        with get_conn() as conn:
            conn.set_session(isolation_level="REPEATABLE READ", readonly=True)
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                return json(_verified_nav_weekly(cur, start_date=start_date, baseCurrency=baseCurrency,
                                                 latest=latest, interval=interval))


    @router.get("/investment/return-series")
    def investment_return_series(
        start: str = Query("2026-01-01"),
        interval: str = Query("daily", pattern="^(daily|weekly)$"),
        baseCurrency: str = "CNY",
    ):
        """Return sampled investment return rate based on market value, cost, and confirmed income."""
        try:
            start_date = Date.fromisoformat(start)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="start must be YYYY-MM-DD") from exc
    
    
        with get_conn() as conn:
            conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                payload = accounts_endpoint._overview_payload(cur=cur)
                view_as_of = _investment_return_view_as_of(cur)
                if start_date > view_as_of:
                    start_date = view_as_of
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
                    payload_with_valuation = (
                        _apply_published_valuation_state_to_return_series_payload(
                            snapshot_payload,
                            valuation_state,
                        )
                    )
                    summary_payload = _build_summary2_latest_payload_for_request(cur)
                    return json(_apply_summary_pnl_to_return_series_payload(payload_with_valuation, summary_payload))
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
                valuation_state = _latest_published_portfolio_valuation_state(
                    cur,
                    base_currency=baseCurrency,
                )
                payload_with_valuation = (
                    _apply_published_valuation_state_to_return_series_payload(
                        payload,
                        valuation_state,
                    )
                )
                summary_payload = _build_summary2_latest_payload_for_request(cur)
                return json(_apply_summary_pnl_to_return_series_payload(payload_with_valuation, summary_payload))


    @router.post("/investment/return-series/snapshot")
    def refresh_investment_return_series_snapshot(
        start: str = Query("2026-01-01"),
        interval: str = Query("daily", pattern="^(daily|weekly)$"),
        baseCurrency: str = "CNY",
    ):
        """Explicitly rebuild and persist the total investment return-series snapshot."""
        try:
            start_date = Date.fromisoformat(start)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="start must be YYYY-MM-DD") from exc
    
        try:
            with get_conn() as conn:
                conn.set_session(isolation_level='REPEATABLE READ')
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    overview_payload = accounts_endpoint._overview_payload(cur=cur)
                    view_as_of = _investment_return_view_as_of(cur)
                    if start_date > view_as_of:
                        start_date = view_as_of
                    payload = _build_investment_return_series_payload(
                        cur,
                        start_date=start_date,
                        interval=interval,
                        base_currency=baseCurrency,
                        view_as_of=view_as_of,
                        overview_payload=overview_payload,
                        reported_returns_cfg=_reported_returns_cfg(),
                        owner_ratio_case_sql=_owner_ratio_case_sql,
                        account_owner_ratio=_account_owner_ratio,
                        account_initial_capital_cny=_account_initial_capital_cny,
                        excluded_holding_accounts=_excluded_holding_accounts(),
                        today_label_date=_investment_today_label_date(),
                    )
                    written = _upsert_investment_return_series_snapshot(
                        cur,
                        payload=payload,
                        base_currency=baseCurrency,
                    )
                conn.commit()
        except (psycopg2.errors.SerializationFailure, psycopg2.errors.DeadlockDetected) as exc:
            raise HTTPException(status_code=409, detail="资产数据并发变化，请重新读取后重试") from exc
        return json(
            {
                "status": "ok",
                "section": "total",
                "base_currency": baseCurrency,
                "rows": len(payload.get("rows") or []),
                "written": written,
                "as_of": payload.get("as_of"),
            }
        )


    @router.get("/nav/drawdown")
    def nav_drawdown(limit: int = Query(365, ge=2, le=5000)):
        """NAV回撤"""
        with get_conn() as conn:
            conn.set_session(readonly=True, isolation_level="REPEATABLE READ")
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                start = _investment_return_view_as_of(cur) - timedelta(days=limit - 1)
                payload = _verified_nav_weekly(cur, start_date=start, baseCurrency="CNY",
                                               latest=True, interval="daily")
                return json(_build_nav_drawdown_payload(payload["rows"][-limit:]))


    @router.get("/cashflow/series")
    def cashflow_series(limit: int = Query(30, ge=1, le=365), currency: str = "CNY"):
        """现金流序列（占位）"""
        return json(_build_cashflow_series_payload(limit=limit, currency=currency))


    @router.get("/risk/latest")
    def risk_latest(limit: int = Query(100, ge=1, le=1000)):
        """风险事件"""
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                return json(_build_risk_latest_payload(cur, limit=limit))


    return router, (summary2_latest, holdings_latest_compat, holdings_by_category, account_performance, holdings_history_by_category, nav_series, investment_section_return_series, refresh_investment_section_return_series_snapshot, nav_weekly, investment_return_series, refresh_investment_return_series_snapshot, nav_drawdown, cashflow_series, risk_latest)
