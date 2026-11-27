"""Roll forward Tonghuashun post-snapshot verified trades and merge cash-management balances."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from domain.investments.trades.trade_read_v2 import load_trade_rows

_SUPPORTED_TRADE_TYPES = frozenset({"buy", "sell", "dividend", "tax", "deposit", "withdraw", "interest"})
_ZERO = Decimal("0")


def _to_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _to_dec(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _table_exists(cur, name: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL AS exists", (f"public.{name}",))
    row = cur.fetchone()
    return bool(row["exists"] if isinstance(row, dict) else (row[0] if row else False))


def project_ths_post_snapshot_rows(
    rows: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    *,
    view_as_of: date,
    account_code: str = "tonghuashun",
) -> list[dict[str, Any]]:
    """Apply verified post-snapshot CNY trades to Tonghuashun open holdings before owner scaling.

    Fail-closed: if any post-snapshot trade is unverified, non-CNY, unsupported, or references
    a buy/sell symbol not present in the snapshot rows, the original rows are returned unchanged
    so positions and cash never drift silently.
    """
    ths_indices = [
        idx for idx, r in enumerate(rows)
        if str(r.get("account") or r.get("account_code") or "") == account_code
        and str(r.get("asset_liability") or "资产") == "资产"
        and str(r.get("currency") or "").upper() == "CNY"
        and str(r.get("major_category") or "") != "现金"
        and not r.get("is_closed")
    ]
    if not ths_indices or not trades:
        return rows

    snap_dates = [_to_date(rows[i].get("as_of")) for i in ths_indices]
    snap_dates = [d for d in snap_dates if d is not None]
    if not snap_dates:
        return rows
    snap_day = max(snap_dates)
    cutoff = _to_date(view_as_of) or snap_day
    if cutoff <= snap_day:
        return rows

    post_trades = [
        t for t in trades
        if (td := _to_date(t.get("trade_date"))) is not None and snap_day < td <= cutoff
    ]
    if not post_trades:
        return rows

    by_symbol = {
        str(rows[i].get("symbol") or "").strip(): i
        for i in ths_indices
        if str(rows[i].get("symbol") or "").strip()
    }

    for t in post_trades:
        t_type = str(t.get("trade_type") or "").lower()
        if (t.get("source_evidence_status") != "verified"
                or str(t.get("currency") or "").upper() != "CNY"
                or t_type not in _SUPPORTED_TRADE_TYPES):
            return rows
        if t_type in {"buy", "sell"}:
            sym = str(t.get("symbol") or "").strip()
            qty = _to_dec(t.get("quantity"))
            net_cash = _to_dec(t.get("net_cash"))
            if not sym or sym not in by_symbol or qty is None or qty <= 0 or net_cash is None:
                return rows

    out = [dict(r) for r in rows]
    touched: dict[int, date] = {}
    same_day_buys: dict[int, tuple[Decimal, Decimal]] = {}

    ordered = sorted(
        post_trades,
        key=lambda t: (_to_date(t.get("trade_date")) or date.min, int(t.get("trade_id") or 0)),
    )
    for t in ordered:
        t_type = str(t.get("trade_type") or "").lower()
        sym = str(t.get("symbol") or "").strip()
        if sym not in by_symbol:
            continue
        idx = by_symbol[sym]
        row = out[idx]
        t_day = _to_date(t.get("trade_date"))
        if t_day is None:
            continue

        if t_type == "buy":
            old_qty = _to_dec(row.get("quantity"))
            delta_qty = _to_dec(t.get("quantity"))
            net_cash = _to_dec(t.get("net_cash"))
            if old_qty is None or delta_qty is None or net_cash is None:
                return rows
            trade_cost = abs(net_cash)
            new_qty = old_qty + delta_qty
            row["quantity"] = new_qty
            old_cost = _to_dec(row.get("cost"))
            if old_cost is not None:
                new_cost = old_cost + trade_cost
                row["cost"] = new_cost
                row["cost_cny"] = new_cost
                row["buy_price"] = new_cost / new_qty if new_qty > 0 else None
            price_day = _to_date(row.get("snapshot_as_of")) or _to_date(row.get("as_of"))
            if price_day and t_day == price_day:
                gross = _to_dec(t.get("gross_amount"))
                buy_val = abs(gross) if gross is not None else trade_cost
                prev_q, prev_v = same_day_buys.get(idx, (_ZERO, _ZERO))
                same_day_buys[idx] = (prev_q + delta_qty, prev_v + buy_val)
            touched[idx] = max(touched.get(idx, t_day), t_day)

        elif t_type == "sell":
            old_qty = _to_dec(row.get("quantity"))
            delta_qty = _to_dec(t.get("quantity"))
            net_cash = _to_dec(t.get("net_cash"))
            if old_qty is None or delta_qty is None or net_cash is None or delta_qty > old_qty:
                return rows
            new_qty = old_qty - delta_qty
            old_cost = _to_dec(row.get("cost"))
            if old_cost is not None and old_qty > 0:
                sold_cost = old_cost * (delta_qty / old_qty)
                new_cost = old_cost - sold_cost
                row["cost"] = new_cost
                row["cost_cny"] = new_cost
                row["buy_price"] = (new_cost / new_qty) if new_qty > 0 else None
                realized = _to_dec(row.get("realized_trade_pnl"))
                if realized is not None:
                    updated_realized = realized + (net_cash - sold_cost)
                    row["realized_trade_pnl"] = updated_realized
                    row["realized_trade_pnl_native"] = updated_realized
                    row["realized_trade_pnl_cny"] = updated_realized
            row["quantity"] = new_qty
            touched[idx] = max(touched.get(idx, t_day), t_day)

        elif t_type in {"dividend", "tax"}:
            net_cash = _to_dec(t.get("net_cash"))
            if net_cash is not None and row.get("net_distributions") is not None:
                dist = _to_dec(row.get("net_distributions"))
                if dist is not None:
                    updated_dist = dist + net_cash
                    row["net_distributions"] = updated_dist
                    row["net_distributions_cny"] = updated_dist
                    touched[idx] = max(touched.get(idx, t_day), t_day)

    drop_indices: set[int] = set()
    for idx, latest_day in touched.items():
        row = out[idx]
        qty = _to_dec(row.get("quantity")) or _ZERO
        if qty <= Decimal("0.0001"):
            drop_indices.add(idx)
            continue
        cur_px = _to_dec(row.get("current_price"))
        prev_px = _to_dec(row.get("previous_price"))
        if cur_px is not None:
            mv = qty * cur_px
            row["market_value"] = mv
            row["market_value_native"] = mv
            row["market_value_cny"] = mv
        mv = _to_dec(row.get("market_value"))
        cost = _to_dec(row.get("cost"))
        if mv is not None and cost is not None:
            float_pnl = mv - cost
            realized = _to_dec(row.get("realized_trade_pnl"))
            dist = _to_dec(row.get("net_distributions"))
            if (row.get("diluted_cost_source") == "verified_trade_snapshot"
                    and realized is not None and dist is not None):
                total_pnl = float_pnl + realized + dist
                diluted = cost - realized - dist
                row["diluted_cost"] = diluted
                row["diluted_cost_cny"] = diluted
            else:
                total_pnl = float_pnl
            row["holding_pnl"] = total_pnl
            row["holding_pnl_native"] = total_pnl
            row["holding_pnl_cny"] = total_pnl
            if "holding_pnl_before_realized_trade" in row:
                row["holding_pnl_before_realized_trade"] = float_pnl
                row["holding_pnl_before_realized_trade_native"] = float_pnl
                row["holding_pnl_before_realized_trade_cny"] = float_pnl
        if cur_px is not None and prev_px is not None:
            today_q, today_v = same_day_buys.get(idx, (_ZERO, _ZERO))
            existing_q = max(qty - today_q, _ZERO)
            daily = (cur_px - prev_px) * existing_q + (cur_px * today_q - today_v)
            row["daily_pnl"] = daily
            row["daily_pnl_native"] = daily
            row["daily_pnl_cny"] = daily
            if "daily_total_pnl_cny" in row:
                row["daily_total_pnl_cny"] = daily
            if "daily_total_pnl" in row:
                row["daily_total_pnl"] = daily
        row["projected_from_trades"] = True
        row["projected_trade_as_of"] = latest_day.isoformat()

    if drop_indices:
        return [r for i, r in enumerate(out) if i not in drop_indices]
    return out


def apply_ths_post_snapshot_trades(
    cur,
    rows: list[dict[str, Any]],
    *,
    view_as_of: date,
    account_code: str = "tonghuashun",
) -> list[dict[str, Any]]:
    """Load verified post-snapshot Tonghuashun trades from DB and project holding rows."""
    ths_dates = [
        _to_date(r.get("as_of")) for r in rows
        if str(r.get("account") or r.get("account_code") or "") == account_code
        and str(r.get("asset_liability") or "资产") == "资产"
        and str(r.get("currency") or "").upper() == "CNY"
        and str(r.get("major_category") or "") != "现金"
        and not r.get("is_closed")
    ]
    ths_dates = [d for d in ths_dates if d is not None]
    if not ths_dates:
        return rows
    snap_day = max(ths_dates)
    cutoff = _to_date(view_as_of) or snap_day
    if cutoff <= snap_day or not _table_exists(cur, "source_records"):
        return rows
    trades = load_trade_rows(cur, account_code=account_code, start=snap_day + timedelta(days=1), end=cutoff)
    if not trades:
        return rows
    return project_ths_post_snapshot_rows(rows, trades, view_as_of=cutoff, account_code=account_code)


def _latest_cn_a_price(cur, symbol: str, as_of: date) -> Decimal | None:
    prefixed = ("sh" if symbol.startswith("6") else "sz") + symbol
    if _table_exists(cur, "market_observations"):
        cur.execute(
            """SELECT price FROM market_observations
                WHERE observation_type = 'quote' AND market = 'CN_A'
                  AND symbol IN (%s, %s) AND quote_date <= %s
                  AND price > 0 AND price::text NOT IN ('NaN', 'Infinity', '-Infinity')
                ORDER BY quote_date DESC, updated_at DESC LIMIT 1""",
            (symbol, prefixed, as_of),
        )
        row = cur.fetchone()
        if row and row.get("price") is not None:
            return _to_dec(row["price"])
    if _table_exists(cur, "pams_ohlcv_daily"):
        cur.execute(
            """SELECT close FROM pams_ohlcv_daily
                WHERE market = 'CN_A' AND symbol = %s AND trade_date <= %s
                  AND close > 0 AND close::text NOT IN ('NaN', 'Infinity', '-Infinity')
                ORDER BY trade_date DESC LIMIT 1""",
            (prefixed, as_of),
        )
        row = cur.fetchone()
        if row and row.get("close") is not None:
            return _to_dec(row["close"])
    return None


def ths_post_snapshot_position_delta(
    cur,
    *,
    snap_day: date,
    as_of: date,
    account_code: str = "tonghuashun",
) -> Decimal | None:
    """Return whole-account CNY market value delta from verified post-snapshot buy/sell trades.

    Returns Decimal(0) if there are no buy/sell trades after snap_day, or None (fail-closed)
    if any post-snapshot trade is unverified or lacks a market price.
    """
    if as_of <= snap_day or not _table_exists(cur, "source_records"):
        return _ZERO
    trades = load_trade_rows(cur, account_code=account_code, start=snap_day + timedelta(days=1), end=as_of)
    if not trades:
        return _ZERO
    qty_deltas: dict[str, Decimal] = {}
    for t in trades:
        t_type = str(t.get("trade_type") or "").lower()
        if (t.get("source_evidence_status") != "verified"
                or str(t.get("currency") or "").upper() != "CNY"
                or t_type not in _SUPPORTED_TRADE_TYPES):
            return None
        if t_type in {"buy", "sell"}:
            sym = str(t.get("symbol") or "").strip()
            qty = _to_dec(t.get("quantity"))
            if not sym or qty is None or qty <= 0:
                return None
            signed = qty if t_type == "buy" else -qty
            qty_deltas[sym] = qty_deltas.get(sym, _ZERO) + signed
    total_delta = _ZERO
    for sym, delta_qty in qty_deltas.items():
        if delta_qty == 0:
            continue
        px = _latest_cn_a_price(cur, sym, as_of)
        if px is None:
            return None
        total_delta += delta_qty * px
    return total_delta


def merge_cash_management_holding_rows(
    holdings: dict[str, Any],
    *,
    account_config: dict[str, Any],
    account_balances: dict[str, Any] | None,
    excluded_accounts: set[str],
    owner_ratio,
) -> dict[str, Any]:
    """Ensure active cash_management accounts appear in holdings rows with their latest balance."""
    accounts = (account_config or {}).get("accounts") or {}
    balances = account_balances or {}
    rows = [dict(r) for r in (holdings.get("rows") or [])]

    for code, cfg in accounts.items():
        if not isinstance(cfg, dict) or code in excluded_accounts:
            continue
        if str(cfg.get("account_type") or "").lower() != "cash_management":
            continue
        if cfg.get("active") is False or cfg.get("include_in_nav") is False:
            continue
        display_name = cfg.get("display_name") or code
        bal = balances.get(code) if isinstance(balances, dict) else None
        amount = _to_dec(bal.get("balance_amount")) if isinstance(bal, dict) else None
        personal = _to_dec(bal.get("personal_contribution")) if isinstance(bal, dict) else None
        currency = str((bal or {}).get("balance_currency") or cfg.get("base_currency") or "CNY").upper()
        as_of = (bal or {}).get("balance_as_of")
        existing_idx = next(
            (i for i, r in enumerate(rows)
             if str(r.get("account") or r.get("account_code") or "") == code
             and str(r.get("major_category") or "") in {"现金", "现金/活期"}),
            None,
        )
        if amount is None:
            if existing_idx is not None:
                rows[existing_idx]["name"] = display_name
                rows[existing_idx]["account_name"] = display_name
                rows[existing_idx]["is_cash_management"] = True
                if rows[existing_idx].get("as_of") and not rows[existing_idx].get("balance_as_of"):
                    rows[existing_idx]["balance_as_of"] = str(rows[existing_idx]["as_of"])[:10]
            continue
        if abs(amount) <= Decimal("0.005"):
            if existing_idx is not None:
                rows.pop(existing_idx)
            continue
        ratio = _to_dec(owner_ratio(code)) if callable(owner_ratio) else Decimal("1")
        if ratio is None:
            ratio = Decimal("1")
        if personal is None and currency == "CNY":
            personal = amount * ratio
        cny_val = personal if personal is not None else (amount * ratio if currency == "CNY" else None)
        as_of_str = str(as_of)[:10] if as_of else None
        cash_row = {
            "account": code,
            "account_code": code,
            "account_name": display_name,
            "symbol": f"CASH_{currency}",
            "name": display_name,
            "asset_liability": "资产",
            "major_category": "现金",
            "minor_category": "现金/活期",
            "type": "现金/活期",
            "currency": currency,
            "quantity": str(amount),
            "market_value": str(amount),
            "market_value_native": str(amount),
            "market_value_cny": str(cny_val) if cny_val is not None else None,
            "cost": str(amount),
            "cost_cny": str(cny_val) if cny_val is not None else None,
            "holding_pnl": "0",
            "holding_pnl_native": "0",
            "holding_pnl_cny": "0",
            "daily_pnl": "0",
            "daily_pnl_native": "0",
            "daily_pnl_cny": "0",
            "daily_total_pnl_cny": "0",
            "as_of": as_of_str,
            "balance_as_of": as_of_str,
            "owner_ratio": str(ratio),
            "is_closed": False,
            "is_cash_management": True,
            "data_source": (bal or {}).get("balance_basis") or "cash_anchor",
        }
        if existing_idx is not None:
            rows[existing_idx] = {**rows[existing_idx], **cash_row}
        else:
            rows.append(cash_row)

    return {**holdings, "rows": rows}
