"""
市场行情刷新编排层。
外部报价采集位于 domain.market.price_providers，快照与交易 roll-forward 位于 domain.market.price_snapshots。
"""
from contextlib import closing
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from psycopg2.extras import RealDictCursor, execute_values

from domain.market.benchmark_history import BENCHMARKS, benchmark_session, missing_index_closes
from domain.market.price_history import normalize_price_instrument
from domain.market.price_providers import (
    _A_STOCK_FUND_MINOR_CATEGORY,
    _A_STOCK_MAJOR_CATEGORY,
    _OVERSEAS_MAJOR_CATEGORY,
    _YFINANCE_CLOSE_CACHE,
    _fetch_a_stock_prices,
    _fetch_crypto_prices,
    _fetch_precious_metal_prices,
    _fetch_sge_au9999_history,
    _fetch_us_stock_estimate_prices,
    _fetch_us_stock_prices,
    _get_conn,
    _is_fund_code,
    _upsert_gold_spot_closes,
    logger,
)
from domain.market.price_snapshots import (
    _apply_same_day_a_stock_trades,
    _completed_yfinance_closes,
    _overseas_market_bucket,
    _estimate_quote_source_summary,
    _filter_us_estimate_prices_for_holdings,
    _get_holdings_with_symbol,
    _latest_investment_snapshot_as_of,
    _latest_stored_yfinance_prices,
    _normalize_overseas_market_filter,
    _price_refresh_as_of,
    _refresh_scheduled_overseas_quotes,
    _today_cst,
    _upsert_cn_a_closes,
    _upsert_daily_return_quote_snapshots,
    _upsert_price_snapshots,
    _upsert_scheduled_us_quotes,
    _upsert_us_estimate_market_daily,
    _upsert_yfinance_closes,
)


# ---------- 通用刷新逻辑 ----------

def _refresh_category(
    category: str,
    fetch_fn,
    exclude_minor_categories: Tuple[str, ...] = (),
    market_buckets: Tuple[str, ...] = (),
    price_basis: str = "close",
    require_current_close: bool = False,
) -> dict:
    """刷新指定类别的价格"""
    holdings = _get_holdings_with_symbol(
        category,
        exclude_minor_categories=exclude_minor_categories,
        market_buckets=market_buckets,
    )
    if category == "贵金属":
        close_rows = _upsert_gold_spot_closes(_fetch_sge_au9999_history(_today_cst()))
        holdings = [holding for holding in holdings if _is_fund_code(holding["symbol"])]
    else:
        close_rows = 0
    if not holdings:
        return {
            "category": category,
            "found": 0,
            "updated": 0,
            "market_buckets": list(market_buckets),
            "price_basis": price_basis,
            "close_rows": close_rows,
            "errors": [],
        }

    target_as_of = _price_refresh_as_of(category, price_basis)
    if category == _A_STOCK_MAJOR_CATEGORY and _A_STOCK_FUND_MINOR_CATEGORY in set(exclude_minor_categories):
        holdings = _apply_same_day_a_stock_trades(holdings, target_as_of)
    same_day_trade_adjusted = sum(1 for h in holdings if h.get("same_day_trade_adjusted"))
    symbols = [h["symbol"] for h in holdings]
    prices = fetch_fn(symbols)
    estimate_rows = 0
    quote_sources: dict[str, int] = {}
    skipped_estimates: list[dict[str, Any]] = []
    if category == "美股":
        completed = _completed_yfinance_closes(holdings, _YFINANCE_CLOSE_CACHE)
        close_rows = _upsert_yfinance_closes(completed)
        if price_basis == "estimate":
            prices, skipped_estimates = _filter_us_estimate_prices_for_holdings(holdings, prices)
            priced_symbols = [str(sym) for sym in symbols if sym in prices or str(sym).upper() in prices]
            estimate_rows = _upsert_us_estimate_market_daily(holdings, prices, target_as_of)
            quote_sources = _estimate_quote_source_summary(priced_symbols)
        if price_basis == "close":
            prices = _latest_stored_yfinance_prices(symbols, as_of=target_as_of, fallback=prices)
            # A prior close or a still-open daily bar cannot confirm the requested US day.
            for holding in holdings:
                symbol = holding["symbol"]
                if require_current_close or _overseas_market_bucket(holding) == "us":
                    close = completed.get((symbol, target_as_of))
                    if close is None:
                        prices.pop(symbol, None)
                    else:
                        prices[symbol] = close

    updates = []
    errors = []
    for h in holdings:
        sym = h["symbol"]
        if sym not in prices:
            errors.append(f"{sym} ({h['name']}): 未获取到价格")
            continue
        new_price = prices[sym]
        qty = float(h["quantity"])
        new_mv = round(qty * new_price, 2)
        updates.append((new_price, new_mv, h["id"]))

    # Quote-only jobs must not turn a market subset into a complete account snapshot.
    daily_quote_rows = 0
    if category == "A股":
        close_rows = _upsert_cn_a_closes(holdings, prices, target_as_of)
        daily_quote_rows = _upsert_daily_return_quote_snapshots(
            holdings,
            prices,
            target_as_of,
            source="tencent_a_stock_close",
            market="CN_A",
        )
        updated = daily_quote_rows
    elif category == _OVERSEAS_MAJOR_CATEGORY:
        daily_quote_rows = _upsert_scheduled_us_quotes(holdings, prices, target_as_of, price_basis)
        updated = daily_quote_rows
    elif category == "加密":
        daily_quote_rows = _upsert_daily_return_quote_snapshots(
            holdings,
            prices,
            target_as_of,
            source="crypto_quote",
            market="CRYPTO",
            currency="USD",
        )
        updated = daily_quote_rows
    elif category == "贵金属":
        daily_quote_rows = _upsert_daily_return_quote_snapshots(
            holdings,
            prices,
            target_as_of,
            source="precious_metal_fund_nav",
            market="CN_FUND",
            currency="CNY",
        )
        updated = daily_quote_rows
    else:
        updated = 0

    return {
        "category": category,
        "source_as_of": str(holdings[0]["as_of"]) if holdings else None,
        "as_of": str(target_as_of),
        "found": len(holdings),
        "updated": updated,
        "same_day_trade_adjusted": same_day_trade_adjusted,
        "close_rows": close_rows,
        "estimate_rows": estimate_rows,
        "quote_sources": quote_sources,
        "skipped_estimates": skipped_estimates,
        "market_buckets": list(market_buckets),
        "price_basis": price_basis,
        "daily_quote_rows": daily_quote_rows,
        "valuation_projection_only": True,
        "errors": errors,
    }


def refresh_benchmark_closes():
    today = _today_cst()
    errors = []
    inserted = 0
    with closing(_get_conn()) as conn:
        for _, _, symbol, secid, bucket in BENCHMARKS:
            instrument = normalize_price_instrument(symbol, benchmark=True)
            end = benchmark_session(bucket, today)
            if end is None:
                errors.append(symbol)
                continue
            start = date(end.year, 1, 1)
            with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""SELECT max(trade_date) AS latest FROM pams_ohlcv_daily
                               WHERE symbol=%s AND market=%s AND close>0 AND trade_date BETWEEN %s AND %s""",
                            (instrument.symbol, instrument.market, start, end))
                latest = cur.fetchone()["latest"]
                try:
                    rows = missing_index_closes(latest, secid, bucket, start, end)
                except (OSError, ValueError):
                    logger.exception("Benchmark close unavailable: %s", symbol)
                    errors.append(symbol)
                    continue
                if rows:
                    written = execute_values(cur, """INSERT INTO pams_ohlcv_daily (symbol, market, trade_date, close)
                                   VALUES %s ON CONFLICT (symbol, market, trade_date) DO NOTHING RETURNING 1""",
                                   [(instrument.symbol, instrument.market, day, price) for day, price in rows.items()],
                                   fetch=True)
                    inserted += len(written)
                if latest != end and end.isoformat() not in rows:
                    errors.append(symbol)
    return {"inserted": inserted, "errors": errors}
