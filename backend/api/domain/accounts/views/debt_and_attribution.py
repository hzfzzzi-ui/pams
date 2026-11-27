"""Monthly Net Worth attribution summary and multi-currency debt / credit-card due-date panel."""
from __future__ import annotations

import os
from calendar import monthrange
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from domain.accounts.views.account_policy import excluded_accounts, owner_ratio as policy_owner_ratio
from domain.investments.valuation.page_projection import read_page


CREDIT_CARD_DUE_DAYS: dict[str, tuple[str, int]] = {
    "ccb_credit_0453": ("建行 0453 信用卡", 25),
    "gf_7443": ("广发 7443 人民币信用卡", 20),
    "gf_9711": ("广发 9711 美元信用卡", 20),
    "ccb_cny": ("建行 0453 信用卡", 25),
    "cgb_cny": ("广发 7443/9711 信用卡", 20),
}

_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _shanghai_today() -> date:
    return datetime.now(_SHANGHAI).date()


def _load_accounts_config() -> dict[str, Any]:
    for candidate in (
        Path(os.environ.get("PAMS_ACCOUNTS_CONFIG", "/app/config/accounts.yaml")),
        Path(__file__).resolve().parents[5] / "config" / "accounts.yaml",
    ):
        if candidate.exists():
            try:
                data = yaml.safe_load(candidate.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
    return {}


def _ibkr_negative_cash_from_cards(cards: dict[str, Any]) -> float:
    ov = cards.get("investment_overview") or {}
    items = ov.get("financing_items") or []
    ibkr_items = [
        item for item in items
        if isinstance(item, dict)
        and item.get("account_code") == "ibkr"
        and item.get("kind") == "negative_cash"
        and item.get("amount_cny") is not None
    ]
    if ibkr_items:
        return sum(abs(float(item["amount_cny"])) for item in ibkr_items)
    ibkr_card = cards.get("ibkr") or {}
    by_ccy = ibkr_card.get("cash_by_currency")
    if isinstance(by_ccy, list) and by_ccy:
        neg = sum(
            abs(float(entry["amount_base"]))
            for entry in by_ccy
            if isinstance(entry, dict) and entry.get("amount_base") is not None and float(entry["amount_base"]) < 0
        )
        return neg
    ibkr_cash = (ibkr_card.get("cash") or {}).get("value")
    if ibkr_cash is not None and float(ibkr_cash) < 0:
        return abs(float(ibkr_cash))
    return 0.0


def _esop_loan_from_cards(cards: dict[str, Any]) -> float:
    ov = cards.get("investment_overview") or {}
    items = ov.get("financing_items") or []
    loan_items = [
        item for item in items
        if isinstance(item, dict)
        and item.get("kind") == "loan"
        and item.get("amount_cny") is not None
        and (
            "esop" in str(item.get("account_code") or "").lower()
            or "huawei" in str(item.get("account_code") or "").lower()
            or "内部借款" in str(item.get("name") or "")
        )
    ]
    if loan_items:
        return sum(abs(float(item["amount_cny"])) for item in loan_items)
    return 0.0


def _fx_to_cny(cur, currency: str, as_of: date) -> float | None:
    ccy = str(currency or "CNY").strip().upper()
    if ccy == "CNY":
        return 1.0
    cur.execute(
        """
        SELECT rate FROM pams_fx_rates_daily
        WHERE base_currency = 'CNY' AND quote_currency = %s
          AND as_of <= %s AND rate > 0
          AND rate::text NOT IN ('NaN', 'Infinity', '-Infinity')
          AND NULLIF(BTRIM(source), '') IS NOT NULL
        ORDER BY as_of DESC LIMIT 1
        """,
        (ccy, as_of),
    )
    row = cur.fetchone()
    if not row:
        return None
    val = row["rate"] if isinstance(row, dict) else row[0]
    return float(val) if val is not None else None


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
        raw_name = str(card.get("name") or "").strip()
        display_name = label if (not raw_name or raw_name.lower().startswith("cash ")) else raw_name
        due_iso, days_left = next_due_date_countdown(due_day, ref_day)
        rows.append({
            "id": f"cc:{code}:{display_name}",
            "name": display_name,
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
    ref_day = today or _shanghai_today()
    month_start = date(ref_day.year, ref_day.month, 1)
    config = _load_accounts_config()
    excluded = excluded_accounts(config)

    # 1. Investment net value & financing debts from page projection
    ibkr_debt_cny = 0.0
    esop_debt_cny = 0.0
    inv_net_cny = None
    inv_pnl_cny = None
    try:
        projected = read_page(cur, "holdings", business_date=ref_day, member="account_cards")
        cards = (projected or {}).get("data") or {}
        if isinstance(cards, dict) and cards:
            if "investment_overview" not in cards:
                full = read_page(cur, "holdings", business_date=ref_day)
                page_data = (full or {}).get("data") or {}
                if isinstance(page_data, dict) and page_data.get("holdings") and page_data.get("accounts"):
                    from domain.investments.valuation.investment_overview import holdings_overview_card
                    cards = {**cards, "investment_overview": holdings_overview_card(page_data, config)}
            ov = cards.get("investment_overview") or {}
            nv = (ov.get("net_value") or {}).get("value")
            if nv is not None:
                inv_net_cny = round(float(nv), 2)
            pnl = (ov.get("cumulative_pnl") or ov.get("holding_pnl") or ov.get("total_pnl") or {}).get("value")
            if pnl is not None:
                inv_pnl_cny = round(float(pnl), 2)
            ibkr_debt_cny = _ibkr_negative_cash_from_cards(cards)
            esop_debt_cny = _esop_loan_from_cards(cards)
    except Exception:
        pass

    # 2. Credit card liabilities from canonical account_balances projection (fallback: snapshot)
    credit_cards: list[dict[str, Any]] = []
    covered_card_codes: set[str] = set()
    accounts_cfg = config.get("accounts") or {}
    try:
        bal_proj = read_page(cur, "account_balances", business_date=ref_day)
        bal_map = (bal_proj or {}).get("data") or {}
        if isinstance(bal_map, dict):
            for code, cfg in accounts_cfg.items():
                if code in excluded or not isinstance(cfg, dict):
                    continue
                if str(cfg.get("account_type") or "").lower() != "liability" or cfg.get("active") is False:
                    continue
                entry = bal_map.get(code)
                if not isinstance(entry, dict) or entry.get("balance_amount") is None:
                    continue
                covered_card_codes.add(code)
                raw_amt = float(entry["balance_amount"])
                if raw_amt >= -0.01:
                    continue
                ccy = str(entry.get("balance_currency") or cfg.get("base_currency") or "CNY").strip().upper()
                if entry.get("personal_contribution") is not None:
                    val_cny = abs(float(entry["personal_contribution"]))
                else:
                    rate = _fx_to_cny(cur, ccy, ref_day)
                    if rate is None:
                        continue
                    val_cny = abs(raw_amt) * rate * float(policy_owner_ratio(config, code))
                credit_cards.append({
                    "account_code": code,
                    "name": str(cfg.get("display_name") or CREDIT_CARD_DUE_DAYS.get(code, (code, 25))[0]),
                    "currency": ccy,
                    "balance_cny": val_cny,
                })
    except Exception:
        pass

    snapshot_esop_total = 0.0
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
            if code in excluded:
                continue
            name = str(row.get("name") or "")
            ccy = str(row.get("currency") or "CNY").strip().upper()
            raw_val = abs(float(row.get("market_value") or 0.0))
            rate = _fx_to_cny(cur, ccy, ref_day)
            if rate is None:
                continue
            ratio = float(policy_owner_ratio(config, code))
            val = raw_val * rate * ratio
            if "esop" in code.lower() or "huawei" in code.lower() or "内部借款" in name:
                snapshot_esop_total += val
            elif code not in covered_card_codes and ("信用卡" in name or code in CREDIT_CARD_DUE_DAYS):
                credit_cards.append({
                    "account_code": code,
                    "name": name,
                    "currency": ccy,
                    "balance_cny": val,
                })
    except Exception:
        pass
    if esop_debt_cny <= 0.0 and snapshot_esop_total > 0.0:
        esop_debt_cny = snapshot_esop_total

    # 3. Monthly operating cashflow savings via canonical v2 reader (verified, CNY-valued, refund-aware)
    monthly_income = Decimal("0")
    monthly_expense = Decimal("0")
    cashflow_count = 0
    try:
        from domain.ledger.cashflow.cashflow_read_v2 import load_cash_rows
        rows = load_cash_rows(cur, start=month_start, end=ref_day, base_currency="CNY")
        for row in rows or []:
            if row.get("account_code") in excluded:
                continue
            if row.get("source_evidence_status") not in (None, "verified"):
                continue
            if row.get("is_internal_transfer"):
                continue
            ctype = str(row.get("cashflow_type") or "")
            if ctype not in {"operating_income", "operating_expense"}:
                continue
            amt_raw = row.get("amount_cny")
            if amt_raw is None:
                if str(row.get("currency") or "").upper() != "CNY":
                    continue
                amt_raw = row.get("amount")
            if amt_raw is None:
                continue
            signed_amt = Decimal(str(amt_raw))
            cashflow_count += 1
            if ctype == "operating_income":
                monthly_income += signed_amt
            elif ctype == "operating_expense":
                # In cashflow facts, expenses are negative and refunds are positive.
                monthly_expense += -signed_amt
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
    income_f = round(float(monthly_income), 2)
    expense_f = round(float(monthly_expense), 2)

    return {
        "as_of": ref_day.isoformat(),
        "month": month_start.strftime("%Y-%m"),
        "attribution": {
            "monthly_income_cny": income_f,
            "monthly_expense_cny": expense_f,
            "monthly_savings_cny": round(income_f - expense_f, 2),
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
