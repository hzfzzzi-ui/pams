"""Capital-return (投入本金口径) time series grouped strictly by asset type.

All curves express net PnL divided by net personal invested capital (including
broker margin leverage and financing interest for leveraged accounts like IBKR,
and net of ESOP financing loans for ESOP holdings). Time-weighted return (TWR)
is computed alongside and retained for tooltip reference.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Callable

from domain.investments.holdings.position_performance import build_position_return_series
from domain.investments.returns.investment_return_math import norm
from domain.investments.valuation.capital_ownership import funding_history
from domain.investments.valuation.portfolio_state import _load_accounts_config
from domain.investments.valuation.valuation_nav import series_dates
from domain.ledger.cashflow.cashflow_read_v2 import load_cash_rows

ZERO = Decimal("0")
ONE = Decimal("1")
METHOD = "capital_return_by_asset_type"
RETURN_BASIS = "capital_return"

_EXTERNAL_TRANSFER_TOKENS = (
    "CASH RECEIPTS",
    "ELECTRONIC FUND TRANSFER",
    "DISBURSEMENT",
    "DEPOSIT",
    "WITHDRAW",
)
_NON_EXTERNAL_TOKENS = (
    "DIVIDEND",
    "DEBIT INT",
    "CREDIT INT",
    "CUSTODY FEE",
    "SYEP",
    "PAYMENT IN LIEU",
    "WITHHOLDING",
    "TAX",
)

SECTION_LABELS = {
    "total": "总收益率",
    "stocks": "股票",
    "a_fund": "基金",
    "precious_metal": "贵金属",
    "wealth": "理财",
}


def _dec(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return d if d.is_finite() else None


def _is_ibkr_external_transfer(row: dict[str, Any]) -> bool:
    desc = str(row.get("description") or "").upper()
    if not desc or any(tok in desc for tok in _NON_EXTERNAL_TOKENS):
        return False
    return any(tok in desc for tok in _EXTERNAL_TRANSFER_TOKENS)


def _history_rows_by_date(histories: dict[str, Any] | None, account_code: str) -> dict[date, dict[str, Any]]:
    raw = ((histories or {}).get(account_code) or {}).get("rows")
    out: dict[date, dict[str, Any]] = {}
    if isinstance(raw, dict):
        items = raw.items()
        for raw_day, point in items:
            if not isinstance(point, dict):
                continue
            day = raw_day if isinstance(raw_day, date) else date.fromisoformat(str(raw_day)[:10])
            out[day] = point
    elif isinstance(raw, list):
        for point in raw:
            if not isinstance(point, dict) or not point.get("date"):
                continue
            raw_day = point["date"]
            day = raw_day if isinstance(raw_day, date) else date.fromisoformat(str(raw_day)[:10])
            out[day] = point
    return out


def _align_external_flow_day(
    tx_day: date,
    amount_cny: Decimal,
    nlv_by_day: dict[date, Decimal],
    ordered_nlv_days: list[date],
) -> date:
    """Align a T+1 wire deposit/withdrawal to the exact Flex NLV settlement day.

    IBKR cashflow facts record wire initiation date (`event_date`) with no
    `posting_date`, whereas IBKR daily close NLV reflects the cash 1 business
    day later. Crediting the deposit before it enters NLV creates a 1-day fake
    drawdown spike.
    """
    if not ordered_nlv_days or amount_cny == ZERO:
        return tx_day
    threshold = abs(amount_cny) * Decimal("0.45")

    def _step(day: date) -> Decimal | None:
        if day not in nlv_by_day:
            return None
        prior = [d for d in ordered_nlv_days if d < day]
        prev_val = nlv_by_day[prior[-1]] if prior else ZERO
        return nlv_by_day[day] - prev_val

    same_step = _step(tx_day)
    matched_same = (
        same_step is not None
        and ((amount_cny > ZERO and same_step >= threshold) or (amount_cny < ZERO and same_step <= -threshold))
    )
    if matched_same:
        return tx_day

    max_lookahead = tx_day + timedelta(days=5)
    for cand in ordered_nlv_days:
        if cand <= tx_day:
            continue
        if cand > max_lookahead:
            break
        step = _step(cand)
        if step is not None and (
            (amount_cny > ZERO and step >= threshold) or (amount_cny < ZERO and step <= -threshold)
        ):
            return cand
    return tx_day


def _ibkr_leveraged_account_series(
    cur,
    *,
    start: date,
    end: date,
    base_currency: str,
    histories: dict[str, Any] | None,
) -> dict[date, dict[str, Decimal | None]]:
    """Build daily IBKR leveraged equity series (all markets + margin + interest) in CNY."""
    ibkr_hist = _history_rows_by_date(histories, "ibkr")
    if not ibkr_hist:
        return {}

    nlv_by_day: dict[date, Decimal] = {}
    for day, point in ibkr_hist.items():
        val = _dec(point.get("personal_contribution"))
        if val is not None:
            nlv_by_day[day] = val
    if not nlv_by_day:
        return {}
    ordered_nlv_days = sorted(nlv_by_day.keys())

    # Load all verified external capital deposits/withdrawals for IBKR
    cash_rows = load_cash_rows(
        cur,
        start=date(2020, 1, 1),
        end=end,
        base_currency=base_currency.upper(),
        account_codes=["ibkr"],
    )
    ext_flows: dict[date, Decimal] = {}
    for row in cash_rows:
        if not _is_ibkr_external_transfer(row):
            continue
        if row.get("source_evidence_status") != "verified":
            return {}
        amt_cny = _dec(row.get("amount_cny"))
        if amt_cny is None:
            return {}
        tx_day = row.get("transaction_date") or row.get("event_date")
        if not isinstance(tx_day, date):
            tx_day = date.fromisoformat(str(tx_day)[:10])
        settled_day = _align_external_flow_day(tx_day, amt_cny, nlv_by_day, ordered_nlv_days)
        ext_flows[settled_day] = ext_flows.get(settled_day, ZERO) + amt_cny

    # Opening NLV strictly before `start` so a deposit on `start` is never double-counted.
    opening_days = [d for d in nlv_by_day if d < start]
    opening_nlv = nlv_by_day[max(opening_days)] if opening_days else ZERO

    days = series_dates(start, end, "daily")
    out: dict[date, dict[str, Decimal | None]] = {}
    cum_capital = opening_nlv
    prev_nlv: Decimal | None = None
    twr = ZERO
    has_started = opening_nlv > ZERO

    for day in days:
        flow = ext_flows.get(day, ZERO)
        if flow != ZERO:
            cum_capital += flow
            if cum_capital > ZERO:
                has_started = True
        nlv = nlv_by_day.get(day)
        if nlv is None:
            continue
        if nlv > ZERO:
            has_started = True
        if not has_started:
            out[day] = {
                "market_value": ZERO,
                "cumulative_capital": ZERO,
                "total_pnl": ZERO,
                "daily_twr": ZERO,
                "twr": ZERO,
                "flow": ZERO,
            }
            prev_nlv = ZERO
            continue

        pnl = nlv - cum_capital
        daily_twr: Decimal | None = None
        if prev_nlv is not None and prev_nlv > ZERO:
            daily_twr = (nlv - prev_nlv - flow) / prev_nlv
            twr = (ONE + twr) * (ONE + daily_twr) - ONE
        elif prev_nlv is not None and cum_capital > ZERO:
            daily_twr = pnl / cum_capital
            twr = daily_twr
        out[day] = {
            "market_value": nlv,
            "cumulative_capital": cum_capital,
            "total_pnl": pnl,
            "daily_twr": daily_twr,
            "twr": twr,
            "flow": flow,
        }
        prev_nlv = nlv
    return out


def _position_kind_series(
    cur,
    *,
    start: date,
    end: date,
    base_currency: str,
    owner_ratio: Callable[[str], Decimal],
    excluded_accounts: set[str],
    position_kind: str,
) -> dict[date, dict[str, Decimal | None]]:
    """Convert build_position_return_series output into daily (mv, capital, pnl, twr) map."""
    raw = build_position_return_series(
        cur,
        start=start,
        end=end,
        interval="daily",
        base_currency=base_currency,
        owner_ratio=owner_ratio,
        excluded_accounts=excluded_accounts,
        position_kind=position_kind,
    )
    out: dict[date, dict[str, Decimal | None]] = {}
    for row in raw.get("rows") or []:
        if row.get("return_rate") is None or row.get("market_value") is None:
            continue
        day = date.fromisoformat(str(row["date"])[:10])
        mv = _dec(row.get("market_value"))
        pnl = _dec(row.get("total_pnl"))
        if mv is None or pnl is None:
            continue
        cap = mv - pnl
        out[day] = {
            "market_value": mv,
            "cumulative_capital": cap,
            "total_pnl": pnl,
            "daily_twr": _dec(row.get("daily_return_rate")),
            "twr": _dec(row.get("time_weighted_return_rate")),
        }
    return out


def _esop_net_series(
    cur,
    *,
    start: date,
    end: date,
    base_currency: str,
    owner_ratio: Callable[[str], Decimal],
    excluded_accounts: set[str],
    histories: dict[str, Any] | None,
) -> dict[date, dict[str, Decimal | None]]:
    """ESOP net equity series (gross ESOP value minus ESOP financing loan)."""
    gross = _position_kind_series(
        cur,
        start=start,
        end=end,
        base_currency=base_currency,
        owner_ratio=owner_ratio,
        excluded_accounts=excluded_accounts,
        position_kind="reference",
    )
    if not gross:
        return {}
    # huawei_stock history already records ESOP net equity (gross minus ESOP loan).
    esop_net_hist = _history_rows_by_date(histories, "huawei_stock")
    net_by_day: dict[date, Decimal] = {}
    for day, point in esop_net_hist.items():
        val = _dec(point.get("personal_contribution"))
        if val is not None:
            net_by_day[day] = val

    out: dict[date, dict[str, Decimal | None]] = {}
    for day in sorted(gross):
        item = gross[day]
        mv = net_by_day.get(day, item["market_value"] or ZERO)
        pnl = item["total_pnl"] or ZERO
        cap = mv - pnl
        out[day] = {
            "market_value": mv,
            "cumulative_capital": cap,
            "total_pnl": pnl,
            "daily_twr": item["daily_twr"],
            "twr": item["twr"],
        }
    return out


def _combine_capital_series(
    components: list[dict[date, dict[str, Decimal | None]]],
    *,
    start: date,
    end: date,
    section: str,
    base_currency: str,
    coverage_notes: list[str],
) -> dict[str, Any]:
    """Sum active daily components into one capital-return series without drop-out cliffs."""
    active = [c for c in components if c]
    if not active:
        return {
            "section": section,
            "label": SECTION_LABELS.get(section, section),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "interval": "daily",
            "base_currency": base_currency,
            "method": METHOD,
            "return_basis": RETURN_BASIS,
            "return_scope": RETURN_BASIS,
            "status": "unavailable",
            "reason": "no_verified_capital_series",
            "coverage_notes": coverage_notes,
            "rows": [],
        }

    # Stop at the earliest terminal date among STILL-OPEN components so an active
    # component ending 1-2 days earlier never causes an artificial PnL cliff, while
    # a permanently closed component (e.g. redeemed wealth product with mv == 0)
    # carries its realized PnL forward as a flat line through the year-end.
    open_ends = [
        max(c.keys()) for c in active
        if (c[max(c.keys())].get("market_value") or ZERO) != ZERO
    ]
    common_end = min(open_ends) if open_ends else end
    all_days = series_dates(start, common_end, "daily")

    # Pre-track each component's ordered days and last positive capital (for flat post-exit return).
    comp_meta = []
    for comp in active:
        ordered = sorted(comp.keys())
        comp_meta.append({
            "comp": comp,
            "first": ordered[0],
            "last": ordered[-1],
            "last_seen": None,
            "last_pos_cap": ZERO,
        })

    rows: list[dict[str, Any]] = []
    prev_mv: Decimal | None = None
    prev_cap: Decimal | None = None
    prev_pnl: Decimal | None = None
    last_pos_total_cap = ZERO
    twr = ZERO

    for day in all_days:
        mv_sum = ZERO
        cap_sum = ZERO
        pnl_sum = ZERO
        for meta in comp_meta:
            comp = meta["comp"]
            if day < meta["first"]:
                # Not yet invested: contributes 0 (flat 0% line before first buy)
                continue
            if day in comp:
                item = comp[day]
                meta["last_seen"] = item
                item_cap = item.get("cumulative_capital") or ZERO
                if item_cap > ZERO:
                    meta["last_pos_cap"] = item_cap
            else:
                item = meta["last_seen"]
                if item is None:
                    continue
            item_mv = item.get("market_value") or ZERO
            item_cap = item.get("cumulative_capital") or ZERO
            item_pnl = item.get("total_pnl") or ZERO
            if day > meta["last"] and item_mv == ZERO:
                # Permanently closed component: carry realized PnL forward flat
                pnl_sum += item_pnl
            else:
                mv_sum += item_mv
                if item_mv == ZERO and item_cap <= ZERO:
                    # Closed on or before this day: don't subtract negative cap from other components
                    pass
                else:
                    cap_sum += item_cap
                pnl_sum += item_pnl

        if cap_sum > ZERO:
            last_pos_total_cap = cap_sum
            cap_ret: Decimal | None = pnl_sum / cap_sum
        elif pnl_sum != ZERO and last_pos_total_cap > ZERO:
            # Fully liquidated section: hold flat at final realized return rate
            cap_ret = pnl_sum / last_pos_total_cap
        else:
            # Not yet invested: flat 0% line
            cap_ret = ZERO

        daily_twr: Decimal | None = None
        if prev_mv is not None and prev_pnl is not None and prev_cap is not None:
            net_added = max(ZERO, cap_sum - prev_cap)
            denom = prev_mv + net_added
            if denom > ZERO:
                daily_twr = (pnl_sum - prev_pnl) / denom
                if daily_twr > Decimal("-1"):
                    twr = (ONE + twr) * (ONE + daily_twr) - ONE
            else:
                daily_twr = ZERO
        elif not rows:
            daily_twr = ZERO
            twr = ZERO

        display_cap = cap_sum if cap_sum > ZERO else last_pos_total_cap
        rows.append(
            {
                "date": day.isoformat(),
                "market_value": norm(mv_sum),
                "cumulative_capital": norm(display_cap),
                "total_pnl": norm(pnl_sum),
                "return_rate": norm(cap_ret),
                "display_return_rate": norm(cap_ret),
                "capital_return_rate": norm(cap_ret),
                "time_weighted_return_rate": norm(twr) if cap_ret is not None else None,
                "daily_return_rate": norm(daily_twr),
                "return_basis": RETURN_BASIS,
            }
        )
        prev_mv = mv_sum
        prev_cap = cap_sum
        prev_pnl = pnl_sum

    available = any(r["return_rate"] is not None for r in rows)
    return {
        "section": section,
        "label": SECTION_LABELS.get(section, section),
        "start": start.isoformat(),
        "end": common_end.isoformat(),
        "interval": "daily",
        "base_currency": base_currency,
        "method": METHOD,
        "return_basis": RETURN_BASIS,
        "return_scope": RETURN_BASIS,
        "status": "available" if available else "unavailable",
        **({} if available else {"reason": "no_verified_capital_series"}),
        "coverage_notes": coverage_notes,
        "rows": rows,
    }


def _apply_ths_personal_capital_base(
    cur,
    a_stock: dict[date, dict[str, Decimal | None]],
) -> dict[date, dict[str, Decimal | None]]:
    """Use Tonghuashun personal funding balance as capital denominator.

    When A-share stocks are temporarily sold into broker cash for 1-2 days
    before buying another stock, `stock_mv - stock_pnl` shrinks sharply and
    creates artificial upward spikes in return rate. The true invested capital
    in the brokerage account is the cumulative personal net deposit balance.
    """
    if not a_stock:
        return a_stock
    try:
        ths_cfg = (_load_accounts_config().get("accounts") or {}).get("tonghuashun") or {}
        history = funding_history(cur, "tonghuashun", ths_cfg)
    except Exception:
        return a_stock
    if not history:
        return a_stock
    schedule: list[tuple[date, Decimal]] = []
    for entry in history:
        d = date.fromisoformat(str(entry["date"])[:10])
        bal = _dec(entry.get("personal_balance"))
        if bal is not None and bal > ZERO:
            schedule.append((d, bal))
    if not schedule:
        return a_stock
    schedule.sort(key=lambda x: x[0])

    out: dict[date, dict[str, Decimal | None]] = {}
    for day, item in a_stock.items():
        personal_cap: Decimal | None = None
        for s_day, s_bal in schedule:
            if s_day <= day:
                personal_cap = s_bal
            else:
                break
        if personal_cap is not None and personal_cap > ZERO:
            out[day] = {**item, "cumulative_capital": personal_cap}
        else:
            out[day] = item
    return out


def build_asset_type_capital_return_bundle(
    cur,
    *,
    start: date,
    end: date,
    base_currency: str,
    owner_ratio: Callable[[str], Decimal],
    excluded_accounts: set[str],
    histories: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Build capital-return curves for total, stocks, a_fund, precious_metal, and wealth."""
    a_stock = _apply_ths_personal_capital_base(
        cur,
        _position_kind_series(
            cur, start=start, end=end, base_currency=base_currency,
            owner_ratio=owner_ratio, excluded_accounts=excluded_accounts, position_kind="stock",
        ),
    )
    ibkr_leveraged = _ibkr_leveraged_account_series(
        cur, start=start, end=end, base_currency=base_currency, histories=histories,
    )
    fund = _position_kind_series(
        cur, start=start, end=end, base_currency=base_currency,
        owner_ratio=owner_ratio, excluded_accounts=excluded_accounts, position_kind="fund",
    )
    gold = _position_kind_series(
        cur, start=start, end=end, base_currency=base_currency,
        owner_ratio=owner_ratio, excluded_accounts=excluded_accounts, position_kind="gold",
    )
    wealth = _position_kind_series(
        cur, start=start, end=end, base_currency=base_currency,
        owner_ratio=owner_ratio, excluded_accounts=excluded_accounts, position_kind="wealth",
    )
    esop_net = _esop_net_series(
        cur, start=start, end=end, base_currency=base_currency,
        owner_ratio=owner_ratio, excluded_accounts=excluded_accounts, histories=histories,
    )

    stocks_payload = _combine_capital_series(
        [a_stock, ibkr_leveraged],
        start=start, end=end, section="stocks", base_currency=base_currency,
        coverage_notes=[
            "股票口径包含 A股个股（同花顺）与盈透海外全市场（美/港/韩/瑞，含融资杠杆与利息），按自有投入本金计算收益率。"
        ],
    )
    fund_payload = _combine_capital_series(
        [fund],
        start=start, end=end, section="a_fund", base_currency=base_currency,
        coverage_notes=["基金口径按已核验持仓净值与累计净投入本金计算收益率。"],
    )
    gold_payload = _combine_capital_series(
        [gold],
        start=start, end=end, section="precious_metal", base_currency=base_currency,
        coverage_notes=["贵金属口径按黄金收盘价与累计净投入本金计算收益率。"],
    )
    wealth_payload = _combine_capital_series(
        [wealth],
        start=start, end=end, section="wealth", base_currency=base_currency,
        coverage_notes=["理财口径按已核验理财估值与累计净投入本金计算收益率。"],
    )
    total_payload = _combine_capital_series(
        [a_stock, ibkr_leveraged, fund, gold, wealth, esop_net],
        start=start, end=end, section="total", base_currency=base_currency,
        coverage_notes=[
            "总收益率按全投资组合自有投入本金口径计算（含 IBKR 融资杠杆与利息、扣除 ESOP 借款），悬浮提示附带时间加权收益率（TWR）。"
        ],
    )

    return {
        "total": total_payload,
        "stocks": stocks_payload,
        "a_fund": fund_payload,
        "precious_metal": gold_payload,
        "wealth": wealth_payload,
    }
