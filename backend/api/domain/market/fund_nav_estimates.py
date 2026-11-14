"""A-share fund NAV estimation and correction helpers.

The job has two responsibilities:
- store confirmed unit NAV from Tushare fund_nav when available, with
  Eastmoney F10 as a fallback;
- store intraday estimated NAV from Tiantian Fund after A-share close, then
  correct the same day with confirmed NAV after the US post-close refresh.

Confirmed NAV must only backfill the NAV date it belongs to. A stale confirmed
NAV from the previous trading day must not be copied forward as today's
confirmed return.
"""

from __future__ import annotations

import html
import json
import os
import re
from collections import defaultdict
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any
from zoneinfo import ZoneInfo

import psycopg2.extras
import requests
from domain.investments.daily_return_state import quote_snapshot_from_market_daily, upsert_quote_snapshot
from domain.imports.isolation import source_enabled
from domain.investments.holding_evidence_writer import finish_holding_change, lock_holding_accounts, prepare_holding_change


MONEY = Decimal("0.01")
QUANTITY = Decimal("0.000001")
PRICE = Decimal("0.000001")
RATIO = Decimal("0.000001")

NAV_HISTORY_DAYS = 14
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
    return parsed if parsed.is_finite() and parsed != 0 else None


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


def _parse_tushare_nav_history(df: Any) -> list[ConfirmedNav]:
    """Parse one current value per published NAV date, newest first."""
    if df is None or getattr(df, "empty", True):
        return []
    rows = df.copy()
    if "unit_nav" not in rows:
        return []
    date_column = next((column for column in ("nav_date", "end_date") if column in rows), None)
    if not date_column:
        return []
    sort_columns = [date_column]
    if "ann_date" in rows:
        sort_columns.append("ann_date")
    rows = rows.sort_values(sort_columns, ascending=False)
    by_date: dict[date, ConfirmedNav] = {}
    for _, row in rows.iterrows():
        unit_nav = optional_dec(row.get("unit_nav"), PRICE)
        if unit_nav is None or unit_nav <= 0:
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
        by_date.setdefault(nav_date, ConfirmedNav(
            nav_date=nav_date,
            unit_nav=unit_nav,
            accumulated_nav=optional_dec(row.get("accum_nav"), PRICE),
            growth_pct=None,
            source="tushare_fund_nav",
            ts_code=str(row.get("ts_code") or ""),
        ))
    return list(by_date.values())


def parse_tushare_fund_nav_frame(df: Any) -> ConfirmedNav | None:
    rows = _parse_tushare_nav_history(df)
    return rows[0] if rows else None


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
    from domain.imports.isolation import require_external_access
    require_external_access()
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
    from domain.imports.isolation import require_external_access
    require_external_access()
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


def fetch_tushare_nav_history(symbol, *, pro, start, end):
    from domain.imports.isolation import require_external_access
    require_external_access()
    for code in tushare_fund_ts_code_candidates(symbol):
        frame = pro.fund_nav(
            ts_code=code, start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"),
            fields="ts_code,ann_date,nav_date,end_date,unit_nav,accum_nav",
        )
        rows = [replace(nav, ts_code=code) for nav in _parse_tushare_nav_history(frame)
                if nav.ts_code == code and start <= nav.nav_date <= end]
        if rows:
            return rows
    return []


def fetch_eastmoney_nav_history(symbol, *, session, start, end):
    """Read published unit NAV history from Eastmoney's F10 JSON endpoint."""
    from domain.imports.isolation import require_external_access
    require_external_access()
    if not re.fullmatch(r"\d{6}", symbol):
        raise ValueError("invalid fund symbol")
    response = session.get(
        "https://api.fund.eastmoney.com/f10/lsjz",
        params={"fundCode": symbol, "pageIndex": 1, "pageSize": NAV_HISTORY_DAYS + 1,
                "startDate": start.isoformat(), "endDate": end.isoformat()},
        headers={"User-Agent": "Mozilla/5.0",
                 "Referer": f"https://fundf10.eastmoney.com/jjjz_{symbol}.html"},
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("ErrCode") != 0:
        raise ValueError("Eastmoney fund NAV history unavailable")
    rows = {}
    for row in (payload.get("Data") or {}).get("LSJZList") or []:
        nav_date = date.fromisoformat(row["FSRQ"])
        nav = optional_dec(row.get("DWJZ"), PRICE)
        if start <= nav_date <= end and nav is not None and nav > 0:
            record = ConfirmedNav(nav_date, nav, optional_dec(row.get("LJJZ"), PRICE),
                                  optional_dec(row.get("JZZZL"), RATIO), source="eastmoney_f10")
            if nav_date in rows and rows[nav_date].unit_nav != nav:
                raise ValueError("conflicting Eastmoney NAVs")
            rows[nav_date] = record
    return list(rows.values())


def fetch_confirmed_nav_history(symbol, *, session, pro, start, end, missing_days):
    """Keep the primary source for shared dates and use the backup for missing dates."""
    errors = []
    try:
        primary = fetch_tushare_nav_history(symbol, pro=pro, start=start, end=end) if pro else []
    except Exception as exc:
        errors.append(f"Tushare: {type(exc).__name__}")
        primary = []
    by_date = {nav.nav_date: nav for nav in primary}
    if not by_date or missing_days.difference(by_date):
        try:
            backup = fetch_eastmoney_nav_history(symbol, session=session, start=start, end=end)
            for nav in backup:
                if start <= nav.nav_date <= end:
                    by_date.setdefault(nav.nav_date, nav)
        except Exception as exc:
            errors.append(f"Eastmoney: {type(exc).__name__}")
    return sorted((nav for nav in by_date.values() if start <= nav.nav_date <= end),
                  key=lambda nav: nav.nav_date), errors


def fetch_intraday_estimate(symbol: str, session: requests.Session | None = None) -> IntradayEstimate | None:
    """Fetch current intraday estimated unit NAV for a fund code."""
    from domain.imports.isolation import require_external_access
    require_external_access()
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
    """Require the preinstalled schema without changing database structure."""
    cur.execute("SELECT to_regclass('public.market_observations') AS table_name")
    row = cur.fetchone()
    table = row.get('table_name') if isinstance(row, dict) else row[0] if row else None
    if table is None:
        raise RuntimeError('fund NAV schema unavailable; initialize database schema before refresh')


def load_latest_active_fund_holdings(cur) -> list[dict[str, Any]]:
    """Load each active A-share fund from its own latest snapshot date."""
    cur.execute(
        """
        WITH latest_fund_snapshot AS (
            SELECT MAX(as_of) AS as_of
            FROM pams_holdings_snapshot
            WHERE major_category = 'A股'
              AND minor_category = 'A股基金'
              AND asset_liability = '资产'
        ), ranked_holdings AS (
            SELECT
                h.*,
                ROW_NUMBER() OVER (
                    PARTITION BY h.account_code, h.symbol, h.name
                    ORDER BY h.as_of DESC, h.id DESC
                ) AS rn
            FROM pams_holdings_snapshot h
            CROSS JOIN latest_fund_snapshot latest
            WHERE major_category = 'A股'
              AND minor_category = 'A股基金'
              AND asset_liability = '资产'
              AND latest.as_of IS NOT NULL
              AND h.as_of >= latest.as_of - INTERVAL '14 days'
        )
        SELECT h.id, h.account_code, h.symbol, h.name, h.asset_liability, h.currency,
               h.major_category, h.minor_category, h.market_value, h.cost, h.as_of,
               h.quantity, h.buy_price, h.current_price,
               COALESCE(h.asset_id, matched_asset.asset_id) AS asset_id
        FROM ranked_holdings h
        LEFT JOIN LATERAL (
            SELECT am.asset_id
            FROM pams_asset_master am
            WHERE am.status = 'active'
              AND am.primary_symbol = LPAD(h.symbol, 6, '0')
              AND am.asset_type IN ('ETF / 基金', 'fund_cn', 'fund', '基金')
            ORDER BY
                CASE WHEN am.asset_type = 'ETF / 基金' THEN 0 ELSE 1 END,
                am.updated_at DESC
            LIMIT 1
        ) matched_asset ON TRUE
        WHERE h.rn = 1
          AND COALESCE(h.market_value, 0) >= 0
          AND (h.quantity > 0 OR h.market_value > 0)
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
        FROM (SELECT * FROM market_observations WHERE observation_type = 'fund') AS observation_rows
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
        INSERT INTO market_observations (
            observation_type,
            as_of, account_code, symbol, name,
            confirmed_nav_date, confirmed_unit_nav, confirmed_growth_pct,
            estimate_time, estimated_unit_nav, estimated_growth_pct,
            anchor_as_of, anchor_market_value, anchor_unit_nav,
            estimated_market_value, estimated_delta, estimated_delta_pct,
            status, note, raw_json
        ) VALUES ('fund',
            %(as_of)s, %(account_code)s, %(symbol)s, %(name)s,
            %(confirmed_nav_date)s, %(confirmed_unit_nav)s, %(confirmed_growth_pct)s,
            %(estimate_time)s, %(estimated_unit_nav)s, %(estimated_growth_pct)s,
            %(anchor_as_of)s, %(anchor_market_value)s, %(anchor_unit_nav)s,
            %(estimated_market_value)s, %(estimated_delta)s, %(estimated_delta_pct)s,
            %(status)s, %(note)s, %(raw_json)s
        )
        ON CONFLICT (as_of, account_code, symbol, name) WHERE observation_type = 'fund' DO UPDATE SET
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
        INSERT INTO market_observations (
            observation_type,
            as_of, source, source_key, asset_id, symbol, name, market, currency,
            unit_nav, estimated_unit_nav, confidence, raw_json
        ) VALUES ('daily',
            %(as_of)s, %(source)s, %(source_key)s, %(asset_id)s, %(symbol)s, %(name)s,
            '基金', 'CNY', %(unit_nav)s, %(estimated_unit_nav)s, %(confidence)s, %(raw_json)s
        )
        ON CONFLICT (as_of, source, source_key) WHERE observation_type = 'daily' DO UPDATE SET
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
    lock_holding_accounts(cur, [holding['account_code']])
    isolated = source_enabled(cur)
    if isolated:
        keys = ('account_code','name','symbol','currency','as_of','quantity','cost','buy_price','market_value','current_price')
        if not holding.get('id'):
            raise ValueError('fund source holding identity missing')
        cur.execute('SELECT ' + ','.join(keys) + ',xmin::text=pg_current_xact_id()::text AS own_write '
                    'FROM pams_holdings_snapshot WHERE id=%s FOR UPDATE', (holding['id'],))
        stored = cur.fetchone()
        own_write = (stored['own_write'] if isinstance(stored, dict) else stored[len(keys)]) if stored else False
        compared = keys[:7] if own_write else keys
        if not stored or any((stored[key] if isinstance(stored, dict) else stored[index]) != holding.get(key)
                             for index,key in enumerate(compared)):
            raise ValueError('fund source holding changed; refresh the input')
        cur.execute('SELECT id,quantity,cost FROM pams_holdings_snapshot WHERE account_code=%s AND name=%s AND as_of=%s FOR UPDATE',
                    (holding['account_code'], holding['name'], as_of))
        target = cur.fetchone()
        if target:
            target_id = target['id'] if isinstance(target, dict) else target[0]
            target_quantity = target['quantity'] if isinstance(target, dict) else target[1]
            target_cost = target['cost'] if isinstance(target, dict) else target[2]
            if target_id != holding['id'] and (target_quantity != holding.get('quantity') or target_cost != holding.get('cost')):
                raise ValueError('newer holding input conflicts with fund valuation')
        prepare_holding_change(cur, account_code=holding['account_code'], name=holding['name'],
                               as_of=as_of, source_item_id=None)
    cost = dec(holding.get("cost"), MONEY)
    buy_price = (cost / units).quantize(PRICE, rounding=ROUND_HALF_UP) if units > 0 and cost > 0 else None
    if update_existing:
        cur.execute(
            """
            INSERT INTO pams_holdings_snapshot (
                symbol, name, asset_liability, currency, account_code,
                major_category, minor_category, market_value, cost, as_of,
                quantity, buy_price, current_price, asset_id
            ) VALUES (
                %(symbol)s, %(name)s, '资产', 'CNY', %(account_code)s,
                'A股', 'A股基金', %(market_value)s, %(cost)s, %(as_of)s,
                %(quantity)s, %(buy_price)s, %(current_price)s, %(asset_id)s
            )
            ON CONFLICT (account_code, name, as_of) DO UPDATE SET
                symbol = EXCLUDED.symbol,
                market_value = EXCLUDED.market_value,
                cost = EXCLUDED.cost,
                quantity = EXCLUDED.quantity,
                buy_price = EXCLUDED.buy_price,
                current_price = EXCLUDED.current_price,
                asset_id = COALESCE(EXCLUDED.asset_id, pams_holdings_snapshot.asset_id),
                updated_at = now()
            """ + (""" WHERE (pams_holdings_snapshot.symbol,pams_holdings_snapshot.market_value,
                pams_holdings_snapshot.cost,pams_holdings_snapshot.quantity,pams_holdings_snapshot.buy_price,
                pams_holdings_snapshot.current_price,pams_holdings_snapshot.asset_id)
                IS DISTINCT FROM (EXCLUDED.symbol,EXCLUDED.market_value,EXCLUDED.cost,EXCLUDED.quantity,
                EXCLUDED.buy_price,EXCLUDED.current_price,COALESCE(EXCLUDED.asset_id,pams_holdings_snapshot.asset_id))
                RETURNING id""" if isolated else ""),
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
                "asset_id": holding.get("asset_id"),
            },
        )
        if isolated:
            row = cur.fetchone()
            if row:
                identifier = row['id'] if isinstance(row, dict) else row[0]
                finish_holding_change(cur, holding_id=identifier, source_item_id=None,
                                      input_refs=[{'kind':'quote', 'key':None, 'role':'valuation_quote'}])
        return True

    cur.execute(
        """
        INSERT INTO pams_holdings_snapshot (
            symbol, name, asset_liability, currency, account_code,
            major_category, minor_category, market_value, cost, as_of,
            quantity, buy_price, current_price, asset_id
        ) VALUES (
            %(symbol)s, %(name)s, '资产', 'CNY', %(account_code)s,
            'A股', 'A股基金', %(market_value)s, %(cost)s, %(as_of)s,
            %(quantity)s, %(buy_price)s, %(current_price)s, %(asset_id)s
        )
        ON CONFLICT (account_code, name, as_of) DO NOTHING
        """ + (" RETURNING id" if isolated else ""),
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
            "asset_id": holding.get("asset_id"),
        },
    )
    if isolated:
        row = cur.fetchone()
        if row:
            identifier = row['id'] if isinstance(row, dict) else row[0]
            finish_holding_change(cur, holding_id=identifier, source_item_id=None,
                                  input_refs=[{'kind':'quote', 'key':None, 'role':'valuation_quote'}])
        return bool(row)
    return cur.rowcount > 0


def estimate_delta_pct(delta: Decimal, base: Decimal) -> Decimal:
    """Return delta/base, or zero when base is not positive."""
    if base <= 0:
        return Decimal("0").quantize(RATIO)
    return (delta / base).quantize(RATIO, rounding=ROUND_HALF_UP)


def should_copy_confirmed_nav_to_current_snapshot(confirmed_nav_date: date, current_date: date) -> bool:
    """Only same-day confirmed NAV can be copied into the current snapshot."""
    return confirmed_nav_date == current_date


def current_china_fund_date() -> date:
    """Return the fund trading date boundary used by the Beijing post-close job."""
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def _fund_history_days(current_date):
    import exchange_calendars as xcals

    start = current_date - timedelta(days=NAV_HISTORY_DAYS)
    calendar = xcals.get_calendar("XSHG")
    return {day.date() for day in calendar.sessions_in_range(start.isoformat(), current_date.isoformat())}


def _stored_nav_prices(cur, symbol, start, end):
    cur.execute("""SELECT as_of, unit_nav FROM market_observations
        WHERE observation_type='daily' AND market='基金' AND currency='CNY' AND symbol=%s
          AND as_of BETWEEN %s AND %s AND source IN ('tushare_fund_nav','eastmoney_f10')
          AND unit_nav>0 AND unit_nav::text NOT IN ('NaN','Infinity','-Infinity')""",
                (symbol, start, end))
    prices = defaultdict(set)
    for row in cur.fetchall():
        prices[row['as_of']].add(row['unit_nav'])
    return prices


def _fund_history_holdings(cur, holding, start):
    cur.execute("""SELECT * FROM pams_holdings_snapshot
        WHERE account_code=%s AND symbol=%s AND currency='CNY'
          AND as_of BETWEEN %s AND %s ORDER BY as_of,id""",
                (holding['account_code'], holding['symbol'], start, holding['as_of']))
    rows = defaultdict(list)
    for row in cur.fetchall():
        rows[row['as_of']].append(dict(row))
    return rows


def _historical_holding_basis(cur, holding, snapshots, day):
    """Require dated position evidence or explicitly reconciled trade quantities."""
    if day in snapshots:
        rows = snapshots[day]
        return rows[0] if len(rows) == 1 and rows[0]['name'] == holding['name'] else None
    prior_days = [known_day for known_day in snapshots if known_day < day]
    if prior_days:
        rows = snapshots[max(prior_days)]
        if len(rows) != 1:
            return None
        basis = rows[0]
    else:
        basis = holding
    if basis['name'] != holding['name']:
        return None
    first, last = sorted((basis['as_of'], day))
    cur.execute("""SELECT COUNT(*) FILTER (WHERE GREATEST(trade_date, COALESCE(settle_date,trade_date))>%s
                    AND LEAST(trade_date,COALESCE(settle_date,trade_date))<=%s) AS changes,
            COALESCE(SUM(CASE WHEN trade_type IN ('fund_subscription','buy') THEN ABS(quantity)
                             WHEN trade_type IN ('fund_redemption','sell') THEN -ABS(quantity)
                             ELSE 0 END) FILTER (
                WHERE GREATEST(trade_date,COALESCE(settle_date,trade_date))<=%s),0) AS proven_units,
            COUNT(*) FILTER (WHERE GREATEST(trade_date,COALESCE(settle_date,trade_date))<=%s
                  AND (quantity IS NULL OR execution_status IS DISTINCT FROM 'confirmed'
                       OR source_item_id IS NULL OR trade_type IS NULL
                       OR trade_type NOT IN ('fund_subscription','fund_redemption','buy','sell'))) AS unverified
        FROM pams_investment_trade_fact
        WHERE account_code=%s AND symbol=%s""",
                (first, last, day, day, holding['account_code'], holding['symbol']))
    trades = cur.fetchone()
    if trades['changes'] or trades['unverified'] or trades['proven_units'] != basis['quantity']:
        return None
    return basis


def _backfill_fund_nav(cur, holding, history, known_prices, start):
    """Add confirmed observations and historical positions supported by account evidence."""
    if not history:
        return 0, 0, [], []
    snapshots = _fund_history_holdings(cur, holding, start)
    observations = valuations = 0
    pending_days = []
    conflict_days = []
    for nav in history:
        day = nav.nav_date
        prior = known_prices.get(day, set())
        if prior and prior != {nav.unit_nav}:
            conflict_days.append(day.isoformat())
            continue
        raw = {"source": nav.source, "ts_code": nav.ts_code, "nav_date": day.isoformat(),
               "unit_nav": str(nav.unit_nav)}
        if not prior:
            upsert_asset_market_daily_nav(cur, holding=holding, as_of=day,
                                          unit_nav=nav.unit_nav, source=nav.source, raw=raw)
            known_prices.setdefault(day, set()).add(nav.unit_nav)
            observations += 1
        existing = existing_estimate(cur, as_of=day, account_code=holding['account_code'],
                                     symbol=holding['symbol'], name=holding['name'])
        if existing:
            if (existing['status'] not in {'confirmed', 'confirmed_corrected'}
                    or existing['confirmed_unit_nav'] != nav.unit_nav
                    or existing['confirmed_nav_date'] != day or day not in snapshots
                    or len(snapshots[day]) != 1 or snapshots[day][0]['current_price'] != nav.unit_nav):
                pending_days.append(day.isoformat())
            continue
        basis = _historical_holding_basis(cur, holding, snapshots, day)
        if (not basis or not basis['quantity'] or basis['quantity'] <= 0 or basis['cost'] is None
                or (day in snapshots and basis['current_price'] != nav.unit_nav)):
            pending_days.append(day.isoformat())
            continue
        value = estimate_value(basis['quantity'], nav.unit_nav)
        if day not in snapshots:
            if not insert_or_update_generated_snapshot(cur, holding=basis, as_of=day, market_value=value,
                                                        units=basis['quantity'], unit_nav=nav.unit_nav,
                                                        update_existing=False):
                pending_days.append(day.isoformat())
                continue
            valuations += 1
        upsert_estimate_row(cur, {
            "as_of": day, "account_code": holding['account_code'], "symbol": holding['symbol'],
            "name": holding['name'], "confirmed_nav_date": day, "confirmed_unit_nav": nav.unit_nav,
            "confirmed_growth_pct": nav.growth_pct, "estimate_time": None, "estimated_unit_nav": None,
            "estimated_growth_pct": None, "anchor_as_of": basis['as_of'],
            "anchor_market_value": basis['market_value'], "anchor_unit_nav": basis['current_price'],
            "estimated_market_value": value, "estimated_delta": Decimal(0), "estimated_delta_pct": Decimal(0),
            "status": "confirmed", "note": "Published historical fund NAV",
            "raw_json": psycopg2.extras.Json({**raw, "holding_anchor_id": basis['id']}),
        })
    return observations, valuations, pending_days, conflict_days


def refresh_fund_nav_estimates(
    conn,
    *,
    use_intraday_estimates: bool | None = None,
) -> dict[str, Any]:
    """Fetch fund NAVs, correct confirmed estimates, and write current snapshots."""
    from domain.imports.isolation import require_external_access
    require_external_access()
    pct_threshold, amount_threshold = review_thresholds()
    session = requests.Session()
    processed: list[dict[str, Any]] = []
    errors: list[str] = []
    inserted_snapshots = 0
    corrected_snapshots = 0
    review_items = 0
    if use_intraday_estimates is None:
        use_intraday_estimates = intraday_estimates_enabled()
    current_date = current_china_fund_date()
    start = current_date - timedelta(days=NAV_HISTORY_DAYS)
    expected_days = _fund_history_days(current_date)
    # The newest session is checked even when an older observation is already stored.
    latest_session = max(expected_days) if expected_days else None
    history_by_symbol = {}
    history_observations = history_valuations = 0
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
        # Writers retain transaction locks until commit; use one account order.
        holdings.sort(key=lambda row: (str(row.get('account_code') or ''), str(row.get('name') or ''), str(row.get('id') or '')))
        for symbol in sorted({str(row["symbol"]).zfill(6) for row in holdings}):
            prices = _stored_nav_prices(cur, symbol, start, current_date)
            missing_days = expected_days - prices.keys()
            if latest_session is not None:
                missing_days.add(latest_session)
            history, provider_errors = fetch_confirmed_nav_history(
                symbol, session=session, pro=tushare_pro, start=start, end=current_date,
                missing_days=missing_days,
            )
            history_by_symbol[symbol] = history, prices
            errors.extend(f"{symbol}: {error}" for error in provider_errors)

        lock_holding_accounts(cur, [row['account_code'] for row in holdings])
        for holding in holdings:
            symbol = str(holding["symbol"]).zfill(6)
            market_value = dec(holding.get("market_value"), MONEY)
            quantity = dec(holding.get("quantity"), QUANTITY)
            stored_price = dec(holding.get("current_price"), PRICE)
            intraday: IntradayEstimate | None = None
            history, prices = history_by_symbol[symbol]
            confirmed = history[-1] if history else None
            historical = history[:-1]
            if confirmed and confirmed.nav_date < holding['as_of']:
                historical = history
                confirmed = None
            observations, valuations, pending, conflicts = _backfill_fund_nav(
                cur, holding, historical, prices, start,
            )
            history_observations += observations
            history_valuations += valuations
            if confirmed and prices.get(confirmed.nav_date, {confirmed.unit_nav}) != {confirmed.unit_nav}:
                conflicts.append(confirmed.nav_date.isoformat())
                confirmed = None
            missing_dates = set(expected_days) - prices.keys() - {nav.nav_date for nav in history}
            errors.extend(f"{symbol}: confirmed NAV conflict on {day}" for day in conflicts)
            if use_intraday_estimates:
                try:
                    intraday = fetch_intraday_estimate(symbol, session)
                    if intraday and (intraday.estimate_time.date() != current_date
                                     or (confirmed and confirmed.nav_date >= current_date)):
                        intraday = None
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
                "status": "historical_nav_checked" if history else "missing_nav",
                "missing_nav_dates": sorted(day.isoformat() for day in missing_dates),
                "pending_valuation_dates": pending,
            }
            if history and confirmed is None:
                item["confirmed_date"] = history[-1].nav_date.isoformat()

            if confirmed:
                precise_value = estimate_value(units, confirmed.unit_nav)
                existing = existing_estimate(
                    cur,
                    as_of=confirmed.nav_date,
                    account_code=holding["account_code"],
                    symbol=symbol,
                    name=holding["name"],
                )
                if (not intraday and existing and holding["as_of"] >= confirmed.nav_date
                        and existing.get("status") in {"confirmed", "confirmed_corrected"}
                        and existing.get("confirmed_unit_nav") == confirmed.unit_nav
                        and confirmed.unit_nav in prices.get(confirmed.nav_date, ())
                        and (holding["as_of"] > confirmed.nav_date or stored_price == confirmed.unit_nav)):
                    item.update(confirmed_date=confirmed.nav_date.isoformat(),
                                confirmed_unit_nav=str(confirmed.unit_nav),
                                confirmed_source=confirmed.source, status="confirmed_unchanged")
                    processed.append(item)
                    continue
                # Confirmed rows also carry a market value. It is not an estimate.
                prior_estimated_value = Decimal("0.00")
                if existing:
                    if existing.get("estimated_unit_nav") is not None:
                        prior_estimated_value = dec(existing.get("estimated_market_value"), MONEY)
                    elif existing.get("status") == "needs_review":
                        prior_estimated_value = (dec(existing.get("estimated_market_value"), MONEY)
                                                 - dec(existing.get("estimated_delta"), MONEY))
                correction_delta = precise_value - prior_estimated_value if prior_estimated_value > 0 else Decimal("0.00")
                status = "confirmed"
                note = "confirmed NAV stored"
                if existing and existing.get("status") == "confirmed_corrected":
                    status = "confirmed_corrected"
                    note = "prior estimate corrected with confirmed NAV"
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
            if confirmed and status != "needs_review":
                wrote = False
                if holding['as_of'] < confirmed.nav_date <= current_date:
                    wrote = insert_or_update_generated_snapshot(
                        cur,
                        holding=holding,
                        as_of=confirmed.nav_date,
                        market_value=precise_value,
                        units=units,
                        unit_nav=confirmed.unit_nav,
                        update_existing=False,
                    )
                    inserted_snapshots += int(wrote)
                if not intraday:
                    item["status"] = ("confirmed_no_intraday_estimate" if wrote or confirmed.nav_date == current_date
                                      else "confirmed_stale_no_intraday_estimate")

            processed.append(item)
        conn.commit()

    return {
        "status": "ok",
        "processed": len(processed),
        "inserted_estimate_snapshots": inserted_snapshots,
        "corrected_confirmed_snapshots": corrected_snapshots,
        "historical_nav_observations": history_observations,
        "historical_valuation_snapshots": history_valuations,
        "manual_review_items": review_items,
        "intraday_estimates_enabled": use_intraday_estimates,
        "review_threshold_pct": str(pct_threshold),
        "review_threshold_cny": str(amount_threshold),
        "rows": processed,
        "errors": errors,
    }
