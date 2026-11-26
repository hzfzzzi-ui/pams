"""
市场行情刷新编排层。
外部报价采集位于 domain.market.price_providers，快照与交易 roll-forward 位于 domain.market.price_snapshots。
"""
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

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
        close_rows = _upsert_yfinance_closes(holdings)
        if price_basis == "estimate":
            prices, skipped_estimates = _filter_us_estimate_prices_for_holdings(holdings, prices)
            priced_symbols = [str(sym) for sym in symbols if sym in prices or str(sym).upper() in prices]
            estimate_rows = _upsert_us_estimate_market_daily(holdings, prices, target_as_of)
            quote_sources = _estimate_quote_source_summary(priced_symbols)
        if price_basis == "close":
            prices = _latest_stored_yfinance_prices(symbols, as_of=target_as_of, fallback=prices)
            if require_current_close:
                # Do not stamp Friday's last close as Monday's confirmed quote.
                current_symbols = {sym for sym in symbols for trade_date, _ in _YFINANCE_CLOSE_CACHE.get(str(sym), [])
                                   if trade_date == target_as_of}
                prices = {sym: value for sym, value in prices.items() if sym in current_symbols}

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
    updated = len(updates)
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
    elif category == _OVERSEAS_MAJOR_CATEGORY:
        daily_quote_rows = _upsert_scheduled_us_quotes(holdings, prices, target_as_of, price_basis)
        updated = daily_quote_rows

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


