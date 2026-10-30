"""Price-history storage and read helpers for analysis and trade review.

The canonical storage stays in ``pams_ohlcv_daily``. This service gives
analysis pages and future trade-review views one stable API shape for both
portfolio instruments and benchmark curves.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as Date, datetime, timedelta
from decimal import Decimal
from typing import Any


CN_INDEX_SYMBOLS = {
    "000001.SH",
    "000300.SH",
    "000905.SH",
    "000852.SH",
    "399001.SZ",
    "399006.SZ",
}

BENCHMARK_LABELS = {
    "000001.SH": "SSE Composite",
    "000300.SH": "CSI 300",
    "000905.SH": "CSI 500",
    "000852.SH": "CSI 1000",
    "399001.SZ": "SZSE Component",
    "399006.SZ": "ChiNext",
}

MARKET_ALIASES = {
    "A": "CN_A",
    "A_SHARES": "CN_A",
    "A_SHARE": "CN_A",
    "A_STOCK": "CN_A",
    "ASHARE": "CN_A",
    "CN": "CN_A",
    "CN_A": "CN_A",
    "CN_ETF": "CN_A",
    "CN_INDEX": "CN_INDEX",
    "INDEX": "CN_INDEX",
    "BENCHMARK": "CN_INDEX",
    "YF": "YF",
    "YAHOO": "YF",
    "US": "YF",
    "US_STOCK": "YF",
    "HK": "YF",
    "HK_STOCK": "YF",
    "KR": "YF",
    "KR_STOCK": "YF",
    "EU": "YF",
    "EU_STOCK": "YF",
    "EUROPE": "YF",
    "SE": "YF",
    "SEK": "YF",
    "SE_STOCK": "YF",
    "SWEDEN": "YF",
    "SVERIGE": "YF",
    "STOCKHOLM": "YF",
    "欧股": "YF",
    "瑞典": "YF",
}


@dataclass(frozen=True)
class PriceInstrument:
    """Normalized instrument key used by ``pams_ohlcv_daily`` readers."""

    symbol: str
    market: str
    query_symbol: str
    label: str
    requested_symbol: str
    source: str
    role: str = "instrument"


def dec(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    return Decimal(str(value))


def norm(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _text(value: Any) -> str:
    return str(value or "").strip()


def _compact_symbol(symbol: str) -> str:
    return _text(symbol).replace(" ", "")


def _ts_to_internal(ts_code: str) -> str:
    raw = _compact_symbol(ts_code).upper()
    if raw.endswith(".SH"):
        return f"sh{raw[:6]}"
    if raw.endswith(".SZ"):
        return f"sz{raw[:6]}"
    return raw


def _internal_to_ts(symbol: str) -> str:
    raw = _compact_symbol(symbol)
    lower = raw.lower()
    if lower.startswith("sh") and len(lower) == 8:
        return f"{lower[2:]}.SH"
    if lower.startswith("sz") and len(lower) == 8:
        return f"{lower[2:]}.SZ"
    if "." in raw:
        return raw.upper()
    return raw.upper()


def _index_ts_code(symbol: str) -> str:
    raw = _compact_symbol(symbol).upper()
    if raw.endswith((".SH", ".SZ")):
        return raw
    if raw.lower().startswith(("sh", "sz")) and len(raw) == 8:
        return _internal_to_ts(raw)
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 6:
        suffix = "SZ" if digits.startswith("399") else "SH"
        return f"{digits}.{suffix}"
    return raw


def _a_stock_symbol(symbol: str) -> str:
    raw = _compact_symbol(symbol)
    lower = raw.lower()
    if lower.startswith(("sh", "sz", "bj")):
        return lower
    if raw.upper().endswith((".SH", ".SZ")):
        return _ts_to_internal(raw)
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 6:
        if digits.startswith(("5", "6", "9")):
            return f"sh{digits}"
        return f"sz{digits}"
    return raw


def _yf_symbol(symbol: str, market_hint: str | None = None) -> str:
    raw = _compact_symbol(symbol)
    hint = _text(market_hint).upper()
    if hint in {"HK", "HK_STOCK"} and raw.isdigit():
        return f"{raw.zfill(4)}.HK"
    if hint in {"KR", "KR_STOCK"} and raw.isdigit():
        return f"{raw.zfill(6)}.KS"
    if hint in {"SE", "SEK", "SE_STOCK", "SWEDEN", "SVERIGE", "STOCKHOLM", "瑞典"} and "." not in raw:
        return f"{raw.upper()}.ST"
    if raw.isdigit() and 1 <= len(raw) <= 5:
        return f"{raw.zfill(4)}.HK"
    return raw.upper() if "." not in raw else raw.upper()


def normalize_price_instrument(
    symbol: str,
    *,
    market: str | None = None,
    label: str | None = None,
    benchmark: bool = False,
) -> PriceInstrument:
    """Normalize user/API symbols to the storage key used by price tables."""
    requested = _compact_symbol(symbol)
    if not requested:
        raise ValueError("symbol is required")

    raw_market = _text(market)
    normalized_market = MARKET_ALIASES.get(raw_market.upper(), raw_market.upper() if raw_market else "")

    ts_candidate = requested.upper()
    if benchmark and not normalized_market:
        normalized_market = (
            "CN_INDEX"
            if ts_candidate in CN_INDEX_SYMBOLS or ts_candidate.startswith(("000", "399"))
            else "CN_A"
            if len(ts_candidate) == 9 and ts_candidate[:6].isdigit() and ts_candidate[6:] in {".SH", ".SZ"}
            else "YF"
        )
    if not normalized_market:
        if ts_candidate in CN_INDEX_SYMBOLS:
            normalized_market = "CN_INDEX"
        elif requested.isdigit() and len(requested) == 6:
            normalized_market = "CN_A"
        elif len(ts_candidate) == 9 and ts_candidate[:6].isdigit() and ts_candidate[6:] in {".SH", ".SZ"}:
            normalized_market = "CN_A"
        else:
            normalized_market = "YF"

    if normalized_market == "CN_INDEX":
        query_symbol = _index_ts_code(requested)
        storage_symbol = _ts_to_internal(query_symbol)
        source = "pit_index_daily/pams_ohlcv_daily"
        display_label = label or BENCHMARK_LABELS.get(query_symbol, requested)
    elif normalized_market == "CN_A":
        storage_symbol = _a_stock_symbol(requested)
        query_symbol = storage_symbol
        source = "pams_ohlcv_daily"
        display_label = label or requested
    elif normalized_market == "YF":
        storage_symbol = _yf_symbol(requested, raw_market)
        query_symbol = storage_symbol
        source = "pams_ohlcv_daily:YF"
        display_label = label or requested
    else:
        storage_symbol = requested
        query_symbol = requested
        source = "pams_ohlcv_daily"
        display_label = label or requested

    return PriceInstrument(
        symbol=storage_symbol,
        market=normalized_market,
        query_symbol=query_symbol,
        label=display_label,
        requested_symbol=requested,
        source=source,
        role="benchmark" if benchmark else "instrument",
    )


def _row_date(row: dict[str, Any]) -> Date:
    value = row.get("trade_date") or row.get("date")
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, Date):
        return value
    return Date.fromisoformat(str(value)[:10])


def rows_to_price_curve(
    rows: list[dict[str, Any]],
    *,
    normalize: bool = True,
) -> list[dict[str, Any]]:
    """Convert DB OHLC rows to chart-ready close and return series."""
    clean = [
        {
            "date": _row_date(row),
            "open": dec(row.get("open")),
            "high": dec(row.get("high")),
            "low": dec(row.get("low")),
            "close": dec(row.get("close")),
            "volume": dec(row.get("volume")),
            "turnover": dec(row.get("turnover")),
        }
        for row in rows
        if dec(row.get("close")) > 0
    ]
    clean.sort(key=lambda item: item["date"])
    if not clean:
        return []

    base_close = clean[0]["close"]
    out: list[dict[str, Any]] = []
    for item in clean:
        close = item["close"]
        return_pct = (close / base_close - Decimal("1")) * Decimal("100") if base_close > 0 else Decimal("0")
        normalized = close / base_close * Decimal("100") if normalize and base_close > 0 else close
        out.append(
            {
                "date": item["date"].isoformat(),
                "open": norm(item["open"]),
                "high": norm(item["high"]),
                "low": norm(item["low"]),
                "close": norm(close),
                "volume": norm(item["volume"]),
                "turnover": norm(item["turnover"]),
                "normalized": norm(normalized),
                "return_pct": norm(return_pct),
            }
        )
    return out


def _fetch_dict_rows(cur) -> list[dict[str, Any]]:
    return [dict(row) for row in cur.fetchall()]


def _query_ohlcv_rows(cur, instrument: PriceInstrument, start: Date, end: Date) -> list[dict[str, Any]]:
    if instrument.market == "CN_INDEX":
        cur.execute(
            """
            SELECT trade_date, open, high, low, close, volume, turnover
            FROM pit_index_daily
            WHERE index_code = %s
              AND trade_date >= %s
              AND trade_date <= %s
            ORDER BY trade_date
            """,
            (instrument.query_symbol, start, end),
        )
        pit_rows = _fetch_dict_rows(cur)

    if instrument.market == "CN_INDEX":
        cur.execute(
            """
            SELECT trade_date, open, high, low, close, volume, turnover
            FROM pams_ohlcv_daily
            WHERE symbol = %s
              AND market = %s
              AND trade_date >= %s
              AND trade_date <= %s
            ORDER BY trade_date
            """,
            (instrument.symbol, "CN_INDEX", start, end),
        )
    else:
        cur.execute(
            """
            SELECT trade_date, open, high, low, close, volume, turnover
            FROM pams_ohlcv_daily
            WHERE symbol = %s
              AND market = %s
              AND trade_date >= %s
              AND trade_date <= %s
            ORDER BY trade_date
            """,
            (instrument.symbol, instrument.market, start, end),
        )
    rows = _fetch_dict_rows(cur)
    if instrument.market == "CN_INDEX":
        # Preserve the index source for duplicate dates; fill only its gaps.
        by_day = {row["trade_date"]: row for row in rows}
        by_day.update({row["trade_date"]: row for row in pit_rows})
        return [by_day[day] for day in sorted(by_day)]
    return rows


def build_price_series_payload(
    cur,
    *,
    instruments: list[PriceInstrument],
    start: Date,
    end: Date,
    normalize: bool = True,
) -> dict[str, Any]:
    """Build chart-ready price/benchmark series for analysis and reviews."""
    series = []
    for instrument in instruments:
        rows = _query_ohlcv_rows(cur, instrument, start, end)
        curve = rows_to_price_curve(rows, normalize=normalize)
        series.append(
            {
                "requested_symbol": instrument.requested_symbol,
                "symbol": instrument.symbol,
                "query_symbol": instrument.query_symbol,
                "market": instrument.market,
                "label": instrument.label,
                "source": instrument.source,
                "role": instrument.role,
                "requested_start": start.isoformat(),
                "requested_end": end.isoformat(),
                "start": curve[0]["date"] if curve else None,
                "end": curve[-1]["date"] if curve else None,
                "points": len(curve),
                "coverage": "ok" if curve else "missing",
                "rows": curve,
            }
        )
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "normalize": normalize,
        "series": series,
    }


def fetch_yfinance_history(
    symbols: list[str],
    *,
    start: Date,
    end: Date,
) -> tuple[dict[str, dict[Date, Decimal]], list[str]]:
    """Fetch Yahoo close history for symbols that are not local A-share data."""
    tickers = sorted({symbol for symbol in symbols if symbol})
    if not tickers:
        return {}, []
    try:
        import pandas as pd
        import yfinance as yf
    except Exception as exc:  # pragma: no cover - environment dependent
        return {}, [f"yfinance unavailable: {exc}"]

    try:
        frame = yf.download(
            tickers,
            start=(start - timedelta(days=7)).isoformat(),
            end=(end + timedelta(days=1)).isoformat(),
            auto_adjust=False,
            progress=False,
            group_by="ticker",
            threads=True,
        )
    except Exception as exc:  # pragma: no cover - network dependent
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
            missing.append(ticker)
            continue
        rows: dict[Date, Decimal] = {}
        for idx, value in series.dropna().items():
            try:
                day = idx.date()
                close = Decimal(str(float(value)))
            except Exception:
                continue
            if start <= day <= end and close > 0:
                rows[day] = close
        if rows:
            closes[ticker] = rows
        else:
            missing.append(ticker)
    return closes, [f"missing Yahoo close: {', '.join(missing)}"] if missing else []


def upsert_yfinance_history(
    cur,
    *,
    symbols: list[str],
    start: Date,
    end: Date,
) -> dict[str, Any]:
    """Fetch and persist Yahoo closes into ``pams_ohlcv_daily`` market ``YF``."""
    closes, warnings = fetch_yfinance_history(symbols, start=start, end=end)
    payload = []
    for symbol, rows in closes.items():
        for trade_date, close in rows.items():
            payload.append((symbol, "YF", trade_date, close))
    if payload:
        cur.executemany(
            """
            INSERT INTO pams_ohlcv_daily (symbol, market, trade_date, close)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (symbol, market, trade_date)
            DO UPDATE SET close = EXCLUDED.close
            """,
            payload,
        )
    return {
        "source": "yfinance",
        "symbols": symbols,
        "rows": len(payload),
        "warnings": warnings,
    }


def ensure_price_history(
    cur,
    *,
    instruments: list[PriceInstrument],
    start: Date,
    end: Date,
) -> dict[str, Any]:
    """Persist missing external price history for supported instruments."""
    yf_symbols = sorted({item.symbol for item in instruments if item.market == "YF"})
    result: dict[str, Any] = {"requested": len(instruments), "stored": []}
    if yf_symbols:
        result["stored"].append(upsert_yfinance_history(cur, symbols=yf_symbols, start=start, end=end))
    local_symbols = [
        item.requested_symbol
        for item in instruments
        if item.market in {"CN_A", "CN_INDEX"}
    ]
    if local_symbols:
        result["local_sources"] = {
            "symbols": local_symbols,
            "note": "CN_A/CN_INDEX history is read from local PIT tables; run datahub ingest if coverage is missing.",
        }
    return result


def parse_symbol_list(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        raw_items = value
    else:
        raw_items = str(value).split(",")
    return [item.strip() for item in raw_items if item and item.strip()]
