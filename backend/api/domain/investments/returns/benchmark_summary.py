"""Secondary-market performance benchmark comparison and open-holding PnL leaderboard."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from domain.investments.valuation.page_projection import read_page
from domain.market.benchmark_history import benchmark_history, local_benchmark, overseas_history


def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed else None


def _series_return(rows: list[dict[str, Any]], start_iso: str) -> tuple[float | None, str | None]:
    valid = []
    for row in rows or []:
        d = str(row.get("date") or "")
        c = _f(row.get("close"))
        if d >= start_iso and c is not None and c > 0:
            valid.append((d, c))
    if len(valid) < 2:
        return None, valid[-1][0] if valid else None
    valid.sort(key=lambda item: item[0])
    first_close = valid[0][1]
    last_date, last_close = valid[-1]
    return round((last_close / first_close - 1.0) * 100.0, 2), last_date


def _load_benchmark_item(cur, *, key: str, name: str, symbol: str, provider_symbol: str, today: date, is_overseas: bool = False) -> dict[str, Any]:
    ytd_start = date(today.year, 1, 1)
    m1_start = today - timedelta(days=31)
    rows: list[dict[str, Any]] = []
    try:
        local = local_benchmark(cur, ytd_start, today, symbol)
    except Exception:
        local = {}
    if is_overseas:
        closes = dict(local)
        if len(closes) < 25:
            try:
                for day, price in overseas_history(provider_symbol, ytd_start, today):
                    closes.setdefault(day.isoformat() if hasattr(day, "isoformat") else str(day), price)
            except Exception:
                pass
        rows = [{"date": d, "close": str(closes[d])} for d in sorted(closes)]
    else:
        try:
            payload = benchmark_history(local, ytd_start, today, today, symbol=provider_symbol, name=name)
            rows = payload.get("rows") or []
        except Exception:
            rows = [{"date": d, "close": str(local[d])} for d in sorted(local)]

    ytd_pct, as_of = _series_return(rows, ytd_start.isoformat())
    m1_pct, m1_as_of = _series_return(rows, m1_start.isoformat())
    return {
        "key": key,
        "name": name,
        "symbol": symbol,
        "ytd_return_pct": ytd_pct,
        "m1_return_pct": m1_pct,
        "as_of": as_of or m1_as_of,
        "status": "ok" if (ytd_pct is not None or m1_pct is not None) else "unavailable",
    }


def summarize_open_holdings_leaderboard(holdings: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute ex-ESOP simple holding return ratio and Top 3 winners / losers."""
    ex_esop_pnl = 0.0
    ex_esop_cost = 0.0
    ex_esop_mv = 0.0
    esop_pnl = 0.0
    items: list[dict[str, Any]] = []

    for row in holdings or []:
        if not isinstance(row, dict):
            continue
        if row.get("is_closed") or row.get("closed"):
            continue
        mv = _f(row.get("market_value_cny") if row.get("market_value_cny") is not None else row.get("market_value"))
        if mv is None or mv <= 1.0:
            continue
        cat = str(row.get("minor_category") or row.get("major_category") or row.get("asset_type") or "")
        sym = str(row.get("symbol") or "").strip()
        name = str(row.get("name") or row.get("asset_name") or sym or "未命名")
        if any(tok in cat for tok in ("现金", "负债", "货币", "银行")) or sym.upper().startswith("CASH"):
            continue
        pnl = _f(row.get("holding_pnl_cny") if row.get("holding_pnl_cny") is not None else row.get("pnl_cny"))
        if pnl is None:
            pnl = _f(row.get("holding_pnl") if row.get("holding_pnl") is not None else row.get("pnl"))
        cost = _f(row.get("cost_cny") if row.get("cost_cny") is not None else row.get("cost"))
        is_esop = "esop" in sym.lower() or "内部股份" in name or "非上市" in cat or "esop" in cat.lower()
        if is_esop:
            if pnl is not None:
                esop_pnl += pnl
            continue
        ex_esop_mv += mv
        if pnl is not None:
            ex_esop_pnl += pnl
        if cost is not None and cost > 0:
            ex_esop_cost += cost
        elif pnl is not None and (mv - pnl) > 0:
            ex_esop_cost += (mv - pnl)
        if pnl is None:
            continue
        eff_cost = cost if (cost is not None and cost > 0) else (mv - pnl if (mv - pnl) > 0 else None)
        pnl_pct = round((pnl / eff_cost) * 100.0, 2) if (eff_cost is not None and eff_cost > 0) else None
        items.append({
            "symbol": sym or name,
            "name": name,
            "account": str(row.get("account_name") or row.get("account_code") or ""),
            "market_value_cny": round(mv, 2),
            "pnl_cny": round(pnl, 2),
            "pnl_pct": pnl_pct,
        })

    winners = sorted([x for x in items if x["pnl_cny"] > 0], key=lambda x: x["pnl_cny"], reverse=True)[:3]
    losers = sorted([x for x in items if x["pnl_cny"] < 0], key=lambda x: x["pnl_cny"])[:3]
    ex_esop_return_pct = round((ex_esop_pnl / ex_esop_cost) * 100.0, 2) if ex_esop_cost > 0 else None

    return {
        "portfolio": {
            "ex_esop_market_value_cny": round(ex_esop_mv, 2),
            "ex_esop_cost_cny": round(ex_esop_cost, 2),
            "ex_esop_pnl_cny": round(ex_esop_pnl, 2),
            "ex_esop_return_pct": ex_esop_return_pct,
            "esop_pnl_cny": round(esop_pnl, 2),
            "method_note": "二级市场（剔除 ESOP）在仓标的总浮盈亏 / 剩余成本（简单持仓回报比，非 TWR/MWR）。",
        },
        "leaderboard": {
            "winners": winners,
            "losers": losers,
        },
    }


def build_performance_summary_payload(cur, *, today: date | None = None) -> dict[str, Any]:
    ref_day = today or date.today()
    holdings: list[dict[str, Any]] = []
    try:
        projected = read_page(cur, "holdings", business_date=ref_day, member="holdings")
        data = (projected or {}).get("data")
        if isinstance(data, list):
            holdings = data
    except Exception:
        holdings = []

    summary = summarize_open_holdings_leaderboard(holdings)
    benchmarks = [
        _load_benchmark_item(cur, key="hs300", name="沪深300", symbol="000300.SH", provider_symbol="sh000300", today=ref_day, is_overseas=False),
        _load_benchmark_item(cur, key="qqq", name="纳指100 (QQQ)", symbol="QQQ", provider_symbol="QQQ", today=ref_day, is_overseas=True),
    ]
    return {
        "as_of": ref_day.isoformat(),
        **summary,
        "benchmarks": benchmarks,
    }
