"""Dated, read-only US close returns; never replace the active daily bucket."""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Callable


RETURN_FIELDS = (
    "daily_pnl", "daily_pnl_native", "daily_pnl_cny", "daily_fx_pnl_cny",
    "daily_total_pnl_cny", "price_change_pct", "price_change_amount", "previous_price",
)


def _decimal(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _key(row: dict) -> tuple[str, str]:
    return (str(row.get("account") or row.get("account_code") or ""), str(row.get("symbol") or ""))


def _us_position(row: dict) -> bool:
    return (row.get("major_category") == "美股" and row.get("currency") == "USD"
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
    cur.execute("SELECT to_regclass('public.pams_market_quote_snapshot') IS NOT NULL AS exists")
    if not (cur.fetchone() or {}).get("exists"):
        return payload
    cur.execute("""
        SELECT MAX(quote_date) AS as_of FROM pams_market_quote_snapshot
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
    historical = {_key(r): r for r in load_date(close_date).get("rows", []) if _us_position(r)}
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
