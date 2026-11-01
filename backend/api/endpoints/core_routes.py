"""API orchestration for account, holdings and FX compatibility routes."""
from typing import Optional
import psycopg2.extras
from fastapi import APIRouter, HTTPException, Query


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
    def holdings_by_account(base_currency: str = Query("CNY")):
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
                    )
                )


    @router.get("/api/fx-rates/latest")
    def get_latest_fx_rates(base_currency: str = Query("CNY")):
        """获取最新汇率"""
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                return json(_build_latest_fx_rates_payload(cur, base_currency=base_currency))


    @router.post("/api/fx-rates/refresh")
    def refresh_fx_rates():
        """从公开API获取最新汇率并存储双向汇率"""
        from domain.imports.isolation import source_mode
        from fastapi import HTTPException
        if source_mode() == "development":
            raise HTTPException(status_code=409, detail="External synchronization is disabled in development mode")
        import urllib.request
        import json as json_mod
        from datetime import date as d_date
    
        today = d_date.today()
        currencies = ['CNY', 'USD', 'MXN', 'HKD', 'KRW', 'SEK']
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


    return router, (get_accounts, get_account_detail, get_categories, holdings_latest, holdings_summary, holdings_by_account, get_latest_fx_rates, refresh_fx_rates)
