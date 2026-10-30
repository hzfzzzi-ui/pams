"""Opt-in compatibility filters for legacy transactions; unknown FX is rejected."""
from decimal import Decimal
from typing import Any, Dict, List, Tuple

import psycopg2.extras
from fastapi import HTTPException


def _norm(v):
    if hasattr(v, "isoformat"):
        return v.isoformat()
    from decimal import Decimal
    if isinstance(v, Decimal):
        return str(v)
    return v


INTERNAL_TRANSFER_CATEGORY = "\u5185\u90e8\u5212\u8f6c"


INTERNAL_TRANSFER_SUBCATEGORY = "账户间划转"


DEFAULT_LARGE_THRESHOLD_CNY = 5000.0


ACCOUNT_CODE_ALIASES = {
    "santander": "santander",
    "Santander": "santander",
    "ibkr": "ibkr",
    "IBKR": "ibkr",
    "binance": "binance",
    "Binance": "binance",
    "jianhang": "ccb_cny",
    "ccb": "ccb_cny",
    "tencent_licaitong": "tencent_licaitong",
    "huawei": "huawei",
    "gongjijin": "gongjijin",
    "guangfa": "guangfa",
    "tonghuashun": "tonghuashun",
    "jiekuan": "jiekuan",
    "wechat": "wechat",
}


def normalize_account_code(account_code: str) -> str:
    code = (account_code or "").strip()
    if not code:
        return code
    return ACCOUNT_CODE_ALIASES.get(code, ACCOUNT_CODE_ALIASES.get(code.lower(), code))


def month_range(month: str) -> Tuple[str, str]:
    try:
        year, mon = month.split("-")
        year_i = int(year)
        mon_i = int(mon)
        if year_i < 1 or year_i > 9999 or mon_i < 1 or mon_i > 12:
            raise ValueError("invalid month")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="month must be YYYY-MM") from exc

    import calendar

    start_date = f"{year_i:04d}-{mon_i:02d}-01"
    last_day = calendar.monthrange(year_i, mon_i)[1]
    end_date = f"{year_i:04d}-{mon_i:02d}-{last_day:02d}"
    return start_date, end_date


def _fetch_month_transactions_with_cny(
    cur,
    start_date: str,
    end_date: str,
) -> List[Dict[str, Any]]:
    cur.execute(
        """
        SELECT
            t.id, t.date, t.amount, t.currency, t.category, t.subcategory,
            t.account_code, t.description, t.location, t.auto_classified,
            t.source, t.created_at,
            CASE WHEN UPPER(t.currency) = 'CNY' THEN 1 WHEN fx.rate > 0 THEN fx.rate END AS fx_rate,
            t.amount * CASE WHEN UPPER(t.currency) = 'CNY' THEN 1 WHEN fx.rate > 0 THEN fx.rate END AS amount_cny
        FROM pams_transactions t
        LEFT JOIN LATERAL (
            SELECT rate FROM pams_fx_rates_daily
            WHERE base_currency = 'CNY' AND quote_currency = t.currency
              AND as_of <= t.date
            ORDER BY as_of DESC LIMIT 1
        ) fx ON TRUE
        WHERE t.date >= %s::date AND t.date <= %s::date
        """,
        (start_date, end_date),
    )
    return [dict(r) for r in cur.fetchall()]


def _is_internal_transfer(tx: Dict[str, Any]) -> bool:
    return (
        (tx.get("category") or "") == INTERNAL_TRANSFER_CATEGORY
        or (tx.get("subcategory") or "") == INTERNAL_TRANSFER_SUBCATEGORY
    )


def _filter_transactions(
    rows: List[Dict[str, Any]],
    view: str,
    include_large: bool,
    large_threshold: float,
) -> Tuple[List[Dict[str, Any]], Dict[str, float]]:
    normalized_view = (view or "operating").lower()
    kept = rows
    excluded_transfer_count = 0
    if normalized_view == "operating":
        kept = [r for r in kept if not _is_internal_transfer(r)]
        excluded_transfer_count = len(rows) - len(kept)

    if any(row.get("amount_cny") is None for row in kept):
        raise HTTPException(status_code=422, detail="Missing exchange rate or transaction amount; cannot calculate CNY totals")

    excluded_large_count = 0
    excluded_large_amount_cny = 0.0
    if not include_large:
        next_rows: List[Dict[str, Any]] = []
        for r in kept:
            amount_cny = float(r["amount_cny"])
            if abs(amount_cny) >= large_threshold:
                excluded_large_count += 1
                excluded_large_amount_cny += abs(amount_cny)
                continue
            next_rows.append(r)
        kept = next_rows

    meta = {
        "view": normalized_view,
        "include_large": include_large,
        "large_threshold_cny": large_threshold,
        "excluded_transfer_count": excluded_transfer_count,
        "excluded_large_count": excluded_large_count,
        "excluded_large_amount_cny": excluded_large_amount_cny,
    }
    return kept, meta


def _requested_filters(view, include_large, large_threshold):
    if view is None and include_large is None and large_threshold is None:
        return None
    view = "operating" if view is None else view
    if view not in {"all", "operating"}:
        raise HTTPException(status_code=422, detail="view must be all or operating")
    try:
        threshold = Decimal(str(DEFAULT_LARGE_THRESHOLD_CNY if large_threshold is None else large_threshold))
    except Exception as exc:
        raise HTTPException(status_code=422, detail="invalid large threshold") from exc
    if not threshold.is_finite() or threshold < 0:
        raise HTTPException(status_code=422, detail="invalid large threshold")
    return view, False if include_large is None else include_large, threshold


def _read_filtered_transactions(start, end, filters, get_conn):
    view, include_large, threshold = filters
    with get_conn() as conn:
        conn.set_session(isolation_level="REPEATABLE READ", readonly=True)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            rows = _fetch_month_transactions_with_cny(cur, start, end)
    return _filter_transactions(rows, view, include_large, float(threshold))


def _filtered_monthly(month, filters, get_conn):
    filtered_rows, meta = _read_filtered_transactions(*month_range(month), filters, get_conn)

    total_income = 0.0
    total_expense = 0.0
    by_category_map: Dict[str, Dict[str, float]] = {}
    for tx in filtered_rows:
        amount_cny = float(tx["amount_cny"])
        if amount_cny > 0:
            total_income += amount_cny
        elif amount_cny < 0:
            total_expense += abs(amount_cny)
            category = tx.get("category") or "未分类"
            bucket = by_category_map.setdefault(category, {"amount": 0.0, "count": 0})
            bucket["amount"] += abs(amount_cny)
            bucket["count"] += 1

    by_category = [
        {"category": k, "amount": round(v["amount"], 2), "count": int(v["count"])}
        for k, v in sorted(by_category_map.items(), key=lambda item: item[1]["amount"], reverse=True)
    ]
    return {
        "month": month,
        "total_income": str(round(total_income, 4)),
        "total_expense": str(round(total_expense, 4)),
        "net": str(round(total_income - total_expense, 4)),
        "by_category": [{k: _norm(v) for k, v in dict(r).items()} for r in by_category],
        "filters": meta,
    }


def _filtered_daily(month, filters, get_conn):
    filtered_rows, _ = _read_filtered_transactions(*month_range(month), filters, get_conn)
    daily: Dict[int, Dict[str, float]] = {}
    for tx in filtered_rows:
        day = int(tx["date"].day)
        amount_cny = float(tx["amount_cny"])
        bucket = daily.setdefault(day, {"day": day, "income": 0.0, "expense": 0.0})
        if amount_cny > 0:
            bucket["income"] += amount_cny
        elif amount_cny < 0:
            bucket["expense"] += abs(amount_cny)
    return [daily[d] for d in sorted(daily.keys())]


def _filtered_categories(start, end, filters, get_conn):
    filtered_rows, _ = _read_filtered_transactions(start, end, filters, get_conn)
    by_category_map: Dict[str, Dict[str, float]] = {}
    for tx in filtered_rows:
        amount_cny = float(tx["amount_cny"])
        if amount_cny >= 0:
            continue
        category = tx.get("category") or "未分类"
        bucket = by_category_map.setdefault(category, {"amount": 0.0, "count": 0})
        bucket["amount"] += abs(amount_cny)
        bucket["count"] += 1
    return [
        {"category": k, "amount": round(v["amount"], 2), "count": int(v["count"])}
        for k, v in sorted(by_category_map.items(), key=lambda item: item[1]["amount"], reverse=True)
    ]
