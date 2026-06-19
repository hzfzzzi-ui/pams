"""Pure investment return calculation helpers."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date as Date, timedelta
from decimal import Decimal
from typing import Any


def dec(value: Any) -> Decimal:
    """Convert a possibly-empty numeric value to Decimal."""
    if value in (None, ""):
        return Decimal("0")
    return Decimal(str(value))


def ibkr_return_effective_date(trade_date: Date, currency: Any) -> Date:
    """Map an IBKR trade date to the Beijing return bucket used by PAMS."""
    if str(currency or "").upper() == "USD":
        return trade_date + timedelta(days=1)
    return trade_date


def norm(value: Any) -> Any:
    """Normalize values for legacy JSON responses."""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def annualized_rate(rate: Decimal, start_date: Date, end_date: Date) -> Decimal | None:
    """Annualize a period return using calendar days."""
    days = max((end_date - start_date).days, 1)
    if rate <= Decimal("-1"):
        return None
    try:
        value = (float(Decimal("1") + rate) ** (365.0 / days)) - 1.0
    except Exception:
        return None
    return Decimal(str(value))


def derived_cumulative_anchor(end_rate: Decimal, period_rate: Any) -> Decimal | None:
    """Return the cumulative return at a period start from an official period return."""
    if period_rate is None:
        return None
    period = Decimal(str(period_rate))
    if period <= Decimal("-1"):
        return None
    return (Decimal("1") + end_rate) / (Decimal("1") + period) - Decimal("1")


def row_index_on_or_after(rows: list[dict[str, Any]], anchor_date: Date) -> int | None:
    """Return the first row index on/after anchor_date, or the last row before it."""
    if not rows:
        return None
    for idx, row in enumerate(rows):
        if row["date"] >= anchor_date:
            return idx
    return len(rows) - 1


def apply_reported_display_anchor(rows: list[dict[str, Any]], reported: dict[str, Any] | None) -> None:
    """Calibrate the display curve to official platform return anchors."""
    if not rows or not reported or reported.get("total_return_rate") is None:
        return
    target = Decimal(str(reported["total_return_rate"]))
    anchor_date = rows[-1]["date"]
    if reported.get("as_of"):
        try:
            anchor_date = Date.fromisoformat(str(reported["as_of"])[:10])
        except (TypeError, ValueError):
            anchor_date = rows[-1]["date"]
    target_idx = row_index_on_or_after(rows, anchor_date)
    if target_idx is None:
        target_idx = len(rows) - 1
    anchor_row_date = rows[target_idx]["date"]

    for row in rows:
        row["display_return_rate_raw"] = row["display_return_rate"]

    anchors_by_idx: dict[int, Decimal] = {0: dec(rows[0].get("display_return_rate"))}
    ytd_anchor = derived_cumulative_anchor(target, reported.get("ytd_return_rate"))
    if ytd_anchor is not None:
        idx = row_index_on_or_after(rows, Date(anchor_row_date.year, 1, 1))
        if idx is not None:
            anchors_by_idx[idx] = ytd_anchor

    three_month_anchor = derived_cumulative_anchor(target, reported.get("three_month_return_rate"))
    if three_month_anchor is not None:
        idx = row_index_on_or_after(rows, anchor_row_date - timedelta(days=90))
        if idx is not None:
            anchors_by_idx[idx] = three_month_anchor

    one_month_anchor = derived_cumulative_anchor(target, reported.get("one_month_return_rate"))
    if one_month_anchor is not None:
        idx = row_index_on_or_after(rows, anchor_row_date - timedelta(days=30))
        if idx is not None:
            anchors_by_idx[idx] = one_month_anchor

    anchors_by_idx[target_idx] = target
    anchor_items = sorted(anchors_by_idx.items())
    if len(anchor_items) < 2:
        rows[target_idx]["display_return_rate"] = target
        anchor_raw = dec(rows[target_idx].get("display_return_rate_raw"))
        for idx in range(target_idx + 1, len(rows)):
            rows[idx]["display_return_rate"] = target + dec(rows[idx].get("display_return_rate_raw")) - anchor_raw
        return

    for (left_idx, left_target), (right_idx, right_target) in zip(anchor_items, anchor_items[1:]):
        left_raw = dec(rows[left_idx].get("display_return_rate_raw"))
        right_raw = dec(rows[right_idx].get("display_return_rate_raw"))
        left_delta = left_target - left_raw
        right_delta = right_target - right_raw
        span_days = max((rows[right_idx]["date"] - rows[left_idx]["date"]).days, 1)
        for idx in range(left_idx, right_idx + 1):
            elapsed = max((rows[idx]["date"] - rows[left_idx]["date"]).days, 0)
            weight = Decimal(elapsed) / Decimal(span_days)
            adjustment = left_delta + (right_delta - left_delta) * weight
            rows[idx]["display_return_rate"] = dec(rows[idx].get("display_return_rate_raw")) + adjustment

    first_idx = anchor_items[0][0]
    for idx in range(0, first_idx):
        rows[idx]["display_return_rate"] = rows[idx]["display_return_rate_raw"]
    last_idx = anchor_items[-1][0]
    last_target = anchor_items[-1][1]
    last_raw = dec(rows[last_idx].get("display_return_rate_raw"))
    for idx in range(last_idx + 1, len(rows)):
        rows[idx]["display_return_rate"] = last_target + dec(rows[idx].get("display_return_rate_raw")) - last_raw


def section_return_summary(
    *,
    rows: list[dict[str, Any]],
    reported: dict[str, Any] | None,
    owner_ratio_lookup: Callable[[str], Decimal] | None = None,
) -> dict[str, Any]:
    """Build total return, total return rate, and annualized return for a section."""
    if not rows:
        return {
            "total_return": Decimal("0"),
            "total_return_rate": Decimal("0"),
            "annualized_return_rate": Decimal("0"),
        }
    latest = rows[-1]
    total_return = dec(latest.get("market_value")) - dec(latest.get("cumulative_capital"))
    total_return_rate = dec(latest.get("display_return_rate"))
    annualized = annualized_rate(total_return_rate, rows[0]["date"], latest["date"])

    if reported:
        account_code = str(reported.get("account_code") or "")
        owner_ratio = owner_ratio_lookup(account_code) if owner_ratio_lookup else Decimal("1")
        if reported.get("total_return") is not None:
            total_return = Decimal(str(reported["total_return"])) * owner_ratio
        elif reported.get("total_return_gross") is not None:
            total_return = Decimal(str(reported["total_return_gross"])) * owner_ratio
        elif reported.get("total_ytd_pnl") is not None:
            total_return = Decimal(str(reported["total_ytd_pnl"])) * owner_ratio
        if reported.get("total_return_rate") is not None:
            total_return_rate = Decimal(str(reported["total_return_rate"]))
        elif reported.get("ytd_return_rate") is not None:
            total_return_rate = Decimal(str(reported["ytd_return_rate"]))
        if reported.get("annualized_return_rate") is not None:
            annualized = Decimal(str(reported["annualized_return_rate"]))

    return {
        "total_return": total_return,
        "total_return_rate": total_return_rate,
        "annualized_return_rate": annualized if annualized is not None else Decimal("0"),
    }


def bucket_flow_events(dates: list[Date], events: list[tuple[Date, Decimal]]) -> dict[Date, Decimal]:
    """Bucket dated cash-flow events into the sample grid."""
    flows = {d: Decimal("0") for d in dates}
    if not dates or not events:
        return flows
    ordered = sorted(events, key=lambda item: item[0])
    idx = 0
    prev_date: Date | None = None
    for sample_date in dates:
        total = Decimal("0")
        while idx < len(ordered) and ordered[idx][0] <= sample_date:
            event_date, amount = ordered[idx]
            if prev_date is None:
                if event_date >= dates[0]:
                    total += amount
            elif event_date > prev_date:
                total += amount
            idx += 1
        flows[sample_date] = total
        prev_date = sample_date
    return flows


def cash_flow_adjusted_return_rows(
    *,
    dates: list[Date],
    values: dict[Date, Decimal],
    flows: dict[Date, Decimal],
) -> list[dict[str, Decimal | Date]]:
    """Build a time-weighted return curve after excluding net capital flows."""
    rows: list[dict[str, Decimal | Date]] = []
    twr = Decimal("0")
    prev_value: Decimal | None = None
    cumulative_capital = Decimal("0")
    capital_initialized = False
    eps = Decimal("0.0001")
    for sample_date in dates:
        value = values.get(sample_date, Decimal("0"))
        net_flow = flows.get(sample_date, Decimal("0"))
        if not capital_initialized:
            cumulative_capital = net_flow if abs(net_flow) > eps else max(value, Decimal("0"))
            capital_initialized = True
        else:
            cumulative_capital += net_flow

        if prev_value is None or abs(prev_value) <= eps:
            daily_return = Decimal("0")
        else:
            daily_return = (value - prev_value - net_flow) / abs(prev_value)
            twr = (Decimal("1") + twr) * (Decimal("1") + daily_return) - Decimal("1")
        capital_return = (
            (value - cumulative_capital) / cumulative_capital
            if cumulative_capital > eps
            else Decimal("0")
        )
        rows.append(
            {
                "date": sample_date,
                "market_value": value,
                "net_flow": net_flow,
                "cumulative_capital": cumulative_capital,
                "capital_return_rate": capital_return,
                "daily_return_rate": daily_return,
                "time_weighted_return_rate": twr,
            }
        )
        prev_value = value
    return rows
