"""
价格刷新与行情查询 HTTP 端点编排层。
核心采集、估值与快照刷新业务逻辑位于 domain.market.price_refresh。
"""
from datetime import date, datetime, timedelta
import json as json_mod
import urllib.request
from typing import Any, List, Optional
from zoneinfo import ZoneInfo

import psycopg2.extras
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from domain.brokers.ibkr.ibkr_report_state import expected_statement_date
from domain.market.fund_nav_estimates import refresh_fund_nav_estimates as _refresh_fund_nav_estimates
from domain.market.price_history import (
    build_price_series_payload,
    ensure_price_history,
    normalize_price_instrument,
    parse_symbol_list,
    valid_holding_history_window,
)
from domain.market.price_refresh import (
    _OVERSEAS_MAJOR_CATEGORY,
    _fetch_a_stock_prices,
    _fetch_crypto_prices,
    _fetch_precious_metal_prices,
    _fetch_us_stock_estimate_prices,
    _fetch_us_stock_prices,
    _get_conn,
    _latest_investment_snapshot_as_of,
    _normalize_overseas_market_filter,
    _price_refresh_as_of,
    _refresh_category,
    refresh_benchmark_closes,
    _refresh_scheduled_overseas_quotes,
    _today_cst,
    logger,
)

router = APIRouter()


class InvestmentPriceRefreshRequest(BaseModel):
    """Manual public-market refresh request for investment holdings."""

    scopes: List[str] = Field(default_factory=lambda: ["a_stock", "us_stock", "a_fund", "precious_metal"])
    fund_use_intraday_estimate: Optional[bool] = None
    overseas_price_basis: Optional[str] = None


class PriceHistoryBackfillRequest(BaseModel):
    """Persist instrument price history for analysis curves and trade review."""

    symbols: List[str] = Field(default_factory=list)
    benchmarks: List[str] = Field(default_factory=list)
    start: date
    end: Optional[date] = None
    market: Optional[str] = None


def _json(data: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        content=data,
        status_code=status_code,
        media_type="application/json; charset=utf-8",
    )


# ---------- API端点 ----------

def _default_price_start(end_date: date) -> date:
    return date(end_date.year, 1, 1)


def _price_request_instruments(
    *,
    symbols: str | List[str] | None = None,
    benchmarks: str | List[str] | None = None,
    market: Optional[str] = None,
):
    instruments = [
        normalize_price_instrument(symbol, market=market)
        for symbol in parse_symbol_list(symbols)
    ]
    instruments.extend(
        normalize_price_instrument(symbol, benchmark=True)
        for symbol in parse_symbol_list(benchmarks)
    )
    return instruments


@router.get("/prices/freshness")
def get_prices_freshness():
    """Return stale-quote alerts (>2 trading days) and Tonghuashun post-snapshot trade share projections."""
    from domain.market.freshness_guard import build_holdings_freshness_and_projection
    with _get_conn() as conn:
        conn.set_session(readonly=True)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            return _json(build_holdings_freshness_and_projection(cur, today=_today_cst()))


@router.get("/prices/holding-benchmark")
def get_holding_benchmark(start: date, end: date):
    from domain.market.benchmark_history import benchmark_history, local_benchmark
    if not valid_holding_history_window(start, end):
        return _json({"error": "无效的日期范围"}, status_code=400)
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            closes = local_benchmark(cur, start, end)
    return _json(benchmark_history(closes, start, end, _today_cst()))

@router.get("/prices/series")
def get_price_series(
    symbols: str = "",
    benchmarks: str = "",
    start: Optional[date] = None,
    end: Optional[date] = None,
    normalize: bool = True,
    market: Optional[str] = None,
):
    """Read locally stored price curves for instruments and benchmarks."""
    end_date = end or _today_cst()
    start_date = start or _default_price_start(end_date)
    try:
        instruments = _price_request_instruments(symbols=symbols, benchmarks=benchmarks, market=market)
    except ValueError as exc:
        return _json({"status": "error", "error": str(exc)}, status_code=400)
    if not instruments:
        return _json(
            {
                "start": start_date.isoformat(),
                "end": end_date.isoformat(),
                "normalize": normalize,
                "series": [],
            }
        )
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            payload = build_price_series_payload(
                cur,
                instruments=instruments,
                start=start_date,
                end=end_date,
                normalize=normalize,
            )
    return _json(payload)


@router.post("/prices/history/backfill")
def backfill_price_history(body: PriceHistoryBackfillRequest):
    """Persist supported external price history into local OHLCV storage."""
    from domain.imports.isolation import source_mode
    from fastapi import HTTPException
    if source_mode() == "development":
        raise HTTPException(status_code=409, detail="External synchronization is disabled in development mode")
    end_date = body.end or _today_cst()
    try:
        instruments = _price_request_instruments(
            symbols=body.symbols,
            benchmarks=body.benchmarks,
            market=body.market,
        )
    except ValueError as exc:
        return _json({"status": "error", "error": str(exc)}, status_code=400)
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            backfill = ensure_price_history(cur, instruments=instruments, start=body.start, end=end_date)
            payload = build_price_series_payload(
                cur,
                instruments=instruments,
                start=body.start,
                end=end_date,
                normalize=True,
            )
        conn.commit()
    return _json({"status": "ok", "backfill": backfill, "series": payload})


@router.get("/prices/trade-context")
def get_trade_price_context(
    symbol: str,
    tradeDate: date,
    market: Optional[str] = None,
    benchmarks: str = "",
    lookbackDays: int = 20,
    lookaheadDays: int = 20,
    normalize: bool = True,
):
    """Read a trade-centered price window for future transaction review."""
    lookback = max(0, min(int(lookbackDays), 365))
    lookahead = max(0, min(int(lookaheadDays), 365))
    start_date = tradeDate - timedelta(days=lookback)
    end_date = min(_today_cst(), tradeDate + timedelta(days=lookahead))
    try:
        instruments = _price_request_instruments(symbols=[symbol], benchmarks=benchmarks, market=market)
    except ValueError as exc:
        return _json({"status": "error", "error": str(exc)}, status_code=400)
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            payload = build_price_series_payload(
                cur,
                instruments=instruments,
                start=start_date,
                end=end_date,
                normalize=normalize,
            )
    payload["trade_date"] = tradeDate.isoformat()
    payload["lookback_days"] = lookback
    payload["lookahead_days"] = lookahead
    return _json(payload)


@router.post("/prices/refresh/benchmarks")
def refresh_benchmarks():
    return refresh_benchmark_closes()


@router.post("/prices/refresh/a-stock")
def refresh_a_stock():
    """刷新A股价格"""
    from domain.imports.isolation import source_mode
    from fastapi import HTTPException
    if source_mode() == "development":
        raise HTTPException(status_code=409, detail="External synchronization is disabled in development mode")
    result = _refresh_category("A股", _fetch_a_stock_prices, exclude_minor_categories=("A股基金",))
    from domain.investments.returns.ths_nav_history_refresh import refresh_history
    try:
        history = refresh_history(_get_conn, now=datetime.now(ZoneInfo("Asia/Shanghai")))
    except (OSError, ValueError, psycopg2.Error) as exc:
        logger.warning("Tencent NAV history refresh failed: %s", type(exc).__name__)
        history = {"status": "error", "rows": 0}
    result["nav_history"] = history
    if history["status"] in {"error", "incomplete"}:
        result["errors"].append("tencent_nav_history_incomplete")
    return _json(result)


@router.post("/prices/refresh/us-stock")
def refresh_us_stock(
    market: Optional[str] = Query(None),
    price_basis: str = Query("close", alias="priceBasis"),
    scheduled: bool = Query(False),
):
    """刷新美股价格"""
    from domain.imports.isolation import source_mode
    from fastapi import HTTPException
    if source_mode() == "development":
        raise HTTPException(status_code=409, detail="External synchronization is disabled in development mode")
    if not isinstance(market, str):
        market = None
    if not isinstance(price_basis, str):
        price_basis = "close"
    if price_basis not in {"close", "estimate"}:
        price_basis = "close"
    market_buckets = _normalize_overseas_market_filter(market)
    if scheduled is True:
        if len(market_buckets) == 1 and market_buckets[0] in {"hk", "kr", "se", "ch"}:
            from domain.market.overseas_return_calendar import latest_completed_session

            bucket = market_buckets[0]
            completed = latest_completed_session(bucket)
            if completed is None:
                return _json({"status": "skipped", "reason": "no_completed_overseas_session", "market": bucket})
            return _json(_refresh_scheduled_overseas_quotes(bucket, completed))
        now = datetime.now(ZoneInfo("America/New_York"))
        target = _price_refresh_as_of(_OVERSEAS_MAJOR_CATEGORY)
        session_date = now.date() + timedelta(days=now.hour >= 20)
        if market != "us" or target != session_date or expected_statement_date(target) != target:
            return _json({"status": "skipped", "reason": "outside_us_quote_session", "as_of": str(target)})
        # Official statements may arrive later; quote refresh never waits for Flex.
        price_basis = "close" if target == now.date() and (now.hour, now.minute) >= (16, 30) else "estimate"
    fetch_fn = _fetch_us_stock_estimate_prices if price_basis == "estimate" else _fetch_us_stock_prices
    scheduled_options: dict[str, Any] = {"require_current_close": True} if scheduled is True else {}
    result = _refresh_category(
        "美股",
        fetch_fn,
        market_buckets=market_buckets,
        price_basis=price_basis,
        **scheduled_options,
    )
    return _json(result)


@router.post("/prices/refresh/crypto")
def refresh_crypto():
    """刷新加密货币价格"""
    from domain.imports.isolation import source_mode
    from fastapi import HTTPException
    if source_mode() == "development":
        raise HTTPException(status_code=409, detail="External synchronization is disabled in development mode")
    result = _refresh_category("加密", _fetch_crypto_prices)
    return _json(result)


@router.post("/prices/refresh/precious-metal")
def refresh_precious_metal():
    """刷新贵金属价格（黄金积存金优先使用本地 SGE 现货金价）"""
    from domain.imports.isolation import source_mode
    from fastapi import HTTPException
    if source_mode() == "development":
        raise HTTPException(status_code=409, detail="External synchronization is disabled in development mode")
    result = _refresh_category("贵金属", _fetch_precious_metal_prices)
    return _json(result)


@router.post("/prices/refresh/fund-nav-estimates")
def refresh_fund_nav_estimates(
    use_intraday_estimate: Optional[bool] = Query(None, alias="useIntradayEstimate"),
):
    """Fetch A-share fund confirmed NAVs and post-close estimated NAVs."""
    from domain.imports.isolation import source_mode
    from fastapi import HTTPException
    if source_mode() == "development":
        raise HTTPException(status_code=409, detail="External synchronization is disabled in development mode")
    if not isinstance(use_intraday_estimate, bool):
        use_intraday_estimate = None
    with _get_conn() as conn:
        result = _refresh_fund_nav_estimates(conn, use_intraday_estimates=use_intraday_estimate)
    return _json(result)


@router.post("/prices/refresh/fund-nav-history")
def refresh_fund_nav_history(start: date, preview: bool = True):
    """Preview or explicitly archive historical unit NAVs for portfolio funds."""
    from fastapi import HTTPException
    from domain.imports.isolation import source_enabled, source_mode
    from domain.investments.returns.fund_return_history import load_fund_history_inputs
    from domain.market.fund_nav_archive import archive_fund_nav_history
    from domain.market.fund_nav_estimates import current_china_fund_date

    if source_mode() == "development":
        raise HTTPException(status_code=409, detail="External synchronization is disabled in development mode")
    end = current_china_fund_date()
    if not end - timedelta(days=366) <= start <= end:
        raise HTTPException(status_code=400, detail="Start must be within the last 366 days")
    with _get_conn() as conn:
        conn.set_session(readonly=preview)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if not source_enabled(cur):
                raise HTTPException(status_code=409, detail="Fund trade source evidence is unavailable")
            assets, _, _ = load_fund_history_inputs(cur, end=end, include_trades=False)
        return _json(archive_fund_nav_history(conn, assets=assets, start=start, end=end, preview=preview))


def _normalize_investment_refresh_scopes(scopes: List[str]) -> List[str]:
    aliases = {
        "all": ["a_stock", "us_stock", "a_fund", "precious_metal"],
        "a-share": ["a_stock"],
        "ashare": ["a_stock"],
        "fund": ["a_fund"],
        "funds": ["a_fund"],
        "gold": ["precious_metal"],
        "metal": ["precious_metal"],
        "metals": ["precious_metal"],
        "precious": ["precious_metal"],
        "us": ["us_stock"],
        "us-only": ["us_only"],
        "usa": ["us_only"],
        "hk": ["hk_stock"],
        "hong-kong": ["hk_stock"],
        "hongkong": ["hk_stock"],
        "kr": ["kr_stock"],
        "korea": ["kr_stock"],
    }
    normalized: List[str] = []
    for raw in scopes or ["a_stock", "us_stock", "a_fund", "precious_metal"]:
        key = str(raw or "").strip().lower().replace("-", "_")
        if key in aliases:
            for item in aliases[key]:
                if item not in normalized:
                    normalized.append(item)
            continue
        if key in {"a_stock", "us_stock", "us_only", "hk_stock", "kr_stock", "a_fund", "precious_metal"} and key not in normalized:
            normalized.append(key)
    return normalized or ["a_stock", "us_stock", "a_fund", "precious_metal"]


@router.post("/prices/refresh/investments")
def refresh_investment_prices(body: InvestmentPriceRefreshRequest | None = None):
    """Refresh public market values for investment holdings.

    This endpoint only updates local valuation snapshots from public data
    sources. It does not connect to brokers, place orders, exchange currency, or
    change transactions.
    """
    from domain.imports.isolation import source_mode
    from fastapi import HTTPException
    if source_mode() == "development":
        raise HTTPException(status_code=409, detail="External synchronization is disabled in development mode")
    req = body or InvestmentPriceRefreshRequest()
    requested_scopes = _normalize_investment_refresh_scopes(req.scopes)
    results: List[dict] = []

    for scope in requested_scopes:
        try:
            if scope == "a_stock":
                detail = _refresh_category("A股", _fetch_a_stock_prices, exclude_minor_categories=("A股基金",))
                label = "A股个股"
            elif scope == "us_stock":
                price_basis = req.overseas_price_basis if req.overseas_price_basis in {"close", "estimate"} else "close"
                fetch_fn = _fetch_us_stock_estimate_prices if price_basis == "estimate" else _fetch_us_stock_prices
                detail = _refresh_category("美股", fetch_fn, price_basis=price_basis)
                label = "美股/海外"
            elif scope in {"us_only", "hk_stock", "kr_stock"}:
                price_basis = req.overseas_price_basis if req.overseas_price_basis in {"close", "estimate"} else "close"
                fetch_fn = _fetch_us_stock_estimate_prices if price_basis == "estimate" else _fetch_us_stock_prices
                bucket = {"us_only": "us", "hk_stock": "hk", "kr_stock": "kr"}[scope]
                detail = _refresh_category(
                    "美股",
                    fetch_fn,
                    market_buckets=(bucket,),
                    price_basis=price_basis,
                )
                label = {"us_only": "美股", "hk_stock": "港股", "kr_stock": "韩股"}[scope]
            elif scope == "a_fund":
                with _get_conn() as conn:
                    detail = _refresh_fund_nav_estimates(
                        conn,
                        use_intraday_estimates=req.fund_use_intraday_estimate,
                    )
                label = "A股基金"
            elif scope == "precious_metal":
                detail = _refresh_category("贵金属", _fetch_precious_metal_prices)
                label = "贵金属"
            else:
                continue
            errors = detail.get("errors") or []
            results.append(
                {
                    "scope": scope,
                    "label": label,
                    "success": not bool(errors),
                    "detail": detail,
                    "errors": errors,
                }
            )
        except Exception as exc:  # pragma: no cover - network/runtime guard
            results.append(
                {
                    "scope": scope,
                    "label": {
                        "a_stock": "A股个股",
                        "us_stock": "美股/海外",
                        "us_only": "美股",
                        "hk_stock": "港股",
                        "kr_stock": "韩股",
                        "a_fund": "A股基金",
                        "precious_metal": "贵金属",
                    }.get(scope, scope),
                    "success": False,
                    "detail": {},
                    "errors": [str(exc)],
                }
            )

    latest_as_of = _latest_investment_snapshot_as_of()
    total_found = sum(int((r.get("detail") or {}).get("found") or (r.get("detail") or {}).get("processed") or 0) for r in results)
    total_updated = 0
    for item in results:
        detail = item.get("detail") or {}
        total_updated += int(detail.get("updated") or 0)
        if "valuation_updates" in detail:
            total_updated += int(detail["valuation_updates"])
        else:
            total_updated += int(detail.get("inserted_estimate_snapshots") or 0)
            total_updated += int(detail.get("corrected_confirmed_snapshots") or 0)
    return _json(
        {
            "status": "ok" if all(r.get("success") for r in results) else "partial",
            "as_of": latest_as_of.isoformat() if hasattr(latest_as_of, "isoformat") else latest_as_of,
            "requested_scopes": requested_scopes,
            "total_found": total_found,
            "total_updated": total_updated,
            "results": results,
            "note": "仅刷新公开行情/净值和本地估值快照，不执行交易。",
        }
    )


@router.post("/prices/refresh")
def refresh_all():
    """刷新全部价格（FX + A股 + 美股 + 加密）"""
    from domain.imports.isolation import source_mode
    from fastapi import HTTPException
    if source_mode() == "development":
        raise HTTPException(status_code=409, detail="External synchronization is disabled in development mode")
    results = []

    # FX rates (调用已有端点逻辑)
    try:
        fx_url = "http://localhost:8000/api/fx-rates/refresh"
        req = urllib.request.Request(fx_url, method="POST",
                                     headers={"User-Agent": "PAMS/2.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            fx_result = json_mod.loads(resp.read().decode())
        results.append({"category": "FX", "success": True, "detail": fx_result})
    except Exception as e:
        results.append({"category": "FX", "success": False, "error": str(e)})

    # 各资产类别
    for category, fetch_fn, exclude_minor_categories in [
        ("A股", _fetch_a_stock_prices, ("A股基金",)),
        ("美股", _fetch_us_stock_prices, ()),
        ("加密", _fetch_crypto_prices, ()),
        ("贵金属", _fetch_precious_metal_prices, ()),
    ]:
        result = _refresh_category(category, fetch_fn, exclude_minor_categories=exclude_minor_categories)
        results.append(result)

    return _json({"results": results})
