"""Secondary-market performance benchmark comparison and open-holding PnL leaderboard."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from domain.investments.valuation.page_projection import read_page
from domain.market.benchmark_history import (
    benchmark_history,
    eastmoney_index_history,
    local_benchmark,
    overseas_history,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_ACCOUNT_LABELS = {
    "tonghuashun": "同花顺",
    "ibkr": "盈透证券",
    "tencent_licaitong": "腾讯理财通",
    "ccb_yhs300_fund": "建行理财",
    "ccb_gold_accumulation": "建行黄金",
    "huawei_stock": "华为 ESOP",
    "wechat_lqt": "微信零钱通",
    "tencent_licaitong_current_plus": "理财通活期+",
}


def _shanghai_today() -> date:
    return datetime.now(_SHANGHAI).date()


def _clean_display_name(name: str, symbol: str) -> str:
    raw = str(name or "").strip()
    sym = str(symbol or "").strip()
    if sym and raw.startswith(f"{sym} - "):
        return raw[len(sym) + 3:].strip() or raw
    return raw or sym or "未命名"


def _account_display(row: dict[str, Any]) -> str:
    if row.get("account_name"):
        return str(row["account_name"]).strip()
    code = str(row.get("account_code") or row.get("account") or "").strip()
    return _ACCOUNT_LABELS.get(code, code)


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


def _normalize_return_series(rows: list[dict[str, Any]], start_iso: str) -> list[dict[str, Any]]:
    valid: list[tuple[str, float]] = []
    for row in rows or []:
        d = str(row.get("date") or "")
        c = _f(row.get("close"))
        if d >= start_iso and c is not None and c > 0:
            valid.append((d, c))
    if not valid:
        return []
    valid.sort(key=lambda item: item[0])
    first_close = valid[0][1]
    return [
        {"date": d, "return_pct": round((c / first_close - 1.0) * 100.0, 2)}
        for d, c in valid
    ]


def _load_benchmark_item(
    cur,
    *,
    key: str,
    name: str,
    symbol: str,
    provider_symbol: str,
    today: date,
    is_overseas: bool = False,
    eastmoney_secid: str | None = None,
) -> dict[str, Any]:
    ytd_start = date(today.year, 1, 1)
    m1_start = today - timedelta(days=31)
    rows: list[dict[str, Any]] = []
    try:
        local = local_benchmark(cur, ytd_start, today, symbol)
    except Exception:
        local = {}
    closes: dict[str, Any] = dict(local)
    if eastmoney_secid:
        try:
            for day_str, price in eastmoney_index_history(eastmoney_secid, ytd_start, today, cache_day=today):
                closes.setdefault(day_str, price)
        except Exception:
            pass
    if is_overseas:
        if len(closes) < 25:
            try:
                for day, price in overseas_history(provider_symbol, ytd_start, today):
                    closes.setdefault(day.isoformat() if hasattr(day, "isoformat") else str(day), price)
            except Exception:
                pass
        rows = [{"date": d, "close": str(closes[d])} for d in sorted(closes)]
    else:
        if len(closes) < 25:
            try:
                payload = benchmark_history(closes, ytd_start, today, today, symbol=provider_symbol, name=name)
                rows = payload.get("rows") or []
            except Exception:
                rows = [{"date": d, "close": str(closes[d])} for d in sorted(closes)]
        else:
            rows = [{"date": d, "close": str(closes[d])} for d in sorted(closes)]

    ytd_pct, as_of = _series_return(rows, ytd_start.isoformat())
    m1_pct, m1_as_of = _series_return(rows, m1_start.isoformat())
    series = _normalize_return_series(rows, ytd_start.isoformat())
    return {
        "key": key,
        "name": name,
        "symbol": symbol,
        "ytd_return_pct": ytd_pct,
        "m1_return_pct": m1_pct,
        "as_of": as_of or m1_as_of,
        "status": "ok" if (ytd_pct is not None or m1_pct is not None) else "unavailable",
        "series": series,
    }


def summarize_open_holdings_leaderboard(
    holdings: list[dict[str, Any]],
    *,
    closed_items: list[dict[str, Any]] | None = None,
    secondary_capital_summary: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Compute ex-ESOP total PnL (open + closed + distributions) and Top 3 winners / losers."""
    ex_esop_pnl = 0.0
    ex_esop_cost = 0.0
    ex_esop_mv = 0.0
    esop_pnl = 0.0
    items: list[dict[str, Any]] = []
    esop_items: list[dict[str, Any]] = []
    open_keys: set[tuple[str, str]] = set()

    for row in holdings or []:
        if not isinstance(row, dict):
            continue
        if row.get("is_closed") or row.get("closed") or row.get("reference_as_of"):
            continue
        ccy = str(row.get("currency") or "CNY").strip().upper()
        mv = _f(row.get("market_value_cny"))
        if mv is None and ccy == "CNY":
            mv = _f(row.get("market_value"))
        if mv is None or mv <= 1.0:
            continue
        cat = f"{row.get('major_category') or ''} {row.get('minor_category') or ''} {row.get('asset_type') or ''}"
        sym = str(row.get("symbol") or "").strip()
        name = _clean_display_name(str(row.get("name") or row.get("asset_name") or sym or "未命名"), sym)
        if any(tok in cat for tok in ("现金", "负债", "借款", "货币", "银行")) or sym.upper().startswith("CASH") or "LOAN" in sym.upper():
            continue
        cost = _f(row.get("cost_cny"))
        if cost is None and ccy == "CNY":
            cost = _f(row.get("cost"))
        # Use total holding PnL (unrealized + realized trades + distributions) for true profit/loss view.
        pnl = _f(row.get("holding_pnl_cny") if row.get("holding_pnl_cny") is not None else row.get("pnl_cny"))
        if pnl is None and ccy == "CNY":
            pnl = _f(row.get("holding_pnl") if row.get("holding_pnl") is not None else row.get("pnl"))
        if pnl is None:
            pnl = _f(row.get("floating_pnl_cny"))
        if pnl is None and cost is not None:
            pnl = mv - cost
        is_esop = "esop" in sym.lower() or "内部股份" in name or "非上市" in cat or "esop" in cat.lower()
        if is_esop:
            if pnl is not None:
                esop_pnl += pnl
                eff_cost = cost if (cost is not None and cost > 0) else (mv - pnl if (mv - pnl) > 0 else None)
                pnl_pct = round((pnl / eff_cost) * 100.0, 2) if (eff_cost is not None and eff_cost > 0) else None
                esop_items.append({
                    "symbol": sym or name,
                    "name": name,
                    "account": _account_display(row),
                    "market_value_cny": round(mv, 2),
                    "pnl_cny": round(pnl, 2),
                    "pnl_pct": pnl_pct,
                    "is_closed": False,
                    "is_esop": True,
                })
            continue
        acct_code = str(row.get("account_code") or row.get("account") or "").strip()
        if acct_code and sym:
            open_keys.add((acct_code, sym.upper()))
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
            "account": _account_display(row),
            "market_value_cny": round(mv, 2),
            "pnl_cny": round(pnl, 2),
            "pnl_pct": pnl_pct,
            "is_closed": False,
        })

    # Include fully closed positions (skipping symbols still open to avoid double-counting realized PnL).
    for closed in closed_items or []:
        if not isinstance(closed, dict):
            continue
        acct_code = str(closed.get("account_code") or closed.get("account") or "").strip()
        sym = str(closed.get("symbol") or "").strip()
        if acct_code and sym and (acct_code, sym.upper()) in open_keys:
            continue
        cycles = closed.get("cycles") or []
        pnl = sum(
            (_f(c.get("trade_pnl")) or 0.0) + (_f(c.get("distributions")) or 0.0)
            for c in cycles if isinstance(c, dict)
        )
        cost = sum((_f(c.get("cost")) or 0.0) for c in cycles if isinstance(c, dict))
        if abs(pnl) < 0.01:
            continue
        name = _clean_display_name(str(closed.get("name") or sym or "未命名"), sym)
        ex_esop_pnl += pnl
        if cost > 0:
            ex_esop_cost += cost
        pnl_pct = round((pnl / cost) * 100.0, 2) if cost > 0 else None
        items.append({
            "symbol": sym or name,
            "name": name,
            "account": _account_display(closed),
            "market_value_cny": 0.0,
            "pnl_cny": round(pnl, 2),
            "pnl_pct": pnl_pct,
            "is_closed": True,
        })

    winners = sorted([x for x in items if x["pnl_cny"] > 0], key=lambda x: x["pnl_cny"], reverse=True)[:3]
    losers = sorted([x for x in items if x["pnl_cny"] < 0], key=lambda x: x["pnl_cny"])[:3]
    all_heatmap_candidates = [*items, *esop_items]
    heatmap_items = sorted(
        [x for x in all_heatmap_candidates if abs(x["pnl_cny"]) >= 0.01],
        key=lambda x: abs(x["pnl_cny"]),
        reverse=True,
    )

    if (
        isinstance(secondary_capital_summary, dict)
        and secondary_capital_summary.get("capital_cny", 0.0) > 0
    ):
        final_pnl = secondary_capital_summary["pnl_cny"]
        final_cost = secondary_capital_summary["capital_cny"]
        final_return_pct = round((final_pnl / final_cost) * 100.0, 2)
        method_note = "二级市场（剔除 ESOP）总盈亏（含已清仓、分红与融资利息）/ 自有投入本金。"
    else:
        final_pnl = ex_esop_pnl
        final_cost = ex_esop_cost
        final_return_pct = round((ex_esop_pnl / ex_esop_cost) * 100.0, 2) if ex_esop_cost > 0 else None
        method_note = "二级市场（剔除 ESOP）在仓与已清仓标的累计总盈亏 / 投入成本。"

    return {
        "portfolio": {
            "ex_esop_market_value_cny": round(ex_esop_mv, 2),
            "ex_esop_cost_cny": round(final_cost, 2),
            "ex_esop_pnl_cny": round(final_pnl, 2),
            "ex_esop_return_pct": final_return_pct,
            "esop_pnl_cny": round(esop_pnl, 2),
            "method_note": method_note,
        },
        "leaderboard": {
            "winners": winners,
            "losers": losers,
        },
        "heatmap_items": heatmap_items,
    }


def _secondary_market_capital_summary(cur, *, ref_day: date) -> dict[str, float] | None:
    """Aggregate ex-ESOP capital-return PnL and capital from the published analysis projection."""
    try:
        projected = read_page(cur, "analysis", business_date=ref_day, member="sections")
        sections = (projected or {}).get("data")
        if not isinstance(sections, dict):
            return None
        total_pnl = 0.0
        total_cap = 0.0
        found = 0
        for key in ("stocks", "a_fund", "precious_metal", "wealth"):
            sec = sections.get(key)
            if not isinstance(sec, dict) or sec.get("return_basis") != "capital_return":
                continue
            rows = sec.get("rows") or []
            if not rows:
                continue
            last = rows[-1]
            pnl = _f(last.get("total_pnl"))
            cap = _f(last.get("cumulative_capital"))
            mv = _f(last.get("market_value"))
            if pnl is not None:
                total_pnl += pnl
                found += 1
            if cap is not None and cap > 0 and (mv is None or mv > 0):
                total_cap += cap
        if found > 0 and total_cap > 0:
            return {"pnl_cny": total_pnl, "capital_cny": total_cap}
    except Exception:
        return None
    return None


def _load_closed_leaderboard_items(cur, *, ref_day: date) -> list[dict[str, Any]]:
    try:
        from domain.accounts.views.account_policy import owner_ratio_lookup
        from domain.investments.returns.period_trade_returns import load_closed_trade_returns
        from domain.investments.valuation.portfolio_state import _excluded_accounts, _load_accounts_config

        cfg = _load_accounts_config()
        by_section = load_closed_trade_returns(
            cur,
            start=date(ref_day.year, 1, 1),
            end=ref_day,
            base_currency="CNY",
            owner_ratio=owner_ratio_lookup(cfg),
            sections=None,
            excluded_accounts=_excluded_accounts(cfg),
        )
        out: list[dict[str, Any]] = []
        for payload in (by_section or {}).values():
            if isinstance(payload, dict) and isinstance(payload.get("items"), list):
                out.extend(payload["items"])
        return out
    except Exception:
        return []


def _portfolio_total_return_series(cur, *, ref_day: date) -> dict[str, Any]:
    """Read YTD daily total portfolio return curve from the published analysis projection."""
    ytd_start = date(ref_day.year, 1, 1).isoformat()
    series: list[dict[str, Any]] = []
    latest_total_pnl: float | None = None
    try:
        projected = read_page(cur, "analysis", business_date=ref_day, member="total")
        total = (projected or {}).get("data")
        rows = (total or {}).get("rows") if isinstance(total, dict) else None
        first_pnl: float | None = None
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            d = str(row.get("date") or "")
            rate = _f(row.get("capital_return_rate"))
            if rate is None:
                rate = _f(row.get("return_rate"))
            pnl = _f(row.get("total_pnl"))
            if d >= ytd_start and rate is not None:
                if first_pnl is None and pnl is not None:
                    first_pnl = pnl
                if pnl is not None:
                    latest_total_pnl = round(pnl - (first_pnl or 0.0), 2)
                series.append({"date": d, "return_pct": round(rate * 100.0, 2)})
    except Exception:
        series = []
    series.sort(key=lambda item: item["date"])
    last = series[-1] if series else None
    return {
        "key": "total",
        "name": "我的总收益率",
        "ytd_return_pct": last["return_pct"] if last else None,
        "ytd_pnl_cny": latest_total_pnl,
        "as_of": last["date"] if last else None,
        "series": series,
    }


def build_performance_summary_payload(cur, *, today: date | None = None) -> dict[str, Any]:
    ref_day = today or _shanghai_today()
    holdings: list[dict[str, Any]] = []
    try:
        projected = read_page(cur, "holdings", business_date=ref_day, member="holdings")
        data = (projected or {}).get("data")
        if isinstance(data, dict) and isinstance(data.get("rows"), list):
            holdings = data["rows"]
        elif isinstance(data, list):
            holdings = data
    except Exception:
        holdings = []

    closed_items = _load_closed_leaderboard_items(cur, ref_day=ref_day)
    secondary_capital = _secondary_market_capital_summary(cur, ref_day=ref_day)
    summary = summarize_open_holdings_leaderboard(
        holdings,
        closed_items=closed_items,
        secondary_capital_summary=secondary_capital,
    )
    benchmarks = [
        _load_benchmark_item(
            cur,
            key="ssec",
            name="上证指数",
            symbol="000001.SH",
            provider_symbol="sh000001",
            eastmoney_secid="1.000001",
            today=ref_day,
            is_overseas=False,
        ),
        _load_benchmark_item(
            cur,
            key="kospi",
            name="KOSPI",
            symbol="^KS11",
            provider_symbol="^KS11",
            eastmoney_secid="100.KS11",
            today=ref_day,
            is_overseas=True,
        ),
        _load_benchmark_item(
            cur,
            key="sp500",
            name="标普500",
            symbol="^GSPC",
            provider_symbol="^GSPC",
            eastmoney_secid="100.SPX",
            today=ref_day,
            is_overseas=True,
        ),
    ]
    portfolio_curve = _portfolio_total_return_series(cur, ref_day=ref_day)
    if portfolio_curve.get("ytd_pnl_cny") is not None:
        summary["portfolio"]["total_pnl_cny"] = portfolio_curve["ytd_pnl_cny"]
    if portfolio_curve.get("ytd_return_pct") is not None:
        summary["portfolio"]["total_return_pct"] = portfolio_curve["ytd_return_pct"]
    return {
        "as_of": ref_day.isoformat(),
        **summary,
        "benchmarks": benchmarks,
        "benchmark_comparison": {
            "portfolio": portfolio_curve,
            "benchmarks": benchmarks,
        },
    }
