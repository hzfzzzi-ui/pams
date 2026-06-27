"""A-share fund NAV estimation and correction helpers.

The job has two responsibilities:
- store confirmed unit NAV from Tushare fund_nav when available, with
  Eastmoney F10 as a fallback;
- optionally store intraday estimated NAV from Tiantian Fund after A-share close.

By default, holding snapshots use confirmed fund NAVs only. This keeps the
Tencent Licaitong view aligned with platform balances, which usually remain on
the latest confirmed NAV until fund companies publish the next NAV.
"""

from __future__ import annotations

import html
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

import psycopg2.extras
import requests
from services.daily_return_state import quote_snapshot_from_market_daily, upsert_quote_snapshot


MONEY = Decimal("0.01")
QUANTITY = Decimal("0.000001")
PRICE = Decimal("0.000001")
RATIO = Decimal("0.000001")

DEFAULT_REVIEW_PCT = Decimal("0.01")
DEFAULT_REVIEW_AMOUNT = Decimal("200")
TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}
CASH_LIKE_FUND_SYMBOLS = {"020509"}
CASH_LIKE_FUND_KEYWORDS = ("\u8d27\u5e01", "\u73b0\u91d1")


@dataclass(frozen=True)
class ConfirmedNav:
    """Confirmed fund unit NAV from Eastmoney F10."""

    nav_date: date
    unit_nav: Decimal
    accumulated_nav: Decimal | None
    growth_pct: Decimal | None
    source: str = "eastmoney_f10"
    ts_code: str | None = None


@dataclass(frozen=True)
class IntradayEstimate:
    """Intraday estimated unit NAV from Tiantian Fund."""

    estimate_time: datetime
    previous_nav_date: date | None
    previous_unit_nav: Decimal | None
    estimated_unit_nav: Decimal
    estimated_growth_pct: Decimal | None


def dec(value: Any, quant: Decimal = MONEY) -> Decimal:
    """Convert a scalar to Decimal and round with a stable quantizer."""
    if value in (None, ""):
        return Decimal("0").quantize(quant)
    try:
        return Decimal(str(value).replace(",", "").replace("%", "")).quantize(
            quant, rounding=ROUND_HALF_UP
        )
    except (InvalidOperation, ValueError):
        return Decimal("0").quantize(quant)


def optional_dec(value: Any, quant: Decimal = PRICE) -> Decimal | None:
    """Return None for missing/non-numeric fields, otherwise a rounded Decimal."""
    if value in (None, ""):
        return None
    parsed = dec(value, quant)
    return parsed if parsed != 0 else None


def parse_confirmed_nav_rows(script_text: str) -> list[ConfirmedNav]:
    """Parse Eastmoney F10DataApi HTML rows into confirmed unit NAV records."""
    rows: list[ConfirmedNav] = []
    trs = re.findall(r"<tr[^>]*>(.*?)</tr>", script_text, flags=re.S | re.I)
    for tr in trs:
        cells = [
            html.unescape(re.sub(r"<[^>]+>", "", cell)).strip()
            for cell in re.findall(r"<td[^>]*>(.*?)</td>", tr, flags=re.S | re.I)
        ]
        if len(cells) < 3 or not re.match(r"\d{4}-\d{2}-\d{2}", cells[0]):
            continue
        try:
            nav_date = datetime.strptime(cells[0], "%Y-%m-%d").date()
        except ValueError:
            continue
        unit_nav = optional_dec(cells[1], PRICE)
        if unit_nav is None:
            continue
        rows.append(
            ConfirmedNav(
                nav_date=nav_date,
                unit_nav=unit_nav,
                accumulated_nav=optional_dec(cells[2], PRICE),
                growth_pct=optional_dec(cells[3], RATIO) if len(cells) > 3 else None,
            )
        )
    return rows


def parse_intraday_estimate(script_text: str) -> IntradayEstimate | None:
    """Parse Tiantian fundgz jsonp into an intraday estimate."""
    match = re.search(r"jsonpgz\((.*)\);?", script_text)
    if not match:
        return None
    payload = json.loads(match.group(1))
    estimated_unit_nav = optional_dec(payload.get("gsz"), PRICE)
    if estimated_unit_nav is None:
        return None
    estimate_time_raw = payload.get("gztime")
    if not estimate_time_raw:
        return None
    estimate_time = datetime.strptime(estimate_time_raw, "%Y-%m-%d %H:%M")
    previous_nav_date = None
    if payload.get("jzrq"):
        previous_nav_date = datetime.strptime(payload["jzrq"], "%Y-%m-%d").date()
    return IntradayEstimate(
        estimate_time=estimate_time,
        previous_nav_date=previous_nav_date,
        previous_unit_nav=optional_dec(payload.get("dwjz"), PRICE),
        estimated_unit_nav=estimated_unit_nav,
        estimated_growth_pct=optional_dec(payload.get("gszzl"), RATIO),
    )


def normalize_tushare_fund_ts_code(symbol: str) -> str:
    """Return the Tushare OTC fund ts_code for a six-digit fund code."""
    raw = str(symbol or "").strip()
    if raw.upper().endswith(".OF"):
        code = raw[:6]
    else:
        code = raw.zfill(6)
    return f"{code}.OF"


def tushare_fund_ts_code_candidates(symbol: str) -> list[str]:
    """Return candidate Tushare fund ts_codes, preferring OTC .OF."""
    raw = str(symbol or "").strip().upper()
    if re.match(r"^\d{6}\.(OF|SH|SZ)$", raw):
        return [raw]
    code = raw[:6] if re.match(r"^\d{6}", raw) else raw.zfill(6)
    candidates = [f"{code}.OF"]
    if code.startswith("5"):
        candidates.append(f"{code}.SH")
    elif code.startswith("1"):
        candidates.append(f"{code}.SZ")
    return candidates


def parse_tushare_fund_nav_frame(df: Any) -> ConfirmedNav | None:
    """Parse Tushare fund_nav rows into the latest confirmed NAV."""
    if df is None or getattr(df, "empty", True):
        return None
    rows = df.copy()
    if "unit_nav" not in rows:
        return None
    date_column = next((column for column in ("nav_date", "end_date", "ann_date") if column in rows), None)
    if not date_column:
        return None
    sort_columns = [date_column]
    if date_column != "ann_date" and "ann_date" in rows:
        sort_columns.append("ann_date")
    rows = rows.sort_values(sort_columns, ascending=False)
    for _, row in rows.iterrows():
        unit_nav = optional_dec(row.get("unit_nav"), PRICE)
        if unit_nav is None:
            continue
        raw_date = str(row.get(date_column) or "").strip()
        if not raw_date:
            continue
        try:
            nav_date = datetime.strptime(raw_date[:8], "%Y%m%d").date()
        except ValueError:
            try:
                nav_date = datetime.strptime(raw_date[:10], "%Y-%m-%d").date()
            except ValueError:
                continue
        return ConfirmedNav(
            nav_date=nav_date,
            unit_nav=unit_nav,
            accumulated_nav=optional_dec(row.get("accum_nav"), PRICE),
            growth_pct=None,
            source="tushare_fund_nav",
            ts_code=str(row.get("ts_code") or ""),
        )
    return None


def infer_units(market_value: Decimal, quantity: Decimal, price: Decimal, fallback_nav: Decimal) -> Decimal:
    """Infer fund units from stored quantity, current price, or fallback NAV."""
    if quantity > 0:
        return quantity.quantize(QUANTITY, rounding=ROUND_HALF_UP)
    if price > 0:
        return (market_value / price).quantize(QUANTITY, rounding=ROUND_HALF_UP)
    if fallback_nav > 0:
        return (market_value / fallback_nav).quantize(QUANTITY, rounding=ROUND_HALF_UP)
    return Decimal("0").quantize(QUANTITY)


def estimate_value(units: Decimal, unit_nav: Decimal) -> Decimal:
    """Estimate holding market value from units and unit NAV."""
    return (units * unit_nav).quantize(MONEY, rounding=ROUND_HALF_UP)


def deviation_status(delta: Decimal, base: Decimal, *, pct_threshold: Decimal, amount_threshold: Decimal) -> str:
    """Classify correction size for manual review."""
    if base <= 0:
        return "needs_review" if abs(delta) >= amount_threshold else "ok"
    pct = abs(delta) / base
    return "needs_review" if pct >= pct_threshold and abs(delta) >= amount_threshold else "ok"


def review_thresholds() -> tuple[Decimal, Decimal]:
    """Read correction thresholds from environment with conservative defaults."""
    pct = dec(os.environ.get("PAMS_FUND_NAV_REVIEW_THRESHOLD_PCT"), RATIO)
    amount = dec(os.environ.get("PAMS_FUND_NAV_REVIEW_THRESHOLD_CNY"), MONEY)
    return (pct or DEFAULT_REVIEW_PCT), (amount or DEFAULT_REVIEW_AMOUNT)


def intraday_estimates_enabled() -> bool:
    """Return whether fundgz intraday estimates should be written as snapshots."""
    raw = os.environ.get("PAMS_FUND_NAV_USE_INTRADAY_ESTIMATE", "")
    return raw.strip().lower() in TRUTHY_ENV_VALUES


def is_cash_like_fund_holding(holding: dict[str, Any]) -> bool:
    """Return true for money-market/cash-like funds that should keep platform balances."""
    symbol = str(holding.get("symbol") or "").zfill(6)
    name = str(holding.get("name") or "")
    return symbol in CASH_LIKE_FUND_SYMBOLS or any(keyword in name for keyword in CASH_LIKE_FUND_KEYWORDS)


def fetch_confirmed_nav(symbol: str, session: requests.Session | None = None) -> ConfirmedNav | None:
    """Fetch latest confirmed unit NAV for a fund code."""
    http = session or requests.Session()
    response = http.get(
        "https://fund.eastmoney.com/f10/F10DataApi.aspx",
        params={"type": "lsjz", "code": symbol, "page": 1, "per": 1},
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://fund.eastmoney.com/"},
        timeout=15,
    )
    response.raise_for_status()
    rows = parse_confirmed_nav_rows(response.text)
    return rows[0] if rows else None


def fetch_confirmed_nav_tushare(symbol: str, pro: Any | None = None) -> ConfirmedNav | None:
    """Fetch latest confirmed OTC fund unit NAV from Tushare fund_nav."""
    token = os.environ.get("TUSHARE_TOKEN", "")
    if pro is None and not token:
        return None
    try:
        if pro is None:
            import tushare as ts

            pro = ts.pro_api(token)
        for ts_code in tushare_fund_ts_code_candidates(symbol):
            df = pro.fund_nav(
                ts_code=ts_code,
                fields="ts_code,ann_date,nav_date,end_date,unit_nav,accum_nav,adj_nav,update_flag",
            )
            nav = parse_tushare_fund_nav_frame(df)
            if nav and not nav.ts_code:
                return ConfirmedNav(
                    nav_date=nav.nav_date,
                    unit_nav=nav.unit_nav,
                    accumulated_nav=nav.accumulated_nav,
                    growth_pct=nav.growth_pct,
                    source=nav.source,
                    ts_code=ts_code,
                )
            if nav:
                return nav
    except Exception:
        raise
    return None


def fetch_confirmed_nav_prefer_tushare(
    symbol: str,
    *,
    session: requests.Session | None = None,
    pro: Any | None = None,
) -> tuple[ConfirmedNav | None, str | None]:
    """Fetch confirmed NAV from Tushare first, falling back to Eastmoney."""
    try:
        nav = fetch_confirmed_nav_tushare(symbol, pro=pro)
        if nav:
            return nav, None
    except Exception as exc:
        tushare_error = f"tushare fund_nav failed: {exc}"
    else:
        tushare_error = None
    try:
        nav = fetch_confirmed_nav(symbol, session=session)
        return nav, tushare_error
    except Exception:
        raise


def fetch_intraday_estimate(symbol: str, session: requests.Session | None = None) -> IntradayEstimate | None:
    """Fetch current intraday estimated unit NAV for a fund code."""
    http = session or requests.Session()
    response = http.get(
        f"http://fundgz.1234567.com.cn/js/{symbol}.js",
        params={"rt": int(time.time() * 1000)},
        headers={"User-Agent": "Mozilla/5.0", "Referer": "http://fund.eastmoney.com/"},
        timeout=10,
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return parse_intraday_estimate(response.text)


def ensure_fund_nav_estimate_table(cur) -> None:
    """Create the local fund NAV estimate table if it is missing."""
    cur.execute("SELECT pg_advisory_xact_lock(hashtext('pams_fund_nav_estimates_schema'))")
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS pams_fund_nav_estimates (
            id BIGSERIAL PRIMARY KEY,
            as_of DATE NOT NULL,
            account_code VARCHAR(50) NOT NULL,
            symbol VARCHAR(100) NOT NULL,
            name VARCHAR(200) NOT NULL,
            confirmed_nav_date DATE,
            confirmed_unit_nav NUMERIC(20, 8),
            confirmed_growth_pct NUMERIC(12, 6),
            estimate_time TIMESTAMP,
            estimated_unit_nav NUMERIC(20, 8),
            estimated_growth_pct NUMERIC(12, 6),
            anchor_as_of DATE,
            anchor_market_value NUMERIC(20, 4),
            anchor_unit_nav NUMERIC(20, 8),
            estimated_market_value NUMERIC(20, 4),
            estimated_delta NUMERIC(20, 4),
            estimated_delta_pct NUMERIC(12, 6),
            status VARCHAR(40) NOT NULL,
            note TEXT,
            raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (as_of, account_code, symbol, name)
        )
        """
    )


def load_latest_active_fund_holdings(cur) -> list[dict[str, Any]]:
    """Load active A-share fund holdings from each account's latest snapshot date."""
    cur.execute(
        """
        WITH latest_account AS (
            SELECT account_code, MAX(as_of) AS as_of
            FROM pams_holdings_snapshot
            WHERE major_category = 'A股'
              AND minor_category = 'A股基金'
              AND asset_liability = '资产'
            GROUP BY account_code
        )
        SELECT h.id, h.account_code, h.symbol, h.name, h.asset_liability, h.currency,
               h.major_category, h.minor_category, h.market_value, h.cost, h.as_of,
               h.quantity, h.buy_price, h.current_price
        FROM pams_holdings_snapshot h
        JOIN latest_account la
          ON la.account_code = h.account_code
         AND la.as_of = h.as_of
        WHERE h.major_category = 'A股'
          AND h.minor_category = 'A股基金'
          AND h.asset_liability = '资产'
          AND COALESCE(h.market_value, 0) >= 0
          AND h.symbol IS NOT NULL
          AND h.symbol <> ''
        ORDER BY h.market_value DESC, h.account_code, h.name
        """
    )
    return [
        holding
        for row in cur.fetchall()
        if not is_cash_like_fund_holding(holding := dict(row))
    ]


def existing_estimate(cur, *, as_of: date, account_code: str, symbol: str, name: str) -> dict[str, Any] | None:
    """Load an existing generated estimate row for one fund/date."""
    cur.execute(
        """
        SELECT *
        FROM pams_fund_nav_estimates
        WHERE as_of = %s
          AND account_code = %s
          AND symbol = %s
          AND name = %s
        """,
        (as_of, account_code, symbol, name),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def upsert_estimate_row(cur, row: dict[str, Any]) -> None:
    """Insert or update one fund NAV estimate record."""
    cur.execute(
        """
        INSERT INTO pams_fund_nav_estimates (
            as_of, account_code, symbol, name,
            confirmed_nav_date, confirmed_unit_nav, confirmed_growth_pct,
            estimate_time, estimated_unit_nav, estimated_growth_pct,
            anchor_as_of, anchor_market_value, anchor_unit_nav,
            estimated_market_value, estimated_delta, estimated_delta_pct,
            status, note, raw_json
        ) VALUES (
            %(as_of)s, %(account_code)s, %(symbol)s, %(name)s,
            %(confirmed_nav_date)s, %(confirmed_unit_nav)s, %(confirmed_growth_pct)s,
            %(estimate_time)s, %(estimated_unit_nav)s, %(estimated_growth_pct)s,
            %(anchor_as_of)s, %(anchor_market_value)s, %(anchor_unit_nav)s,
            %(estimated_market_value)s, %(estimated_delta)s, %(estimated_delta_pct)s,
            %(status)s, %(note)s, %(raw_json)s
        )
        ON CONFLICT (as_of, account_code, symbol, name) DO UPDATE SET
            confirmed_nav_date = EXCLUDED.confirmed_nav_date,
            confirmed_unit_nav = EXCLUDED.confirmed_unit_nav,
            confirmed_growth_pct = EXCLUDED.confirmed_growth_pct,
            estimate_time = EXCLUDED.estimate_time,
            estimated_unit_nav = EXCLUDED.estimated_unit_nav,
            estimated_growth_pct = EXCLUDED.estimated_growth_pct,
            anchor_as_of = EXCLUDED.anchor_as_of,
            anchor_market_value = EXCLUDED.anchor_market_value,
            anchor_unit_nav = EXCLUDED.anchor_unit_nav,
            estimated_market_value = EXCLUDED.estimated_market_value,
            estimated_delta = EXCLUDED.estimated_delta,
            estimated_delta_pct = EXCLUDED.estimated_delta_pct,
            status = EXCLUDED.status,
            note = EXCLUDED.note,
            raw_json = EXCLUDED.raw_json,
            updated_at = now()
        """,
        row,
    )


def upsert_asset_market_daily_nav(
    cur,
    *,
    holding: dict[str, Any],
    as_of: date,
    unit_nav: Decimal | None = None,
    estimated_unit_nav: Decimal | None = None,
    source: str,
    raw: dict[str, Any],
) -> None:
    """Store fund NAV in the normalized asset market daily table."""
    symbol = str(holding["symbol"]).zfill(6)
    source_key = f"{source}:{symbol}:{as_of.isoformat()}"
    cur.execute(
        """
        INSERT INTO pams_asset_market_daily (
            as_of, source, source_key, asset_id, symbol, name, market, currency,
            unit_nav, estimated_unit_nav, confidence, raw_json
        ) VALUES (
            %(as_of)s, %(source)s, %(source_key)s, %(asset_id)s, %(symbol)s, %(name)s,
            '基金', 'CNY', %(unit_nav)s, %(estimated_unit_nav)s, %(confidence)s, %(raw_json)s
        )
        ON CONFLICT (as_of, source, source_key) DO UPDATE SET
            asset_id = EXCLUDED.asset_id,
            symbol = EXCLUDED.symbol,
            name = EXCLUDED.name,
            market = EXCLUDED.market,
            currency = EXCLUDED.currency,
            unit_nav = EXCLUDED.unit_nav,
            estimated_unit_nav = EXCLUDED.estimated_unit_nav,
            confidence = EXCLUDED.confidence,
            raw_json = EXCLUDED.raw_json,
            updated_at = now()
        """,
        {
            "as_of": as_of,
            "source": source,
            "source_key": source_key,
            "asset_id": holding.get("asset_id"),
            "symbol": symbol,
            "name": holding["name"],
            "unit_nav": unit_nav,
            "estimated_unit_nav": estimated_unit_nav,
            "confidence": "high" if source == "tushare_fund_nav" else "medium",
            "raw_json": psycopg2.extras.Json(raw),
        },
    )
    upsert_quote_snapshot(
        cur,
        quote_snapshot_from_market_daily(
            {
                "as_of": as_of,
                "asset_id": holding.get("asset_id"),
                "symbol": symbol,
                "name": holding["name"],
                "market": "\u57fa\u91d1",
                "currency": "CNY",
                "provider": source.split("_")[0] if source else "fund",
                "source": source,
                "source_key": source_key,
                "unit_nav": unit_nav,
                "estimated_unit_nav": estimated_unit_nav,
                "raw_json": raw,
            }
        ),
    )


def insert_or_update_generated_snapshot(
    cur,
    *,
    holding: dict[str, Any],
    as_of: date,
    market_value: Decimal,
    units: Decimal,
    unit_nav: Decimal,
    update_existing: bool,
) -> bool:
    """Write a generated holding snapshot for estimated/confirmed fund valuation."""
    cost = dec(holding.get("cost"), MONEY)
    buy_price = (cost / units).quantize(PRICE, rounding=ROUND_HALF_UP) if units > 0 and cost > 0 else None
    if update_existing:
        cur.execute(
            """
            INSERT INTO pams_holdings_snapshot (
                symbol, name, asset_liability, currency, account_code,
                major_category, minor_category, market_value, cost, as_of,
                quantity, buy_price, current_price
            ) VALUES (
                %(symbol)s, %(name)s, '资产', 'CNY', %(account_code)s,
                'A股', 'A股基金', %(market_value)s, %(cost)s, %(as_of)s,
                %(quantity)s, %(buy_price)s, %(current_price)s
            )
            ON CONFLICT (account_code, name, as_of) DO UPDATE SET
                symbol = EXCLUDED.symbol,
                market_value = EXCLUDED.market_value,
                cost = EXCLUDED.cost,
                quantity = EXCLUDED.quantity,
                buy_price = EXCLUDED.buy_price,
                current_price = EXCLUDED.current_price,
                updated_at = now()
            """,
            {
                "symbol": holding["symbol"],
                "name": holding["name"],
                "account_code": holding["account_code"],
                "market_value": market_value,
                "cost": cost,
                "as_of": as_of,
                "quantity": units,
                "buy_price": buy_price,
                "current_price": unit_nav,
            },
        )
        return True

    cur.execute(
        """
        INSERT INTO pams_holdings_snapshot (
            symbol, name, asset_liability, currency, account_code,
            major_category, minor_category, market_value, cost, as_of,
            quantity, buy_price, current_price
        ) VALUES (
            %(symbol)s, %(name)s, '资产', 'CNY', %(account_code)s,
            'A股', 'A股基金', %(market_value)s, %(cost)s, %(as_of)s,
            %(quantity)s, %(buy_price)s, %(current_price)s
        )
        ON CONFLICT (account_code, name, as_of) DO NOTHING
        """,
        {
            "symbol": holding["symbol"],
            "name": holding["name"],
            "account_code": holding["account_code"],
            "market_value": market_value,
            "cost": cost,
            "as_of": as_of,
            "quantity": units,
            "buy_price": buy_price,
            "current_price": unit_nav,
        },
    )
    return cur.rowcount > 0


def estimate_delta_pct(delta: Decimal, base: Decimal) -> Decimal:
    """Return delta/base, or zero when base is not positive."""
    if base <= 0:
        return Decimal("0").quantize(RATIO)
    return (delta / base).quantize(RATIO, rounding=ROUND_HALF_UP)


def refresh_fund_nav_estimates(
    conn,
    *,
    use_intraday_estimates: bool | None = None,
) -> dict[str, Any]:
    """Fetch fund NAVs, correct confirmed estimates, and write current snapshots."""
    pct_threshold, amount_threshold = review_thresholds()
    session = requests.Session()
    processed: list[dict[str, Any]] = []
    errors: list[str] = []
    inserted_snapshots = 0
    corrected_snapshots = 0
    review_items = 0
    if use_intraday_estimates is None:
        use_intraday_estimates = intraday_estimates_enabled()
    tushare_available = bool(os.environ.get("TUSHARE_TOKEN"))
    tushare_pro: Any | None = None
    if tushare_available:
        try:
            import tushare as ts

            tushare_pro = ts.pro_api(os.environ["TUSHARE_TOKEN"])
        except Exception as exc:  # pragma: no cover - optional runtime dependency/credential edge
            tushare_available = False
            errors.append(f"tushare fund_nav init failed: {exc}")

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        ensure_fund_nav_estimate_table(cur)
        holdings = load_latest_active_fund_holdings(cur)
        for holding in holdings:
            symbol = str(holding["symbol"]).zfill(6)
            market_value = dec(holding.get("market_value"), MONEY)
            quantity = dec(holding.get("quantity"), QUANTITY)
            stored_price = dec(holding.get("current_price"), PRICE)
            confirmed: ConfirmedNav | None = None
            intraday: IntradayEstimate | None = None
            try:
                if tushare_available:
                    confirmed, tushare_error = fetch_confirmed_nav_prefer_tushare(
                        symbol,
                        session=session,
                        pro=tushare_pro,
                    )
                    if tushare_error:
                        errors.append(f"{symbol} {holding['name']}: {tushare_error}; fell back to Eastmoney")
                else:
                    confirmed = fetch_confirmed_nav(symbol, session)
            except Exception as exc:  # pragma: no cover - network edge
                errors.append(f"{symbol} {holding['name']}: confirmed NAV failed: {exc}")
            if use_intraday_estimates:
                try:
                    intraday = fetch_intraday_estimate(symbol, session)
                except Exception as exc:  # pragma: no cover - network edge
                    errors.append(f"{symbol} {holding['name']}: intraday estimate failed: {exc}")

            fallback_nav = confirmed.unit_nav if confirmed else stored_price
            units = infer_units(market_value, quantity, stored_price, fallback_nav)
            item = {
                "symbol": symbol,
                "name": holding["name"],
                "account_code": holding["account_code"],
                "anchor_as_of": holding["as_of"].isoformat() if hasattr(holding["as_of"], "isoformat") else holding["as_of"],
                "anchor_market_value": str(market_value),
                "status": "missing_nav",
            }

            if confirmed:
                precise_value = estimate_value(units, confirmed.unit_nav)
                existing = existing_estimate(
                    cur,
                    as_of=confirmed.nav_date,
                    account_code=holding["account_code"],
                    symbol=symbol,
                    name=holding["name"],
                )
                prior_estimated_value = dec(existing.get("estimated_market_value"), MONEY) if existing else Decimal("0.00")
                correction_delta = precise_value - prior_estimated_value if prior_estimated_value > 0 else Decimal("0.00")
                status = "confirmed"
                note = "confirmed NAV stored"
                if prior_estimated_value > 0:
                    status = deviation_status(
                        correction_delta,
                        prior_estimated_value,
                        pct_threshold=pct_threshold,
                        amount_threshold=amount_threshold,
                    )
                    if status == "needs_review":
                        note = "confirmed NAV differs materially from prior estimate"
                        review_items += 1
                    else:
                        note = "prior estimate corrected with confirmed NAV"
                        if insert_or_update_generated_snapshot(
                            cur,
                            holding=holding,
                            as_of=confirmed.nav_date,
                            market_value=precise_value,
                            units=units,
                            unit_nav=confirmed.unit_nav,
                            update_existing=True,
                        ):
                            corrected_snapshots += 1
                            status = "confirmed_corrected"
                upsert_estimate_row(
                    cur,
                    {
                        "as_of": confirmed.nav_date,
                        "account_code": holding["account_code"],
                        "symbol": symbol,
                        "name": holding["name"],
                        "confirmed_nav_date": confirmed.nav_date,
                        "confirmed_unit_nav": confirmed.unit_nav,
                        "confirmed_growth_pct": confirmed.growth_pct,
                        "estimate_time": None,
                        "estimated_unit_nav": None,
                        "estimated_growth_pct": None,
                        "anchor_as_of": holding["as_of"],
                        "anchor_market_value": market_value,
                        "anchor_unit_nav": fallback_nav,
                        "estimated_market_value": precise_value,
                        "estimated_delta": correction_delta,
                        "estimated_delta_pct": estimate_delta_pct(correction_delta, prior_estimated_value),
                        "status": status,
                        "note": note,
                        "raw_json": psycopg2.extras.Json(
                            {
                                "source": confirmed.source,
                                "ts_code": confirmed.ts_code or normalize_tushare_fund_ts_code(symbol),
                            }
                        ),
                    },
                )
                upsert_asset_market_daily_nav(
                    cur,
                    holding=holding,
                    as_of=confirmed.nav_date,
                    unit_nav=confirmed.unit_nav,
                    source=confirmed.source,
                    raw={
                        "source": confirmed.source,
                        "ts_code": confirmed.ts_code or normalize_tushare_fund_ts_code(symbol),
                        "unit_nav": str(confirmed.unit_nav),
                        "accumulated_nav": str(confirmed.accumulated_nav)
                        if confirmed.accumulated_nav is not None
                        else None,
                        "growth_pct": str(confirmed.growth_pct) if confirmed.growth_pct is not None else None,
                    },
                )
                item.update(
                    {
                        "confirmed_date": confirmed.nav_date.isoformat(),
                        "confirmed_unit_nav": str(confirmed.unit_nav),
                        "confirmed_market_value": str(precise_value),
                        "confirmed_status": status,
                        "confirmed_source": confirmed.source,
                    }
                )

            if intraday:
                estimate_date = intraday.estimate_time.date()
                estimate_mv = estimate_value(units, intraday.estimated_unit_nav)
                delta = estimate_mv - market_value
                wrote = insert_or_update_generated_snapshot(
                    cur,
                    holding=holding,
                    as_of=estimate_date,
                    market_value=estimate_mv,
                    units=units,
                    unit_nav=intraday.estimated_unit_nav,
                    update_existing=True,
                )
                if wrote:
                    inserted_snapshots += 1
                upsert_estimate_row(
                    cur,
                    {
                        "as_of": estimate_date,
                        "account_code": holding["account_code"],
                        "symbol": symbol,
                        "name": holding["name"],
                        "confirmed_nav_date": confirmed.nav_date if confirmed else intraday.previous_nav_date,
                        "confirmed_unit_nav": confirmed.unit_nav if confirmed else intraday.previous_unit_nav,
                        "confirmed_growth_pct": confirmed.growth_pct if confirmed else None,
                        "estimate_time": intraday.estimate_time,
                        "estimated_unit_nav": intraday.estimated_unit_nav,
                        "estimated_growth_pct": intraday.estimated_growth_pct,
                        "anchor_as_of": holding["as_of"],
                        "anchor_market_value": market_value,
                        "anchor_unit_nav": fallback_nav,
                        "estimated_market_value": estimate_mv,
                        "estimated_delta": delta,
                        "estimated_delta_pct": estimate_delta_pct(delta, market_value),
                        "status": "estimated" if wrote else "estimated_existing_snapshot_kept",
                        "note": "intraday estimate; will be corrected by confirmed NAV later",
                        "raw_json": psycopg2.extras.Json({"source": "fundgz_tiantian"}),
                    },
                )
                upsert_asset_market_daily_nav(
                    cur,
                    holding=holding,
                    as_of=estimate_date,
                    unit_nav=confirmed.unit_nav if confirmed else intraday.previous_unit_nav,
                    estimated_unit_nav=intraday.estimated_unit_nav,
                    source="tiantian_fundgz_estimate",
                    raw={
                        "source": "fundgz_tiantian",
                        "estimate_time": intraday.estimate_time.isoformat(sep=" "),
                        "previous_nav_date": intraday.previous_nav_date.isoformat()
                        if intraday.previous_nav_date
                        else None,
                        "previous_unit_nav": str(intraday.previous_unit_nav)
                        if intraday.previous_unit_nav is not None
                        else None,
                        "estimated_unit_nav": str(intraday.estimated_unit_nav),
                        "estimated_growth_pct": str(intraday.estimated_growth_pct)
                        if intraday.estimated_growth_pct is not None
                        else None,
                    },
                )
                item.update(
                    {
                        "estimate_date": estimate_date.isoformat(),
                        "estimate_time": intraday.estimate_time.isoformat(sep=" "),
                        "estimated_unit_nav": str(intraday.estimated_unit_nav),
                        "estimated_market_value": str(estimate_mv),
                        "estimated_delta": str(delta),
                        "status": "estimated" if wrote else "estimated_existing_snapshot_kept",
                    }
                )
            elif confirmed:
                # Keep all active funds represented on the current max snapshot date.
                copy_date = date.today()
                copied_value = estimate_value(units, confirmed.unit_nav)
                wrote = insert_or_update_generated_snapshot(
                    cur,
                    holding=holding,
                    as_of=copy_date,
                    market_value=copied_value,
                    units=units,
                    unit_nav=confirmed.unit_nav,
                    update_existing=False,
                )
                if wrote:
                    inserted_snapshots += 1
                item.update({"status": "confirmed_no_intraday_estimate"})

            processed.append(item)
        conn.commit()

    return {
        "status": "ok",
        "processed": len(processed),
        "inserted_estimate_snapshots": inserted_snapshots,
        "corrected_confirmed_snapshots": corrected_snapshots,
        "manual_review_items": review_items,
        "intraday_estimates_enabled": use_intraday_estimates,
        "review_threshold_pct": str(pct_threshold),
        "review_threshold_cny": str(amount_threshold),
        "rows": processed,
        "errors": errors,
    }
