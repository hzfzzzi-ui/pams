"""Compatibility API orchestration for asset, NAV and investment views."""
from datetime import date as Date, timedelta
from decimal import Decimal
from typing import Optional
import psycopg2.extras
from fastapi import APIRouter, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from domain.accounts.balances.position_reference import read_historical_position_references
from domain.brokers.ibkr import ibkr_daily_close_read
from domain.brokers.ibkr.ibkr_holding_pnl import reuse_ibkr_holding_evidence
from domain.investments.returns.fund_return_history import METHOD as FUND_HISTORY_METHOD
from domain.investments.valuation.investment_overview import holdings_overview_card
from domain.accounts.monthly.valuation_book import current_balances, read_account_view
from domain.investments.holdings.holdings_account_cards import (
    account_center_balances,
    build_holdings_account_cards,
    merge_account_center_balances,
)
from domain.investments.holdings.holding_market_history import holding_history, holding_price_comparison
from domain.investments.holdings.personal_holdings import personal_holdings
from domain.investments.holdings.ths_trade_rollforward import merge_cash_management_holding_rows
from domain.investments.valuation import page_projection
from domain.investments.valuation.portfolio_summary import build_holdings_by_account_payload
from domain.investments.holdings.holding_return_snapshot import return_input_revisions, return_snapshot_revision
from domain.investments.holdings.holding_return_display import attach_dated_holding_performance
from domain.investments.returns.period_trade_returns import (
    attach_closed_trade_returns, fund_holding_profit, load_closed_trade_returns,
)
from domain.investments.holdings.position_performance import METHOD as POSITION_RETURN_METHOD, latest_position_valuation_date
from domain.investments.valuation.portfolio_valuation_state import (
    published_bank_balances_compatible, read_current_portfolio_valuation,
)
from domain.investments.valuation.nav_period_comparison import period_starts
from domain.market.price_history import valid_holding_history_window
from domain.market.fx_rates import OPEN_EXCHANGE_RATES_SOURCE, build_latest_fx_rates_payload


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

    def _return_revision(cur, *, start_date, interval, view_as_of, today_label_date, reported_returns_cfg,
                         section=None, revisions=None):
        broker_close = ibkr_daily_close_read.load_evidence() if ibkr_daily_close_read.configured() else {}
        return return_snapshot_revision(cur, settings={
            "version": {None: 5, "a_stock": 3, "us_stock": 2, "a_fund": 6, "precious_metal": 6}.get(section, 1),
            "start_date": start_date, "interval": interval, "view_as_of": view_as_of,
            "today_label_date": today_label_date,
            "accounts": accounts_endpoint._load_accounts_config(), "banks": accounts_endpoint._load_bank_config(),
            "portfolio_accounts": _load_portfolio_accounts_config(), "reported_returns": reported_returns_cfg,
            "broker_close": {key: broker_close.get(key) for key in ("status", "as_of", "sha256", "reasons")},
        }, revisions=revisions)

    @router.get("/summary2/latest")
    def summary2_latest():
        """总览页汇总（前端兼容）- 使用major_category替代asset_class_map"""
        with get_conn() as conn:
            conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                return json(_build_summary2_latest_payload_for_request(cur))


    def holding_context(cur):
        today = _daily_pnl_view_as_of()
        valuation_date = latest_position_valuation_date(cur, holding_date=_holdings_market_view_as_of(cur), through=today)
        return (
            valuation_date,
            today,
            {
                "excluded_accounts": _excluded_holding_accounts(),
                "owner_ratios": _account_owner_ratios(),
                "default_owner_ratio": _default_owner_ratio(),
            },
        )

    def read_holdings(cur, context, *, limit=500, include_closed=False,
                      category=None, minor_category=None):
        view_date, active_date, config = context
        builder = _build_holdings_latest_payload
        scope = {}
        if category is not None:
            builder = _build_holdings_by_category_payload
            scope = {"category": category, "minor_category": minor_category}

        def load(view, daily):
            return builder(cur, limit=limit, include_closed=include_closed, view_as_of=view,
                           daily_pnl_start_as_of=daily, **config, **scope)

        payload = load(view_date, active_date)
        recent_reads = {}

        def load_recent(day):
            if day not in recent_reads:
                recent_reads[day] = load(day, day)
            return recent_reads[day]

        payload = attach_recent_market_returns(
            cur, payload, active_date=active_date, load_date=load_recent,
        )
        holding_account = {'A股': 'tonghuashun', 'A股基金': 'tencent_licaitong'}.get(minor_category) if category == 'A股' else None
        if holding_account and (holding_account == 'tonghuashun' or include_closed):
            return attach_dated_holding_performance(
                payload, account_code=holding_account, active_date=active_date, limit=limit,
                load_date=load_recent,
                allow_mixed_snapshot_days=holding_account == 'tencent_licaitong',
            )
        return payload

    def holdings_for_list(cur, context, *, limit=500, include_closed=False, account_balances=None):
        holdings = read_holdings(cur, context, limit=None, include_closed=include_closed)
        account_config = _load_portfolio_accounts_config()
        if account_balances is None:
            try:
                account_balances = read_account_view(cur, 'account_balances', accounts_endpoint._view_as_of())
            except Exception:
                account_balances = {}
        holdings = merge_cash_management_holding_rows(
            holdings,
            account_config=account_config,
            account_balances=account_balances,
            excluded_accounts=context[2]["excluded_accounts"],
            owner_ratio=_account_owner_ratio,
        )
        holdings["historical_references"] = read_historical_position_references(
            cur, account_config=account_config, as_of=context[0],
            excluded_accounts=context[2]["excluded_accounts"], owner_ratio=_account_owner_ratio,
        )
        holdings = personal_holdings(holdings, accounts=account_config.get("accounts", {}))
        holdings["cash_view_version"] = 2
        holdings["rows"] = holdings["rows"][:limit]
        return holdings

    @router.get("/holdings/latest")
    def holdings_latest_compat(
        limit: int = Query(500, ge=1, le=5000),
        include_closed: bool = Query(False, alias="includeClosed"),
    ):
        """Return the legacy holdings list projection."""
        with get_conn() as conn:
            conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                with reuse_ibkr_holding_evidence(cur):
                    return json(holdings_for_list(cur, holding_context(cur), limit=limit, include_closed=include_closed))

    @router.get("/holdings/by-category/{category}")
    def holdings_by_category(
        category: str,
        limit: int = Query(500, ge=1, le=5000),
        minor_category: Optional[str] = None,
        include_closed: bool = Query(False, alias="includeClosed"),
        include_closed_legacy: Optional[bool] = Query(None, alias="include_closed"),
    ):
        """Return holdings by major category."""
        closed = include_closed if include_closed_legacy is None else include_closed_legacy
        with get_conn() as conn:
            conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                with reuse_ibkr_holding_evidence(cur):
                    return json(read_holdings(cur, holding_context(cur), limit=limit, include_closed=closed,
                                              category=category, minor_category=minor_category))

    @router.get("/api/investment/holdings-view")
    def investment_holdings_view(show_closed: bool = False):
        """Read the holdings screen's projections under one consistent snapshot."""
        with get_conn() as conn:
            conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                published = None if show_closed else page_projection.read_page(
                    cur, "holdings", business_date=_investment_today_label_date())
                if published is not None:
                    data = published["data"]
                    if (data["holdings"].get("cash_view_version") != 2 or
                            "investment_overview" not in (data.get("account_cards") or {}) or
                            "personal_summary" not in data["holdings"] or
                            "daily_pnl_cny" not in (data["holdings"].get("personal_summary") or {}) or
                            any("position_dividend_return" not in r or "position_pnl" not in r
                                for r in data["holdings"]["rows"])):
                        published = None
                if published is None:
                    data = build_holdings_page(cur, show_closed=show_closed)
                else:
                    data = {**data, "projection": published["projection"]}
                if "investment_overview" not in (data.get("account_cards") or {}):
                    overview = holdings_overview_card(data, _load_portfolio_accounts_config())
                    shares = overview.pop("account_card_net_shares")
                    cards = {key: {**card, "portfolio_share": shares[key]} if key in shares else card
                             for key, card in data["account_cards"].items()}
                    data = {**data, "account_cards": {**cards, "investment_overview": overview}}
                return json(data)

    @router.get("/api/investment/account-funding-history")
    def investment_account_funding_history(account: str):
        from domain.investments.valuation.capital_ownership import funding_history
        cfg = _load_portfolio_accounts_config().get("accounts", {}).get(account) or {}
        if account in _excluded_holding_accounts() or not cfg.get("funding_history"):
            return {"rows": []}
        with get_conn() as conn:
            conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                return {"rows": funding_history(cur, account, cfg)}

    @router.get("/api/investment/account-center-balances")
    def investment_account_center_balances():
        with get_conn() as conn:
            conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                balances = read_account_view(cur, 'account_balances', accounts_endpoint._view_as_of())
                published = page_projection.read_page(
                    cur, "holdings", business_date=_investment_today_label_date())
                if published is not None and isinstance(published.get("data"), dict):
                    pdata = published["data"]
                    if pdata.get("account_positions") and pdata.get("funds") and pdata.get("account_cards"):
                        card_bals = account_center_balances(
                            pdata["account_positions"], pdata["funds"], pdata["account_cards"])
                        balances = merge_account_center_balances(balances, card_bals)
                return json(accounts_endpoint._norm(balances))

    def build_holdings_page(cur, *, show_closed, account_balances=None):
        with reuse_ibkr_holding_evidence(cur):
            context = holding_context(cur)

            def category(major, minor=None, *, closed=show_closed):
                return read_holdings(cur, context, include_closed=closed,
                                     category=major, minor_category=minor)

            funds_all = category('A股', 'A股基金', closed=True)
            view_date = context[0]
            closed_returns = load_closed_trade_returns(
                cur, start=Date.min, end=view_date,
                owner_ratio=_account_owner_ratio, sections={'a_fund', 'wealth_management'},
                excluded_accounts=context[2]['excluded_accounts'],
            )
            if fund_returns := closed_returns.get('a_fund'):
                fund_returns['known_holding_and_closed_pnl'] = fund_holding_profit(funds_all, fund_returns)
                fund_returns['by_account'] = {
                    code: fund_holding_profit(funds_all, fund_returns, account_code=code)
                    for code in {item['account_code'] for item in fund_returns['items']}
                }
            stock = category('A股', 'A股', closed=True)
            overseas = category('美股')
            accounts = read_investment_accounts(cur)
            owner_sql, owner_params = _owner_ratio_case_sql("h")
            positions = build_holdings_by_account_payload(
                cur, base_currency="CNY", view_as_of=view_date,
                owner_ratio_sql=owner_sql, owner_ratio_params=owner_params,
                portfolio_owner_ratio=_account_owner_ratio, cash_view_as_of=_view_as_of(),
            )
            summary = _build_summary2_latest_payload_for_request(cur)
            cards = build_holdings_account_cards(
                positions=positions, overview=accounts["overview"], stock_performance=accounts["tonghuashun"],
                stock=stock, funds=funds_all, overseas=overseas, closed_returns=closed_returns,
            )
            page_data = {
                "holdings": holdings_for_list(
                    cur, context, limit=None, include_closed=show_closed, account_balances=account_balances,
                ),
                "a_stock": stock,
                "funds": funds_all if show_closed else category('A股', 'A股基金'),
                "funds_all": funds_all,
                "closed_trade_returns": closed_returns,
                "us_stock": overseas,
                "precious_metal": category('贵金属'),
                "esop": category('ESOP'),
                "summary": summary,
                "account_cards": cards,
                "account_positions": positions,
                "accounts": accounts,
            }
            overview = holdings_overview_card(page_data, _load_portfolio_accounts_config())
            shares = overview.pop("account_card_net_shares")
            cards_with_share = {
                key: {**card, "portfolio_share": shares[key]} if key in shares else card
                for key, card in cards.items()
            }
            page_data["account_cards"] = {**cards_with_share, "investment_overview": overview}
            return page_data


    def read_account_performance(cur, account_code, overview_payload):
        owner_ratio = _account_owner_ratio(account_code)
        initial_capital = _account_initial_capital_cny(account_code)
        account_config = (_load_portfolio_accounts_config().get("accounts") or {}).get(account_code) or {}
        capital_as_of = account_config.get("initial_capital_as_of") if isinstance(account_config, dict) else None
        account_overview = next(
            (item for item in overview_payload.get("accounts", []) if str(item.get("account_code") or "") == account_code),
            {},
        )
        primary_balance = account_overview.get("primary_balance") or {}
        gross_balance = primary_balance.get("gross_amount")
        net_balance = primary_balance.get("amount")
        payload = _build_account_performance_payload(
            cur,
            account_code=account_code,
            owner_ratio=owner_ratio,
            initial_capital=initial_capital,
            initial_capital_as_of=capital_as_of,
            net_liquidation_gross=Decimal(str(gross_balance)) if gross_balance is not None else None,
            net_liquidation=Decimal(str(net_balance)) if net_balance is not None else None,
            net_liquidation_as_of=primary_balance.get("as_of"),
            daily_pnl_label_date=_daily_pnl_view_as_of(),
        )
        if payload is not None:
            return _apply_reported_account_performance(payload, account_code)
        from domain.accounts.views.accounts_view import build_account_detail_payload
        detail = build_account_detail_payload(cur, account_code=account_code)
        if detail is None:
            raise HTTPException(status_code=404, detail=f"Account {account_code} does not exist")
        return {
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
        }

    def read_investment_accounts(cur):
        overview = accounts_endpoint._overview_payload(cur=cur, include_transfers=False)
        try:
            performance = read_account_performance(cur, 'tonghuashun', overview)
        except HTTPException as error:
            if error.status_code != 404:
                raise
            performance = None
        return {"overview": accounts_endpoint._norm(overview), "tonghuashun": performance}


    @router.get("/api/investment/accounts-view")
    def investment_accounts_view():
        """Read account balances and Tonghuashun performance with one verification."""
        with get_conn() as conn:
            conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                return json(read_investment_accounts(cur))


    @router.get("/api/holdings/market-history")
    def holding_market_history(start: Date, end: Date, account: list[str] = Query(...),
                               asset: list[str] = Query(...), all_trades: bool = False):
        if (not valid_holding_history_window(start, end) or len(account) != len(asset)
                or len(asset) > 24 or any(a in _excluded_holding_accounts() for a in account)):
            raise HTTPException(status_code=400, detail="无效的账户或查询范围")
        with get_conn() as conn:
            conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                return json(holding_history(cur, sorted(set(zip(account, asset))), start, end,
                                            _account_owner_ratio, all_trades=all_trades))

    @router.get("/api/holdings/price-comparison")
    def holding_comparison(start: Date, end: Date, account: list[str] = Query(...),
                           asset: list[str] = Query(...)):
        if (not valid_holding_history_window(start, end) or len(account) != len(asset)
                or len(asset) > 24 or any(a in _excluded_holding_accounts() for a in account)):
            raise HTTPException(status_code=400, detail="无效的账户或查询范围")
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from decimal import Decimal
        from domain.market.benchmark_history import benchmark_history, local_benchmark
        with get_conn() as conn:
            conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                def load_benchmark(symbol, provider_symbol, name):
                    closes = benchmark_history(
                        local_benchmark(cur, start, end, symbol), start, end,
                        datetime.now(ZoneInfo('Asia/Shanghai')).date(), provider_symbol, name)
                    return {row['date']: Decimal(row['close']) for row in closes['rows']}
                return json(holding_price_comparison(
                    cur, sorted(set(zip(account, asset))), start, end,
                    load_benchmark))

    @router.get("/api/holdings/position-return")
    def holding_position_return(start: Date, end: Date, account: list[str] = Query(...),
                                asset: list[str] = Query(...)):
        if (not valid_holding_history_window(start, end) or len(account) != len(asset)
                or len(asset) > 24 or any(a in _excluded_holding_accounts() for a in account)):
            raise HTTPException(status_code=400, detail="无效的账户或查询范围")
        from domain.investments.holdings.position_performance import build_position_return_series
        with get_conn() as conn:
            conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                return json(build_position_return_series(
                    cur, start=start, end=end, interval='daily', base_currency='CNY',
                    owner_ratio=_account_owner_ratio, excluded_accounts=_excluded_holding_accounts(),
                    selection=sorted(set(zip(account, asset)))))

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
        section: str = Query(..., pattern="^(a_stock|a_fund|us_stock|precious_metal|stocks|wealth)$"),
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
                if section in ("stocks", "wealth"):
                    published = page_projection.read_page(cur, "analysis", business_date=_investment_today_label_date(), member="sections")
                    cached = ((published or {}).get("data") or {}).get(section)
                    if isinstance(cached, dict) and cached.get("rows") is not None:
                        return json(cached)
                    from domain.investments.returns.capital_return_series import build_asset_type_capital_return_bundle
                    histories = {}
                    _verified_nav_weekly(cur, latest=True, start_date=None, interval="daily", baseCurrency=baseCurrency, account_histories=histories)
                    view_as_of = latest_position_valuation_date(
                        cur, holding_date=_investment_return_view_as_of(cur), through=_investment_today_label_date()
                    )
                    bundle = build_asset_type_capital_return_bundle(
                        cur, start=start_date, end=view_as_of, base_currency=baseCurrency,
                        owner_ratio=_account_owner_ratio, excluded_accounts=_excluded_holding_accounts(),
                        histories=histories,
                    )
                    return json(bundle[section])
                return json(build_section_curve(cur, section=section, start_date=start_date, interval=interval, baseCurrency=baseCurrency))

    def build_section_curve(cur, *, section, start_date, interval, baseCurrency, read_revision=_return_revision):
        view_as_of = _investment_return_view_as_of(cur)
        today_label_date = _investment_today_label_date()
        if section == "a_stock":
            view_as_of = latest_position_valuation_date(cur, holding_date=view_as_of, through=today_label_date)
        reported_returns_cfg = _reported_returns_cfg()
        snapshot_payload = _build_section_return_series_payload_from_snapshot(
            cur,
            section=section,
            start_date=start_date,
            interval=interval,
            base_currency=baseCurrency,
            view_as_of=view_as_of,
            today_label_date=today_label_date,
            require_data_completeness=True,
            current_revision=lambda: read_revision(
                cur, start_date=start_date, interval=interval, view_as_of=view_as_of, today_label_date=today_label_date,
                reported_returns_cfg=reported_returns_cfg, section=section,
            ),
        )
        payload = snapshot_payload
        if payload is None:
            payload = _build_section_return_series_payload(
                cur,
                section=section,
                start_date=start_date,
                interval=interval,
                base_currency=baseCurrency,
                view_as_of=view_as_of,
                reported_returns_cfg=reported_returns_cfg,
                overview_payload=lambda: accounts_endpoint._overview_payload(cur=cur, include_transfers=False),
                owner_ratio_case_sql=_owner_ratio_case_sql,
                account_owner_ratio=_account_owner_ratio,
                excluded_holding_accounts=_excluded_holding_accounts(),
                today_label_date=today_label_date,
                position_based=section == "a_stock",
            )
            if payload.get("method") not in {FUND_HISTORY_METHOD, POSITION_RETURN_METHOD}:
                payload = _apply_section_holdings_pnl_overlay(cur, section=section, payload=payload)
        if section == 'us_stock' and payload.get('fx_source') == OPEN_EXCHANGE_RATES_SOURCE and (
            payload.get('status') == 'available'
        ):
            return payload
        closed_returns = load_closed_trade_returns(
            cur,
            start=start_date, end=view_as_of, base_currency=baseCurrency.upper(),
            owner_ratio=_account_owner_ratio, sections={section},
            excluded_accounts=_excluded_holding_accounts(),
        )
        return attach_closed_trade_returns(payload, closed_returns.get(section))


    @router.post("/investment/section-return-series/snapshot")
    def refresh_investment_section_return_series_snapshot(
        section: str = Query(..., pattern="^(a_stock|a_fund|us_stock|precious_metal)$"),
        start: str = Query("2026-01-01"),
        interval: str = Query("daily", pattern="^(daily|weekly)$"),
        baseCurrency: str = "CNY",
        expected_through: Date | None = None,
    ):
        """Explicitly rebuild and persist a section return-series snapshot."""
        try:
            start_date = Date.fromisoformat(start)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="start must be YYYY-MM-DD") from exc
        if expected_through and (section != "us_stock" or baseCurrency.upper() != "CNY"):
            raise HTTPException(status_code=400, detail="Expected-through applies to the verified CNY US curve")
    
        try:
            with get_conn() as conn:
                conn.set_session(isolation_level='REPEATABLE READ')
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    view_as_of = _investment_return_view_as_of(cur)
                    today_label_date = _investment_today_label_date()
                    if section == "a_stock":
                        view_as_of = latest_position_valuation_date(cur, holding_date=view_as_of, through=today_label_date)
                    reported_returns_cfg = _reported_returns_cfg()
                    revision_args = dict(start_date=start_date, interval=interval, view_as_of=view_as_of,
                                         today_label_date=today_label_date, reported_returns_cfg=reported_returns_cfg,
                                         section=section)
                    input_revision = _return_revision(cur, **revision_args)
                    payload = _build_section_return_series_payload(
                        cur,
                        section=section,
                        start_date=start_date,
                        interval=interval,
                        base_currency=baseCurrency,
                        view_as_of=view_as_of,
                        reported_returns_cfg=reported_returns_cfg,
                        overview_payload=lambda: accounts_endpoint._overview_payload(cur=cur, include_transfers=False),
                        owner_ratio_case_sql=_owner_ratio_case_sql,
                        account_owner_ratio=_account_owner_ratio,
                        excluded_holding_accounts=_excluded_holding_accounts(),
                        today_label_date=today_label_date,
                        position_based=section == "a_stock",
                    )
                    if payload.get("method") not in {FUND_HISTORY_METHOD, POSITION_RETURN_METHOD}:
                        payload = _apply_section_holdings_pnl_overlay(cur, section=section, payload=payload)
                    if expected_through and (
                        payload.get("fx_source") != OPEN_EXCHANGE_RATES_SOURCE
                        or payload.get("source_through") != expected_through.isoformat()
                    ):
                        return json({"status": "blocked", "source_through": payload.get("source_through"),
                                     "missing_fx_date": payload.get("missing_fx_date")})
                    if input_revision != _return_revision(cur, **revision_args):
                        raise HTTPException(status_code=409, detail="收益输入已更新，请重试")
                    written = _upsert_investment_return_series_snapshot(
                        cur,
                        payload=payload,
                        base_currency=baseCurrency,
                        section=section,
                        input_revision=input_revision,
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
                "source_through": payload.get("source_through"),
            }
        )


    def _verified_nav_weekly(cur, *, start_date, baseCurrency, latest, interval, account_histories=None):
        current_view_as_of = _investment_return_view_as_of(cur) if latest else ANCHOR_NAV_DATE
        account_cfg = _load_portfolio_accounts_config()
        account_balances = _load_account_balances(cur, baseCurrency, account_cfg)
        payload = accounts_endpoint._overview_payload(
            cur=cur, include_transfers=False,
            valuation_balances=account_balances if baseCurrency == 'CNY' else None,
        )
        investment_positions = _load_investment_positions(
            cur, baseCurrency, account_cfg, account_balances=account_balances)
        published_state = (
            _latest_published_portfolio_valuation_state(cur, base_currency=baseCurrency)
            if latest
            else None
        )
        published_state_fresh = bool(
            published_state
            and _is_published_valuation_fresh_for_date(published_state, current_view_as_of)
            and _published_daily_close_compatible(published_state, account_balances=account_balances)
            and published_bank_balances_compatible(published_state, account_balances)
        )
        terminal_date = (
            published_state["valuation_date"]
            if published_state_fresh and published_state and published_state.get("valuation_date")
            else current_view_as_of
        )
        if latest and not published_state_fresh:
            current_as_of_dates = [
                str(item.get("as_of") or "")[:10]
                for item in [*account_balances, *investment_positions]
                if item.get("as_of")
            ]
            if current_as_of_dates:
                terminal_date = Date.fromisoformat(max(current_as_of_dates))
        return _build_nav_weekly_payload(
                cur,
                start_date=start_date if start_date is not None else min(period_starts(terminal_date).values()),
                base_currency=baseCurrency,
                overview_payload=payload,
                owner_ratio_case_sql=_owner_ratio_case_sql,
                account_owner_ratio=_account_owner_ratio,
                terminal_date=terminal_date,
                full_personal_portfolio=True,
                account_histories=account_histories, evidence_through=accounts_endpoint._view_as_of(),
                account_config=account_cfg,
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
            start_date = None if start == "overview" else Date.fromisoformat(start)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="start must be YYYY-MM-DD or overview") from exc
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
                return json(build_total_curve(cur, start_date=start_date, interval=interval, baseCurrency=baseCurrency))

    def build_total_curve(cur, *, start_date, interval, baseCurrency, read_revision=_return_revision):
        from domain.investments.returns.closed_return_display import closed_return_summary
        today_label_date = _investment_today_label_date()
        view_as_of = latest_position_valuation_date(
            cur, holding_date=_investment_return_view_as_of(cur), through=today_label_date,
        )
        if start_date > view_as_of:
            start_date = view_as_of
        def with_closed_wealth(payload):
            lifetime = load_closed_trade_returns(
                cur, start=Date.min, end=view_as_of, base_currency=baseCurrency.upper(),
                owner_ratio=_account_owner_ratio, sections={'wealth_management'},
                excluded_accounts=_excluded_holding_accounts(),
            ).get('wealth_management')
            period = load_closed_trade_returns(
                cur, start=start_date, end=view_as_of, base_currency=baseCurrency.upper(),
                owner_ratio=_account_owner_ratio, sections={'wealth_management'},
                excluded_accounts=_excluded_holding_accounts(),
            ).get('wealth_management') if lifetime else None
            summary = closed_return_summary(lifetime, start=start_date, period_closed=period)
            if summary and any((row.get('estimated_positions') or 0) > 0 for row in payload.get('rows', [])):
                summary['curve_has_estimated_positions'] = True
                # Some estimated positions are present; aggregate coverage of
                # every closed cycle is not established by this flag.
                summary['included_in_return_curve'] = None
            return {**payload, 'closed_wealth_returns': summary} if summary else payload
        reported_returns_cfg = _reported_returns_cfg()
        snapshot_payload = _build_investment_return_series_payload_from_snapshot(
            cur,
            start_date=start_date,
            interval=interval,
            base_currency=baseCurrency,
            view_as_of=view_as_of,
            today_label_date=today_label_date,
            require_data_completeness=True,
            current_revision=lambda: read_revision(
                cur, start_date=start_date, interval=interval, view_as_of=view_as_of, today_label_date=today_label_date,
                reported_returns_cfg=reported_returns_cfg,
            ),
        )
        if snapshot_payload is not None:
            return with_closed_wealth(snapshot_payload)
        payload = _build_investment_return_series_payload(
            cur,
            start_date=start_date,
            interval=interval,
            base_currency=baseCurrency,
            view_as_of=view_as_of,
            overview_payload=lambda: accounts_endpoint._overview_payload(cur=cur, include_transfers=False),
            reported_returns_cfg=reported_returns_cfg,
            owner_ratio_case_sql=_owner_ratio_case_sql,
            account_owner_ratio=_account_owner_ratio,
            account_initial_capital_cny=_account_initial_capital_cny,
            excluded_holding_accounts=_excluded_holding_accounts(),
            today_label_date=today_label_date,
            position_based=True,
        )
        if payload.get("method") == POSITION_RETURN_METHOD:
            return with_closed_wealth(payload)
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
        return with_closed_wealth(_apply_summary_pnl_to_return_series_payload(payload_with_valuation, summary_payload))


    @router.get("/api/assets/monthly-overview")
    def asset_monthly_overview():
        from domain.investments.valuation.monthly_overview import build_monthly_overview
        with get_conn() as conn:
            conn.set_session(readonly=True, isolation_level="REPEATABLE READ")
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                report = build_monthly_overview(cur, today=_investment_today_label_date(),
                                                owner_ratio=_account_owner_ratio,
                                                excluded_accounts=_excluded_holding_accounts())
                return json(jsonable_encoder(report))

    @router.get("/api/investment/analysis-view")
    def investment_analysis_view():
        with get_conn() as conn:
            conn.set_session(readonly=True, isolation_level="REPEATABLE READ")
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                published = page_projection.read_page(cur, "analysis", business_date=_investment_today_label_date())
                return json(published) if published is not None else json({}, status_code=404)

    def build_investment_pages(cur, business_date):
        start = Date.fromisoformat(page_projection.PAGE_START)
        curve_args = dict(start_date=start, interval="daily", baseCurrency="CNY")
        revisions = None

        def read_revision(cur, **settings):
            nonlocal revisions
            if revisions is None:
                revisions = return_input_revisions(cur)
            return _return_revision(cur, **settings, revisions=revisions)

        histories = {}
        nav = _verified_nav_weekly(cur, latest=True, start_date=None, interval="daily", baseCurrency="CNY", account_histories=histories)
        balances = accounts_endpoint._norm(current_balances(histories))
        holdings = build_holdings_page(cur, show_closed=False, account_balances=balances)
        card_bals = account_center_balances(
            holdings["account_positions"], holdings["funds"], holdings["account_cards"])
        balances = accounts_endpoint._norm(merge_account_center_balances(balances, card_bals))
        from domain.investments.returns.capital_return_series import build_asset_type_capital_return_bundle

        legacy_total = build_total_curve(cur, **curve_args, read_revision=read_revision)
        view_as_of = latest_position_valuation_date(
            cur, holding_date=_investment_return_view_as_of(cur), through=business_date
        )
        capital_bundle = build_asset_type_capital_return_bundle(
            cur,
            start=start,
            end=view_as_of,
            base_currency="CNY",
            owner_ratio=_account_owner_ratio,
            excluded_accounts=_excluded_holding_accounts(),
            histories=histories,
        )
        total_curve = capital_bundle["total"] if capital_bundle["total"].get("status") == "available" else legacy_total
        if isinstance(legacy_total, dict) and legacy_total.get("closed_wealth_returns") is not None:
            total_curve = {**total_curve, "closed_wealth_returns": legacy_total["closed_wealth_returns"]}
        legacy_sections = {
            section: build_section_curve(cur, section=section, **curve_args, read_revision=read_revision)
            for section in ("a_stock", "a_fund", "us_stock", "precious_metal")
        }
        sections_payload = {
            **legacy_sections,
            "stocks": capital_bundle["stocks"],
            "a_fund": capital_bundle["a_fund"] if capital_bundle["a_fund"].get("status") == "available" else legacy_sections["a_fund"],
            "precious_metal": capital_bundle["precious_metal"] if capital_bundle["precious_metal"].get("status") == "available" else legacy_sections["precious_metal"],
            "wealth": capital_bundle["wealth"],
        }
        pages = {
            "account_histories": accounts_endpoint._norm(histories),
            "account_balances": balances,
            "holdings": {
                **holdings,
                "fx_rates": build_latest_fx_rates_payload(cur, base_currency="CNY"),
                "valuation_state": read_current_portfolio_valuation(cur),
            },
            "analysis": {
                # The opening observation is the previous year's close; keep
                # its actual date rather than treating it as a January 1 close.
                "nav": nav,
                "total": total_curve,
                "sections": sections_payload,
            },
        }

        pages["performance_summary"] = page_projection.performance_summary(cur, business_date, pages)
        return pages

    @router.post("/api/investment/page-projection/refresh")
    def refresh_investment_pages(expected_through: Date | None = None):
        with get_conn() as conn:
            conn.set_session(isolation_level="REPEATABLE READ")
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                day = _investment_today_label_date()
                return json(page_projection.refresh(
                    cur, business_date=day,
                    build=lambda read: build_investment_pages(read, day), connect=get_conn, expected_through=expected_through,
                ))


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
                    today_label_date = _investment_today_label_date()
                    view_as_of = latest_position_valuation_date(
                        cur, holding_date=_investment_return_view_as_of(cur), through=today_label_date,
                    )
                    if start_date > view_as_of:
                        start_date = view_as_of
                    reported_returns_cfg = _reported_returns_cfg()
                    revision_args = dict(start_date=start_date, interval=interval, view_as_of=view_as_of,
                                         today_label_date=today_label_date, reported_returns_cfg=reported_returns_cfg)
                    input_revision = _return_revision(cur, **revision_args)
                    payload = _build_investment_return_series_payload(
                        cur,
                        start_date=start_date,
                        interval=interval,
                        base_currency=baseCurrency,
                        view_as_of=view_as_of,
                        overview_payload=lambda: accounts_endpoint._overview_payload(cur=cur, include_transfers=False),
                        reported_returns_cfg=reported_returns_cfg,
                        owner_ratio_case_sql=_owner_ratio_case_sql,
                        account_owner_ratio=_account_owner_ratio,
                        account_initial_capital_cny=_account_initial_capital_cny,
                        excluded_holding_accounts=_excluded_holding_accounts(),
                        today_label_date=today_label_date,
                        position_based=True,
                    )
                    if payload.get("method") != POSITION_RETURN_METHOD:
                        valuation_state = _latest_published_portfolio_valuation_state(cur, base_currency=baseCurrency)
                        payload = _apply_published_valuation_state_to_return_series_payload(payload, valuation_state)
                        payload = _apply_summary_pnl_to_return_series_payload(payload, _build_summary2_latest_payload_for_request(cur))
                    if input_revision != _return_revision(cur, **revision_args):
                        raise HTTPException(status_code=409, detail="收益输入已更新，请重试")
                    written = _upsert_investment_return_series_snapshot(
                        cur,
                        payload=payload,
                        base_currency=baseCurrency,
                        input_revision=input_revision,
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


    return router, (summary2_latest, holdings_latest_compat, holdings_by_category, holdings_history_by_category, nav_series, investment_section_return_series, refresh_investment_section_return_series_snapshot, nav_weekly, investment_return_series, refresh_investment_return_series_snapshot, nav_drawdown, cashflow_series, risk_latest, build_investment_pages)
