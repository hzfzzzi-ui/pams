"""Dated overseas close returns; never replace the active daily bucket."""
from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from domain.market.overseas_return_calendar import (
    MARKET_CURRENCIES,
    MARKET_LABELS,
    latest_completed_session,
    market_bucket,
    market_calendar,
    return_session_date,
)

RETURN_FIELDS = (
    "daily_pnl", "daily_pnl_native", "daily_pnl_cny", "daily_fx_pnl_cny",
    "daily_total_pnl_cny", "price_change_pct", "price_change_amount", "previous_price",
)
logger = logging.getLogger(__name__)


def _decimal(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _key(row: dict) -> tuple[str, str]:
    return (str(row.get("account") or row.get("account_code") or ""), str(row.get("symbol") or ""))


def _us_position(row: dict) -> bool:
    return (bool(_key(row)[0]) and bool(row.get("symbol"))
            and row.get("major_category") == "美股" and row.get("currency") == "USD"
            and market_bucket(row) == "us"
            and not row.get("is_closed") and not str(row.get("symbol") or "").startswith("CASH_")
            and (_decimal(row.get("quantity")) or Decimal(0)) > 0)


def attach_recent_us_returns(
    cur, payload: dict[str, Any], *, active_date: date,
    load_date: Callable[[date], dict[str, Any]],
) -> dict[str, Any]:
    """Replay the existing reader once at the latest evidenced prior US close.

    This deliberately does not clamp the active date or change daily_pnl.
    Historical FX, ownership and quantities come from that dated reader, not
    from today's valuation or a frontend calculation.
    """
    rows = payload.get("rows") or []
    pending = [r for r in rows if _us_position(r) and
               (r.get("daily_pnl_stale") is True or r.get("daily_return_status") == "pending")]
    if not pending:
        return payload
    cur.execute("SELECT to_regclass('public.market_observations') IS NOT NULL AS exists")
    if not (cur.fetchone() or {}).get("exists"):
        return payload
    cur.execute("""
        SELECT MAX(quote_date) AS as_of FROM (SELECT * FROM market_observations WHERE observation_type = 'quote') AS observation_rows
        WHERE market = 'US' AND currency = 'USD' AND return_status = 'confirmed'
          AND quote_date < %s AND symbol = ANY(%s::text[])
          AND price > 0 AND price::text NOT IN ('NaN', 'Infinity', '-Infinity')
    """, (active_date, sorted({str(r["symbol"]) for r in pending})))
    raw_date = (cur.fetchone() or {}).get("as_of")
    if not raw_date:
        return payload
    close_date = date.fromisoformat(str(raw_date)[:10])
    if close_date >= active_date:
        return payload
    try:
        historical = {_key(r): r for r in load_date(close_date).get("rows", []) if _us_position(r)}
    except Exception as exc:
        # Optional historical display must not take the current holdings offline.
        logger.warning("Recent US close replay unavailable (%s)", type(exc).__name__)
        return payload
    attached: dict[tuple[str, str], dict] = {}
    short = close_date.strftime("%m/%d")
    for current in pending:
        row = historical.get(_key(current))
        if (not row or row.get("daily_pnl_stale") or row.get("daily_return_status") != "confirmed"
                or not row.get("daily_return_status_source")
                or str(row.get("daily_return_status_as_of"))[:10] != close_date.isoformat()
                or str(row.get("snapshot_as_of") or row.get("as_of"))[:10] != close_date.isoformat()
                or any(_decimal(row.get(field)) is None for field in RETURN_FIELDS)
                or (_decimal(row.get("previous_price")) or Decimal(0)) <= 0):
            continue
        attached[_key(current)] = {
            "as_of": close_date.isoformat(), "status": "confirmed", "label": f"{short}收益",
            "status_label": f"{short}已确认", "source": row.get("daily_return_status_source"),
            "current_as_of": active_date.isoformat(), "current_status": "pending",
            "current_label": f"{active_date:%m/%d}预估待更新",
            "fields": {field: row[field] for field in RETURN_FIELDS},
        }
    if not attached:
        return payload
    output = dict(payload)
    output["rows"] = [dict(r, recent_confirmed_return=attached[_key(r)]) if _key(r) in attached else r for r in rows]
    accounts: dict[str, list[dict]] = defaultdict(list)
    for (account, _), display in attached.items():
        accounts[account].append(display)
    output["recent_confirmed_returns"] = {"by_account": {}}
    for account, values in accounts.items():
        positions = [r for r in rows if _key(r)[0] == account and not r.get("is_closed")
                     and (_decimal(r.get("quantity")) or Decimal(0)) > 0
                     and not str(r.get("symbol") or "").startswith("CASH_")]
        amount = sum((Decimal(v["fields"]["daily_total_pnl_cny"]) for v in values), Decimal(0))
        output["recent_confirmed_returns"]["by_account"][account] = {
            "as_of": close_date.isoformat(), "amount_cny": str(amount),
            "label": f"{short}收益", "partial": len(values) < len(positions),
            "covered": len(values), "expected": len(positions),
            "current_as_of": active_date.isoformat(), "current_status": "pending",
            "title": f"美股 {close_date} 已确认收益；{active_date} 预估待更新，未计入今日汇总",
        }
    return output


def attach_recent_market_returns(
    cur, payload: dict[str, Any], *, active_date: date,
    load_date: Callable[[date], dict[str, Any]], now: datetime | None = None,
) -> dict[str, Any]:
    """Keep each listing's dated close separate from the portfolio daily bucket.

    Older non-US imports lack quote-state records. A complete, consecutive pair
    of persisted closes can corroborate the existing dated reader; it never
    supplies a new PnL formula or promotes an unclosed session to confirmed.
    """
    current_time = now or datetime.now(UTC)
    history: dict[date, dict[str, Any]] = {}
    unavailable: dict[date, Exception] = {}

    def load(day):
        if day in unavailable:
            raise unavailable[day]
        if day not in history:
            try:
                history[day] = load_date(day)
            except Exception as exc:  # noqa: BLE001 - cache one optional replay failure per date
                unavailable[day] = exc
                raise
        return history[day]

    output = attach_recent_us_returns(cur, payload, active_date=active_date, load_date=load)
    if unavailable:
        return output
    rows = output.get("rows") or []
    pending = [r for r in rows if r.get("major_category") == "美股"
               and all(_key(r))
               and market_bucket(r) in {"hk", "kr", "se", "ch"}
               and MARKET_CURRENCIES.get(market_bucket(r) or "") == r.get("currency")
               and not r.get("is_closed") and (_decimal(r.get("quantity")) or Decimal(0)) > 0
               and (r.get("daily_pnl_stale") is True or r.get("daily_return_status") == "pending")]
    if not pending:
        return output
    completed = {bucket: latest_completed_session(bucket, current_time)
                 for bucket in {market_bucket(r) for r in pending} if bucket}
    valid_dates = [day for day in completed.values() if day]
    if not valid_dates:
        return output
    cur.execute("SELECT to_regclass('public.pams_ohlcv_daily') IS NOT NULL AS exists")
    if not (cur.fetchone() or {}).get("exists"):
        return output
    ceilings: dict[str, date] = {}
    for row in pending:
        bucket = market_bucket(row)
        through_date = completed.get(bucket) if bucket else None
        if through_date:
            try:
                represented_date = date.fromisoformat(str(row.get("snapshot_as_of") or row.get("as_of"))[:10])
            except (TypeError, ValueError):
                continue
            symbol = str(row["symbol"])
            ceiling = min(through_date, represented_date)
            ceilings[symbol] = max(ceilings.get(symbol, ceiling), ceiling)
    if not ceilings:
        return output
    symbols = sorted(ceilings)
    cur.execute("""
        WITH requested AS (SELECT * FROM unnest(%s::text[], %s::date[]) AS r(symbol, through_date))
        SELECT symbol, trade_date, close FROM (
            SELECT o.symbol, o.trade_date, o.close,
                   ROW_NUMBER() OVER (PARTITION BY o.symbol ORDER BY o.trade_date DESC) AS rn
            FROM pams_ohlcv_daily o JOIN requested r ON r.symbol = o.symbol
            WHERE o.market = 'YF' AND o.trade_date <= r.through_date
        ) ranked WHERE rn <= 2 ORDER BY symbol, trade_date DESC
    """, (symbols, [ceilings[symbol] for symbol in symbols]))
    closes: dict[str, list[dict]] = defaultdict(list)
    for row in cur.fetchall():
        closes[str(row["symbol"])].append(dict(row))
    added = {}
    for row in pending:
        bucket = market_bucket(row)
        pair = closes.get(str(row.get("symbol")), [])
        close_cutoff = completed.get(bucket) if bucket else None
        if len(pair) != 2 or not bucket or not close_cutoff:
            continue
        close_date = date.fromisoformat(str(pair[0]["trade_date"])[:10])
        previous_date = date.fromisoformat(str(pair[1]["trade_date"])[:10])
        if close_date > close_cutoff:
            continue
        calendar = market_calendar(bucket)
        if not calendar.is_session(close_date.isoformat()):
            continue
        if calendar.previous_session(close_date.isoformat()).date() != previous_date:
            continue
        if any((_decimal(p.get("close")) or Decimal(0)) <= 0 for p in pair):
            continue
        try:
            historical = next((r for r in load(close_date).get("rows", []) if _key(r) == _key(row)), None)
        except Exception as exc:  # noqa: BLE001 - optional dated display must preserve current holdings
            logger.warning("Recent market close replay unavailable (%s)", type(exc).__name__)
            continue
        if (not historical or historical.get("daily_pnl_stale")
                or str(historical.get("snapshot_as_of") or historical.get("as_of"))[:10] != str(close_date)
                or _decimal(historical.get("current_price")) != _decimal(pair[0]["close"])
                or _decimal(historical.get("previous_price")) != _decimal(pair[1]["close"])
                or any(_decimal(historical.get(field)) is None for field in RETURN_FIELDS)):
            continue
        session_date = return_session_date(bucket, current_time)
        if session_date is None:
            continue
        current_status = "confirmed" if close_date == session_date else "pending"
        added[_key(row)] = {
            "as_of": str(close_date), "status": "confirmed", "label": f"{close_date:%m/%d}收益",
            "status_label": f"{close_date:%m/%d}已确认", "source": "yahoo_close",
            "market": bucket, "calendar": calendar.name,
            "current_as_of": str(session_date), "current_status": current_status,
            "current_label": (f"{MARKET_LABELS[bucket]} {session_date:%m/%d}预估待更新"
                              if current_status == "pending" else f"{MARKET_LABELS[bucket]}最近有效交易日已确认"),
            "fields": {field: historical[field] for field in RETURN_FIELDS},
        }
    if not added:
        return output
    result = dict(output)
    result["rows"] = [dict(r, recent_confirmed_return=added[_key(r)]) if _key(r) in added else r for r in rows]
    accounts: dict[str, list[dict]] = defaultdict(list)
    for row in result["rows"]:
        if row.get("recent_confirmed_return"):
            accounts[_key(row)[0]].append(row["recent_confirmed_return"])
    result["recent_confirmed_returns"] = {"by_account": {}}
    for account, values in accounts.items():
        # Different markets can have different latest business dates. The
        # existing single-date card must not silently sum across those dates.
        day = max(v["as_of"] for v in values)
        same_day = [v for v in values if v["as_of"] == day]
        positions = [r for r in rows if _key(r)[0] == account and not r.get("is_closed")
                     and (_decimal(r.get("quantity")) or Decimal(0)) > 0
                     and not str(r.get("symbol") or "").startswith("CASH_")]
        result["recent_confirmed_returns"]["by_account"][account] = {
            "as_of": day, "amount_cny": str(sum((Decimal(v["fields"]["daily_total_pnl_cny"]) for v in same_day), Decimal(0))),
            "label": f"{date.fromisoformat(day):%m/%d}收益", "partial": len(same_day) < len(positions),
            "covered": len(same_day), "expected": len(positions),
            "current_as_of": str(active_date), "current_status": "pending",
            "title": f"{day} 各市场已确认收益；仅汇总同一日期，未计入今日汇总",
        }
    return result
