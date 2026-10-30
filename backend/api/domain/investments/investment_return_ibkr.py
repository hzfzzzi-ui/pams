"""IBKR stock-return reconstruction helpers.

This module owns Yahoo Finance symbol mapping, close-price fetching, and the
full-trade IBKR stock/ETF value reconstruction used by
``domain.investments.investment_returns``.  It does not assemble endpoint payloads.
"""

from __future__ import annotations

from datetime import date as Date, timedelta
from decimal import Decimal
from typing import Any

from domain.investments.investment_return_data import IBKR_EQUITY_MARKETS
from domain.investments.investment_return_math import dec, ibkr_return_effective_date
from domain.investments.valuation_nav import fx_rate_at


_YFINANCE_CLOSE_CACHE: dict[
    tuple[tuple[str, ...], str, str],
    tuple[dict[str, dict[Date, Decimal]], tuple[str, ...]],
] = {}
_ASSET_CATEGORY = "\u8d44\u4ea7"
_US_STOCK_CATEGORY = "\u7f8e\u80a1"


def _ibkr_yfinance_symbol(symbol: str, currency: str) -> str | None:
    """Map an IBKR symbol/currency pair to a Yahoo Finance ticker."""
    raw = str(symbol or "").strip()
    cur = str(currency or "").upper()
    if not raw or raw.startswith("USD."):
        return None
    if raw.endswith((".KS", ".HK")):
        return raw
    if cur == "HKD" and raw.isdigit():
        return f"{raw.zfill(4)}.HK"
    if cur == "KRW" and raw.isdigit():
        return f"{raw.zfill(6)}.KS"
    return raw


def _fetch_yfinance_closes(
    symbols: list[str],
    *,
    start_date: Date,
    end_date: Date,
) -> tuple[dict[str, dict[Date, Decimal]], list[str]]:
    """Fetch daily closes from Yahoo Finance for externally traded symbols."""
    tickers = sorted({s for s in symbols if s})
    if not tickers:
        return {}, []
    cache_key = (tuple(tickers), start_date.isoformat(), end_date.isoformat())
    if cache_key in _YFINANCE_CLOSE_CACHE:
        cached_closes, cached_warnings = _YFINANCE_CLOSE_CACHE[cache_key]
        return cached_closes, list(cached_warnings)
    try:
        import pandas as pd
        import yfinance as yf
    except Exception as exc:
        return {}, [f"yfinance unavailable: {exc}"]

    try:
        frame = yf.download(
            tickers,
            start=(start_date - timedelta(days=7)).isoformat(),
            end=(end_date + timedelta(days=1)).isoformat(),
            auto_adjust=False,
            progress=False,
            group_by="ticker",
            threads=True,
        )
    except Exception as exc:
        return {}, [f"yfinance download failed: {exc}"]

    closes: dict[str, dict[Date, Decimal]] = {}
    missing: list[str] = []
    for ticker in tickers:
        series = None
        if getattr(frame, "empty", True):
            series = None
        elif isinstance(frame.columns, pd.MultiIndex):
            if (ticker, "Close") in frame.columns:
                series = frame[(ticker, "Close")]
            elif ("Close", ticker) in frame.columns:
                series = frame[("Close", ticker)]
        elif len(tickers) == 1 and "Close" in frame.columns:
            series = frame["Close"]
        if series is None:
            try:
                single = yf.download(
                    ticker,
                    start=(start_date - timedelta(days=7)).isoformat(),
                    end=(end_date + timedelta(days=1)).isoformat(),
                    auto_adjust=False,
                    progress=False,
                    threads=False,
                )
                if isinstance(single.columns, pd.MultiIndex):
                    if ("Close", ticker) in single.columns:
                        series = single[("Close", ticker)]
                    elif (ticker, "Close") in single.columns:
                        series = single[(ticker, "Close")]
                elif "Close" in single.columns:
                    series = single["Close"]
            except Exception:
                series = None
        if series is None:
            missing.append(ticker)
            continue
        rows: dict[Date, Decimal] = {}
        for idx, value in series.dropna().items():
            try:
                day = idx.date()
                close = Decimal(str(float(value)))
            except Exception:
                continue
            if close > 0:
                rows[day] = close
        if rows:
            closes[ticker] = rows
        else:
            missing.append(ticker)
    warnings = [f"missing Yahoo close: {', '.join(missing)}"] if missing else []
    _YFINANCE_CLOSE_CACHE[cache_key] = (closes, tuple(warnings))
    return closes, warnings


def _close_on_or_before(series: dict[Date, Decimal], day: Date) -> Decimal | None:
    """Return the latest close on or before a sample date."""
    for price_date in sorted(series.keys(), reverse=True):
        if price_date <= day:
            return series[price_date]
    return None


def _ibkr_exact_position_history_series(
    cur,
    *,
    dates: list[Date],
    base_currency: str,
    fx_timelines: dict[str, list[tuple[Date, Decimal]]],
) -> tuple[dict[Date, Decimal], dict[Date, Decimal], set[Date]]:
    """Return exact IBKR Flex position-history values for sampled dates."""
    values = {d: Decimal("0") for d in dates}
    costs = {d: Decimal("0") for d in dates}
    exact_dates: set[Date] = set()
    if not dates:
        return values, costs, exact_dates

    cur.execute("SELECT to_regclass('public.pams_ibkr_position_history') IS NOT NULL AS exists")
    row = cur.fetchone()
    if not row or not bool(row.get("exists")):
        return values, costs, exact_dates

    cur.execute(
        """
        WITH series_dates AS (
            SELECT unnest(%s::date[]) AS sample_date
        ), currencies AS (
            SELECT DISTINCT UPPER(currency) AS currency
            FROM pams_ibkr_position_history
            WHERE account_code = 'ibkr'
              AND asset_liability = %s
              AND major_category = %s
        ), latest_dates AS (
            SELECT
                d.sample_date,
                c.currency,
                MAX(ph.as_of) AS as_of
            FROM series_dates d
            CROSS JOIN currencies c
            JOIN pams_ibkr_position_history ph
              ON ph.account_code = 'ibkr'
             AND ph.asset_liability = %s
             AND ph.major_category = %s
             AND UPPER(ph.currency) = c.currency
             AND ph.as_of <= (
                 d.sample_date
                 - CASE WHEN c.currency = 'USD' THEN INTERVAL '1 day' ELSE INTERVAL '0 day' END
             )::date
            GROUP BY d.sample_date, c.currency
        ), holdings AS (
            SELECT ld.sample_date, ph.*
            FROM latest_dates ld
            JOIN pams_ibkr_position_history ph
              ON ph.account_code = 'ibkr'
             AND ph.asset_liability = %s
             AND ph.major_category = %s
             AND ph.as_of = ld.as_of
             AND UPPER(ph.currency) = ld.currency
        )
        SELECT
            h.sample_date,
            h.as_of,
            h.symbol,
            h.currency,
            h.quantity,
            h.market_value,
            h.cost,
            visible_px.close AS visible_close
        FROM holdings h
        LEFT JOIN LATERAL (
            SELECT o.close
            FROM pams_ohlcv_daily o
            WHERE o.market = 'YF'
              AND o.symbol = h.symbol
              AND o.trade_date <= h.as_of
            ORDER BY o.trade_date DESC
            LIMIT 1
        ) visible_px ON TRUE
        """,
        (
            [d.isoformat() for d in dates],
            _ASSET_CATEGORY,
            _US_STOCK_CATEGORY,
            _ASSET_CATEGORY,
            _US_STOCK_CATEGORY,
            _ASSET_CATEGORY,
            _US_STOCK_CATEGORY,
        ),
    )
    for row in cur.fetchall():
        sample_date = row["sample_date"]
        if sample_date not in values:
            continue
        currency = str(row.get("currency") or base_currency).upper()
        fx = fx_rate_at(fx_timelines, currency, sample_date, base_currency)
        visible_close = dec(row.get("visible_close"))
        quantity = dec(row.get("quantity"))
        native_value = (
            visible_close * quantity
            if visible_close > 0 and quantity != 0
            else dec(row.get("market_value"))
        )
        values[sample_date] += native_value * fx
        costs[sample_date] += dec(row.get("cost")) * fx
        exact_dates.add(sample_date)
    return values, costs, exact_dates


def _ibkr_full_trade_return_series(
    cur,
    *,
    dates: list[Date],
    base_currency: str,
    fx_timelines: dict[str, list[tuple[Date, Decimal]]],
) -> tuple[dict[Date, Decimal], dict[Date, Decimal], list[str]]:
    """Reconstruct IBKR value and cost from all stock and ETF trades."""
    values = {d: Decimal("0") for d in dates}
    costs = {d: Decimal("0") for d in dates}
    if not dates:
        return values, costs, []

    exact_values, exact_costs, exact_dates = _ibkr_exact_position_history_series(
        cur,
        dates=dates,
        base_currency=base_currency,
        fx_timelines=fx_timelines,
    )
    if exact_dates == set(dates):
        return exact_values, exact_costs, []

    cur.execute(
        """
        SELECT
          symbol,
          COALESCE(MAX(NULLIF(currency, '')), 'USD') AS currency
        FROM pams_investment_trade_fact
        WHERE account_code = 'ibkr'
          AND COALESCE(NULLIF(market, ''), NULLIF(asset_minor_category, ''), NULLIF(asset_major_category, '')) = ANY(%s::text[])
          AND symbol IS NOT NULL
          AND trade_date <= %s
          AND quantity IS NOT NULL
          AND COALESCE(LOWER(review_status), 'confirmed') NOT IN ('cancelled', 'canceled', 'void')
        GROUP BY symbol
        ORDER BY symbol
        """,
        (list(IBKR_EQUITY_MARKETS), dates[-1]),
    )
    traded = [dict(r) for r in cur.fetchall()]
    if not traded:
        return exact_values, exact_costs, []

    symbol_map = {
        str(item["symbol"]): _ibkr_yfinance_symbol(str(item["symbol"]), str(item["currency"]))
        for item in traded
    }
    yahoo_symbols = [s for s in symbol_map.values() if s]
    closes, warnings = _fetch_yfinance_closes(
        yahoo_symbols,
        start_date=dates[0],
        end_date=dates[-1],
    )

    active_symbols = sorted(symbol_map.keys())
    cur.execute(
        """
        SELECT
            trade_date,
            symbol,
            currency,
            quantity,
            gross_amount,
            COALESCE(fee_amount, 0) + COALESCE(tax_amount, 0) AS commission,
            net_cash
        FROM pams_investment_trade_fact
        WHERE account_code = 'ibkr'
          AND symbol = ANY(%s::text[])
          AND trade_date <= %s
          AND quantity IS NOT NULL
          AND COALESCE(NULLIF(market, ''), NULLIF(asset_minor_category, ''), NULLIF(asset_major_category, '')) = ANY(%s::text[])
          AND COALESCE(LOWER(review_status), 'confirmed') NOT IN ('cancelled', 'canceled', 'void')
        ORDER BY trade_date, trade_id
        """,
        (active_symbols, dates[-1], list(IBKR_EQUITY_MARKETS)),
    )
    trades_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for row in cur.fetchall():
        item = dict(row)
        item["return_effective_date"] = ibkr_return_effective_date(item["trade_date"], item.get("currency"))
        trades_by_symbol.setdefault(str(item["symbol"]), []).append(item)
    for symbol_trades in trades_by_symbol.values():
        symbol_trades.sort(key=lambda item: (item["return_effective_date"], item["trade_date"]))

    symbol_currency = {str(item["symbol"]): str(item["currency"] or "USD").upper() for item in traded}
    quantity_eps = Decimal("0.0000001")
    amount_eps = Decimal("0.0001")
    for sample_date in dates:
        for symbol in active_symbols:
            ticker = symbol_map.get(symbol)
            close = _close_on_or_before(closes.get(ticker or "", {}), sample_date) if ticker else None

            quantity = Decimal("0")
            native_cost = Decimal("0")
            for trade in trades_by_symbol.get(symbol, []):
                if trade["return_effective_date"] > sample_date:
                    break
                qty = dec(trade.get("quantity"))
                gross = abs(dec(trade.get("gross_amount")))
                commission = abs(dec(trade.get("commission")))
                net_cash = dec(trade.get("net_cash"))
                quantity += qty
                if qty > 0:
                    native_cost += abs(net_cash) if net_cash != 0 else gross + commission
                elif qty < 0:
                    native_cost -= net_cash if net_cash > 0 else max(gross - commission, Decimal("0"))
            if abs(quantity) <= quantity_eps and abs(native_cost) <= amount_eps:
                continue

            if abs(quantity) > quantity_eps:
                if close is None:
                    continue
                native_value = quantity * close
            else:
                native_value = Decimal("0")

            currency = symbol_currency.get(symbol, "USD")
            fx = fx_rate_at(fx_timelines, currency, sample_date, base_currency)
            values[sample_date] += native_value * fx
            costs[sample_date] += native_cost * fx

    for sample_date in exact_dates:
        values[sample_date] = exact_values[sample_date]
        costs[sample_date] = exact_costs[sample_date]

    return values, costs, warnings
