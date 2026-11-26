"""Holdings freshness guard (>2 trading days stale) and Tonghuashun post-snapshot share projection."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any


def business_days_between(start_day: date | None, end_day: date) -> int:
    """Count weekday trading sessions strictly after start_day up to end_day."""
    if start_day is None or end_day <= start_day:
        return 0
    count = 0
    cur = start_day + timedelta(days=1)
    while cur <= end_day:
        if cur.weekday() < 5:
            count += 1
        cur += timedelta(days=1)
    return count


def compute_ths_trade_projection(
    snapshot_rows: list[dict[str, Any]],
    trade_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project current Tonghuashun share counts from latest snapshot + subsequent verified buy/sell trades."""
    if not snapshot_rows or not trade_rows:
        return []
    snap_by_sym: dict[str, dict[str, Any]] = {}
    snap_by_name: dict[str, str] = {}
    for row in snapshot_rows:
        sym = str(row.get("symbol") or "").strip().upper()
        name = str(row.get("name") or "").strip()
        qty = float(row.get("quantity") or 0)
        as_of = row.get("as_of")
        if not sym and not name:
            continue
        key = sym or name
        snap_by_sym[key] = {
            "symbol": sym or name,
            "name": name or sym,
            "snapshot_qty": qty,
            "snapshot_as_of": as_of.isoformat() if hasattr(as_of, "isoformat") else str(as_of or ""),
        }
        if name:
            snap_by_name[name] = key

    deltas: dict[str, dict[str, Any]] = defaultdict(lambda: {"delta_qty": 0.0, "trade_count": 0, "latest_trade_date": "", "name": ""})
    for tr in trade_rows:
        side = str(tr.get("side") or "").strip().lower()
        if side not in {"buy", "sell"}:
            continue
        raw_qty = tr.get("quantity")
        if raw_qty is None:
            continue
        qty = abs(float(raw_qty))
        if qty <= 0:
            continue
        sym = str(tr.get("symbol") or "").strip().upper()
        name = str(tr.get("asset_name") or tr.get("name") or "").strip()
        key = sym if sym in snap_by_sym else snap_by_name.get(name, sym or name)
        tdate = tr.get("trade_date")
        tdate_iso = tdate.isoformat() if hasattr(tdate, "isoformat") else str(tdate or "")
        snap_date = snap_by_sym.get(key, {}).get("snapshot_as_of") or ""
        if snap_date and tdate_iso and tdate_iso <= snap_date:
            continue
        signed = qty if side == "buy" else -qty
        entry = deltas[key]
        entry["delta_qty"] += signed
        entry["trade_count"] += 1
        if tdate_iso > entry["latest_trade_date"]:
            entry["latest_trade_date"] = tdate_iso
        if name and not entry["name"]:
            entry["name"] = name

    adjustments: list[dict[str, Any]] = []
    for key, info in deltas.items():
        if abs(info["delta_qty"]) < 1e-6:
            continue
        snap = snap_by_sym.get(key) or {"symbol": key, "name": info["name"] or key, "snapshot_qty": 0.0, "snapshot_as_of": ""}
        projected = max(0.0, snap["snapshot_qty"] + info["delta_qty"])
        adjustments.append({
            "symbol": snap["symbol"],
            "name": snap["name"] or info["name"] or key,
            "snapshot_as_of": snap["snapshot_as_of"],
            "snapshot_qty": round(snap["snapshot_qty"], 4),
            "delta_qty": round(info["delta_qty"], 4),
            "projected_qty": round(projected, 4),
            "latest_trade_date": info["latest_trade_date"],
            "trade_count": info["trade_count"],
        })
    return sorted(adjustments, key=lambda x: (-abs(x["delta_qty"]), x["symbol"]))


def _parse_iso_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    text = str(value or "").strip()[:10]
    if len(text) == 10:
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None
    return None


def build_holdings_freshness_and_projection(cur, *, today: date | None = None) -> dict[str, Any]:
    from domain.investments.valuation.page_projection import read_page

    ref_day = today or date.today()
    account_cards: dict[str, Any] = {}
    try:
        projected = read_page(cur, "holdings", business_date=ref_day, member="account_cards")
        if isinstance((projected or {}).get("data"), dict):
            account_cards = projected["data"]
    except Exception:
        account_cards = {}

    # Check actual market quote / valuation freshness (not broker snapshot export date)
    a_stock_day: date | None = None
    try:
        cur.execute(
            "SELECT MAX(as_of) AS max_as_of FROM market_observations WHERE market = 'CN_A' AND observation_type IN ('quote', 'daily')"
        )
        row = cur.fetchone()
        a_stock_day = _parse_iso_date(row["max_as_of"] if isinstance(row, dict) else (row[0] if row else None))
    except Exception:
        a_stock_day = None

    def _card_day(code: str) -> date | None:
        card = account_cards.get(code)
        if not isinstance(card, dict):
            return None
        for field in ("recent_daily_pnl", "market_value", "net_value"):
            node = card.get(field)
            d = _parse_iso_date(node.get("as_of") if isinstance(node, dict) else None)
            if d is not None:
                return d
        return None

    def _snap_day(code: str) -> date | None:
        cur.execute("SELECT MAX(as_of) AS max_as_of FROM pams_holdings_snapshot WHERE account_code = %s", (code,))
        row = cur.fetchone()
        return _parse_iso_date(row["max_as_of"] if isinstance(row, dict) else (row[0] if row else None))

    sources = [
        ("ibkr", "盈透日结", _card_day("ibkr") or _snap_day("ibkr")),
        ("a_stock", "A股行情", a_stock_day or _card_day("tonghuashun") or _snap_day("tonghuashun")),
        ("tencent_licaitong", "基金净值", _card_day("tencent_licaitong") or _snap_day("tencent_licaitong")),
    ]
    stale_items: list[dict[str, Any]] = []
    source_dates: dict[str, str | None] = {}

    for code, label, max_day in sources:
        source_dates[code] = max_day.isoformat() if max_day else None
        lag = business_days_between(max_day, ref_day) if max_day else 0
        if max_day is not None and lag >= 2:
            stale_items.append({
                "account_code": code,
                "label": label,
                "as_of": source_dates[code],
                "trading_days_stale": lag,
            })

    # Tonghuashun latest snapshot vs subsequent verified trades
    ths_adjustments: list[dict[str, Any]] = []
    cur.execute(
        """
        WITH latest AS (
            SELECT MAX(as_of) AS max_as_of
            FROM pams_holdings_snapshot
            WHERE account_code = 'tonghuashun' AND asset_liability = '资产'
        )
        SELECT s.symbol, s.name, s.quantity, s.as_of
        FROM pams_holdings_snapshot s
        JOIN latest l ON s.as_of = l.max_as_of
        WHERE s.account_code = 'tonghuashun'
          AND s.asset_liability = '资产'
          AND COALESCE(s.quantity, 0) > 0
        """
    )
    snap_rows = [dict(r) for r in cur.fetchall()]
    if snap_rows:
        max_snap_date = max(r["as_of"] for r in snap_rows if r.get("as_of"))
        cur.execute(
            """
            SELECT symbol, name, side, quantity, trade_date
            FROM pams_investment_trade_fact
            WHERE account_code = 'tonghuashun'
              AND trade_date > %s
              AND lower(COALESCE(side, trade_type, '')) IN ('buy', 'sell')
              AND quantity IS NOT NULL
            ORDER BY trade_date ASC
            """,
            (max_snap_date,),
        )
        trade_rows = [dict(r) for r in cur.fetchall()]
        ths_adjustments = compute_ths_trade_projection(snap_rows, trade_rows)

    return {
        "as_of": ref_day.isoformat(),
        "has_stale": len(stale_items) > 0,
        "stale_items": stale_items,
        "source_dates": source_dates,
        "ths_projected_adjustments": ths_adjustments,
    }
