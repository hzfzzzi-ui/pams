"""IBKR stock-return reconstruction helpers.

This module owns Yahoo Finance symbol mapping, close-price fetching, and the
full-trade IBKR stock/ETF value reconstruction used by
``domain.investments.investment_returns``.  It does not assemble endpoint payloads.
"""

from __future__ import annotations

from datetime import date as Date, timedelta
from decimal import Decimal
from typing import Any

from domain.investments.investment_return_data import IBKR_EQUITY_MARKETS, exact_fx_rate_at
from domain.investments.investment_return_math import dec, ibkr_return_effective_date


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
    from domain.imports.isolation import source_mode
    if source_mode() == "development":
        return {}, ["External market data is unavailable in development mode"]
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


def _local_exact_closes(cur, symbols: list[str], dates: list[Date]) -> dict[str, dict[Date, Decimal]]:
    """Load recorded Yahoo-market closes only for the requested business dates."""
    if not symbols or not dates:
        return {}
    cur.execute(
        """SELECT symbol, trade_date, close FROM pams_ohlcv_daily
           WHERE market = 'YF' AND symbol = ANY(%s::text[])
             AND trade_date = ANY(%s::date[]) AND close > 0""",
        (symbols, dates),
    )
    closes: dict[str, dict[Date, Decimal]] = {}
    for row in cur.fetchall():
        closes.setdefault(row['symbol'], {})[row['trade_date']] = row['close']
    return closes


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

    cur.execute(
        """
        SELECT
          symbol,
          MAX(NULLIF(currency, '')) AS currency,
          COUNT(DISTINCT NULLIF(currency, '')) AS currency_count,
          COUNT(*) FILTER (WHERE NULLIF(currency, '') IS NULL) AS missing_currency_count
        FROM pams_investment_trade_fact
        WHERE account_code = 'ibkr'
          AND COALESCE(NULLIF(market, ''), NULLIF(asset_minor_category, ''), NULLIF(asset_major_category, '')) = ANY(%s::text[])
          AND symbol IS NOT NULL
          AND trade_date <= %s
          AND quantity IS NOT NULL
          AND COALESCE(LOWER(review_status), 'confirmed') IN ('auto', 'reviewed', 'confirmed')
        GROUP BY symbol
        ORDER BY symbol
        """,
        (list(IBKR_EQUITY_MARKETS), dates[-1]),
    )
    traded = [dict(r) for r in cur.fetchall()]
    if not traded:
        return {}, {}, ["ibkr_position_history_unavailable"]

    symbol_map = {
        str(item["symbol"]): (_ibkr_yfinance_symbol(str(item["symbol"]), str(item["currency"]))
            if item.get("currency") and item.get("currency_count",1)==1 and not item.get("missing_currency_count",0) else None)
        for item in traded
    }
    yahoo_symbols = [s for s in symbol_map.values() if s]
    from domain.imports.isolation import source_mode
    local_only = source_mode() == 'development'
    if local_only:
        closes, warnings = _local_exact_closes(cur, yahoo_symbols, dates), []
    else:
        closes, warnings = _fetch_yfinance_closes(
            yahoo_symbols,
            start_date=dates[0],
            end_date=dates[-1],
        )
    warnings.append("ibkr_history_coverage_unverified")

    active_symbols = sorted(symbol_map.keys())
    cur.execute(
        """
        SELECT
            trade_id,
            source_table,
            trade_date,
            symbol,
            currency,
            quantity,
            gross_amount,
            COALESCE(fee_amount, 0) + COALESCE(tax_amount, 0) AS commission,
            COALESCE((fee_amount=0 OR UPPER(fee_currency)=UPPER(currency)) AND
                (net_cash IS NOT NULL OR tax_amount IS NOT NULL), FALSE) AS tax_known,
            net_cash
        FROM pams_investment_trade_fact
        WHERE account_code = 'ibkr'
          AND symbol = ANY(%s::text[])
          AND trade_date <= %s
          AND quantity IS NOT NULL
          AND COALESCE(NULLIF(market, ''), NULLIF(asset_minor_category, ''), NULLIF(asset_major_category, '')) = ANY(%s::text[])
          AND COALESCE(LOWER(review_status), 'confirmed') IN ('auto', 'reviewed', 'confirmed')
        ORDER BY trade_date, trade_id
        """,
        (active_symbols, dates[-1], list(IBKR_EQUITY_MARKETS)),
    )
    trades_by_symbol: dict[str, list[dict[str, Any]]] = {}
    candidates = [dict(row) for row in cur.fetchall()]
    from domain.brokers.ibkr.ibkr_fact_source_verification import EvidenceCache, verify_ibkr_fact
    from domain.imports.source_fact_evidence import NativeFactVerifier
    source_cache, native_verifier = EvidenceCache(), NativeFactVerifier(cur)
    unverified_from: list[Date] = []
    for item in candidates:
        item["return_effective_date"] = ibkr_return_effective_date(item["trade_date"], item.get("currency"))
        try:
            if item.get('source_table') == 'ibkr_flex_trade':
                evidence = verify_ibkr_fact(cur, trade_id=item['trade_id'], cache=source_cache)
                required = {'trade_date','symbol','currency','side','quantity','gross_amount','net_cash'}
                proven = (evidence.get('status') in {'consistent','partial'} and
                          not required.intersection(evidence.get('unknown_fields', [])))
            else:
                evidence = native_verifier.verify(table='pams_investment_trade_fact', fact_id=item['trade_id'])
                proven = evidence.get('status') == 'verified'
        except (KeyError, TypeError, ValueError):
            proven = False
        if not proven:
            unverified_from.append(item['return_effective_date'])
            continue
        trades_by_symbol.setdefault(str(item["symbol"]), []).append(item)
    for symbol_trades in trades_by_symbol.values():
        symbol_trades.sort(key=lambda item: (item["return_effective_date"], item["trade_date"]))

    symbol_currency = {str(item["symbol"]): (str(item["currency"]).upper()
                       if item.get("currency") and item.get("currency_count",1)==1 and not item.get("missing_currency_count",0) else None)
                       for item in traded}
    quantity_eps = Decimal("0.0000001")
    amount_eps = Decimal("0.0001")
    missing_prices: set[Date] = {day for day in dates if any(day >= start for start in unverified_from)}
    missing_costs: set[Date] = set(missing_prices)
    missing_fx: set[Date] = set()
    for sample_date in dates:
        for symbol in active_symbols:
            ticker = symbol_map.get(symbol)
            series = closes.get(ticker or "", {})
            close = (series.get(sample_date) if local_only else _close_on_or_before(series, sample_date)) if ticker else None

            quantity = Decimal("0")
            native_cost = Decimal("0")
            for trade in trades_by_symbol.get(symbol, []):
                if trade["return_effective_date"] > sample_date:
                    break
                if trade.get("tax_known") is False:
                    missing_prices.add(sample_date)
                    missing_costs.add(sample_date)
                qty = dec(trade.get("quantity"))
                gross = abs(dec(trade.get("gross_amount")))
                commission = abs(dec(trade.get("commission")))
                net_cash = dec(trade.get("net_cash"))
                quantity += qty
                if qty > 0:
                    native_cost += -net_cash if trade.get('net_cash') is not None else gross + commission
                elif qty < 0:
                    native_cost -= net_cash if trade.get('net_cash') is not None else max(gross - commission, Decimal("0"))
            if abs(quantity) <= quantity_eps and abs(native_cost) <= amount_eps:
                continue

            if not symbol_currency.get(symbol):
                missing_fx.add(sample_date)
                continue
            if abs(quantity) > quantity_eps:
                if close is None:
                    missing_prices.add(sample_date)
                    if local_only:
                        fx = exact_fx_rate_at(fx_timelines, symbol_currency.get(symbol), sample_date, base_currency)
                        if fx is None:
                            missing_fx.add(sample_date)
                        else:
                            costs[sample_date] += native_cost * fx
                    continue
                native_value = quantity * close
            else:
                native_value = Decimal("0")

            currency = symbol_currency.get(symbol)
            fx = exact_fx_rate_at(fx_timelines, currency, sample_date, base_currency)
            if fx is None:
                missing_fx.add(sample_date)
                continue
            values[sample_date] += native_value * fx
            costs[sample_date] += native_cost * fx

    for sample_date in missing_fx:
        values[sample_date] = None
        costs[sample_date] = None
    if local_only:
        for sample_date in missing_prices:
            values[sample_date] = None
        for sample_date in missing_costs:
            costs[sample_date] = None
    missing_fx_days = [day.isoformat() for day in dates if day in missing_fx] if local_only else [day.isoformat() for day in dates if values.get(day) is None]
    if missing_fx_days:
        warnings.append("ibkr_fx_missing:" + ",".join(missing_fx_days))
    missing = [day.isoformat() for day in dates if day in missing_prices or values[day] == 0]
    if missing:
        warnings.append("ibkr_history_missing:" + ",".join(missing))

    return values, costs, warnings
