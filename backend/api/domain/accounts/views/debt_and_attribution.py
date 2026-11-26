"""Monthly Net Worth attribution summary and multi-currency debt / credit-card due-date panel."""
from __future__ import annotations

from calendar import monthrange
from datetime import date
from typing import Any

from domain.investments.valuation.page_projection import read_page


CREDIT_CARD_DUE_DAYS: dict[str, tuple[str, int]] = {
    "ccb_cny": ("建行 0453 信用卡", 25),
    "cgb_cny": ("广发 7443/9711 信用卡", 20),
}


def next_due_date_countdown(due_day: int, ref_day: date) -> tuple[str, int]:
    """Compute next monthly repayment date and days remaining."""
    days_this_month = monthrange(ref_day.year, ref_day.month)[1]
    candidate = date(ref_day.year, ref_day.month, min(due_day, days_this_month))
    if candidate < ref_day:
        next_year = ref_day.year + (1 if ref_day.month == 12 else 0)
        next_month = 1 if ref_day.month == 12 else ref_day.month + 1
        days_next = monthrange(next_year, next_month)[1]
        candidate = date(next_year, next_month, min(due_day, days_next))
    return candidate.isoformat(), (candidate - ref_day).days


def build_debt_rows(
    *,
    ibkr_debt_cny: float,
    esop_debt_cny: float,
    credit_cards: list[dict[str, Any]],
    ref_day: date,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if ibkr_debt_cny > 1.0:
        rows.append({
            "id": "ibkr_margin",
            "name": "盈透证券融资负现金",
            "category": "券商融资",
            "currency": "USD/多币",
            "balance_cny": round(ibkr_debt_cny, 2),
            "apr_pct": 5.8,
            "monthly_interest_cny": round(ibkr_debt_cny * 0.058 / 12.0, 2),
            "due_date": None,
            "days_until_due": None,
            "note": "参考年化 5.8% · 随卖股或入金实时计息",
        })
    if esop_debt_cny > 1.0:
        rows.append({
            "id": "esop_loan",
            "name": "华为 ESOP 内部借款",
            "category": "股权借款",
            "currency": "CNY",
            "balance_cny": round(esop_debt_cny, 2),
            "apr_pct": 3.5,
            "monthly_interest_cny": round(esop_debt_cny * 0.035 / 12.0, 2),
            "due_date": None,
            "days_until_due": None,
            "note": "参考年化 3.5% · 年度分红覆盖付息",
        })
    for card in credit_cards:
        bal = abs(float(card.get("balance_cny") or 0.0))
        if bal <= 0.01:
            continue
        code = str(card.get("account_code") or "")
        label, due_day = CREDIT_CARD_DUE_DAYS.get(code, (str(card.get("name") or code), 25))
        due_iso, days_left = next_due_date_countdown(due_day, ref_day)
        rows.append({
            "id": f"cc:{code}:{card.get('name')}",
            "name": str(card.get("name") or label),
            "category": "信用卡账单",
            "currency": str(card.get("currency") or "CNY"),
            "balance_cny": round(bal, 2),
            "apr_pct": 0.0,
            "monthly_interest_cny": 0.0,
            "due_date": due_iso,
            "days_until_due": days_left,
            "note": f"参考还款日每月 {due_day} 日（免息期内 0 息）",
        })
    return sorted(rows, key=lambda r: (-r["apr_pct"], -r["balance_cny"]))


def build_networth_and_debts_payload(cur, *, today: date | None = None) -> dict[str, Any]:
    ref_day = today or date.today()
    month_start = date(ref_day.year, ref_day.month, 1)

    # 1. Monthly operating cashflow savings (income - expense)
    monthly_income = 0.0
    monthly_expense = 0.0
    cashflow_count = 0
    try:
        cur.execute(
            """
            SELECT cashflow_type,
                   COALESCE(SUM(ABS(COALESCE(amount_cny, amount))), 0) AS total_amt,
                   COUNT(*) AS cnt
            FROM pams_cashflow_fact
            WHERE event_date >= %s AND event_date <= %s
              AND cashflow_type IN ('operating_income', 'operating_expense')
            GROUP BY cashflow_type
            """,
            (month_start, ref_day),
        )
        for row in cur.fetchall():
            ctype = row["cashflow_type"]
            amt = float(row["total_amt"] or 0.0)
            cashflow_count += int(row["cnt"] or 0)
            if ctype == "operating_income":
                monthly_income += amt
            elif ctype == "operating_expense":
                monthly_expense += amt
    except Exception:
        pass

    # 2. Investment net value & financing debts from page projection
    ibkr_debt_cny = 0.0
    esop_debt_cny = 0.0
    inv_net_cny = None
    inv_pnl_cny = None
    try:
        projected = read_page(cur, "holdings", business_date=ref_day, member="account_cards")
        cards = (projected or {}).get("data") or {}
        if isinstance(cards, dict):
            ov = cards.get("investment_overview") or {}
            nv = (ov.get("net_value") or {}).get("value")
            if nv is not None:
                inv_net_cny = round(float(nv), 2)
            pnl = (ov.get("holding_pnl") or ov.get("total_pnl") or {}).get("value")
            if pnl is not None:
                inv_pnl_cny = round(float(pnl), 2)
            ibkr_cash = ((cards.get("ibkr") or {}).get("cash") or {}).get("value")
            if ibkr_cash is not None and float(ibkr_cash) < 0:
                ibkr_debt_cny = abs(float(ibkr_cash))
    except Exception:
        pass

    # Check ESOP loan + credit card liabilities from latest snapshots
    credit_cards: list[dict[str, Any]] = []
    try:
        cur.execute(
            """
            SELECT DISTINCT ON (account_code, name)
                   account_code, name, currency, market_value, as_of
            FROM pams_holdings_snapshot
            WHERE asset_liability = '负债'
            ORDER BY account_code, name, as_of DESC
            """
        )
        for row in cur.fetchall():
            code = str(row.get("account_code") or "")
            name = str(row.get("name") or "")
            val = abs(float(row.get("market_value") or 0.0))
            if "esop" in code.lower() or "内部借款" in name:
                esop_debt_cny = max(esop_debt_cny, val)
            elif "信用卡" in name or code in CREDIT_CARD_DUE_DAYS:
                credit_cards.append({
                    "account_code": code,
                    "name": name,
                    "currency": row.get("currency") or "CNY",
                    "balance_cny": val,
                })
    except Exception:
        pass

    debts = build_debt_rows(
        ibkr_debt_cny=ibkr_debt_cny,
        esop_debt_cny=esop_debt_cny,
        credit_cards=credit_cards,
        ref_day=ref_day,
    )
    total_debt_cny = round(sum(d["balance_cny"] for d in debts), 2)
    total_monthly_interest_cny = round(sum(d["monthly_interest_cny"] for d in debts), 2)

    return {
        "as_of": ref_day.isoformat(),
        "month": month_start.strftime("%Y-%m"),
        "attribution": {
            "monthly_income_cny": round(monthly_income, 2),
            "monthly_expense_cny": round(monthly_expense, 2),
            "monthly_savings_cny": round(monthly_income - monthly_expense, 2),
            "cashflow_count": cashflow_count,
            "investment_net_value_cny": inv_net_cny,
            "investment_pnl_cny": inv_pnl_cny,
            "note": "收支结余来自本月已入账现金流事实；投资净值来自持仓投影。",
        },
        "debts": {
            "total_debt_cny": total_debt_cny,
            "total_monthly_interest_cny": total_monthly_interest_cny,
            "items": debts,
        },
    }
