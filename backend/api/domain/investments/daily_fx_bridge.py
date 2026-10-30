"""Pure cash-and-positions bridge; never interpret accruals as investment FX.

Rates are CNY per native unit at each session's end of day. All intervening
activities use the closing rate, not intraday execution FX. Inputs are signed
native Decimal amounts, bounded to 1e18 with at most 36 fractional places
and 80 coefficient digits. Products retain up to 72 fractional places exactly.
Adapters must provide explicit, evidenced zeros for every state/activity key.
Missing CNY FX is the mathematical identity; supplied evidence is not overridden.
"""

from __future__ import annotations

from datetime import date
from decimal import (
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    Inexact,
    InvalidOperation,
    Overflow,
    Rounded,
    localcontext,
)
from typing import Any

MAX_CURRENCIES = 32
MAX_AMOUNT = Decimal("1e18")
MAX_RATE = Decimal("1e6")
MIN_RATE = Decimal("1e-12")
OFFICIAL_TOLERANCE_CNY = Decimal("0.01")
_ZERO = Decimal(0)
_ONE = Decimal(1)
_CONTEXT = Context(
    prec=192, rounding=ROUND_HALF_EVEN, Emin=-200, Emax=200,
    traps=[InvalidOperation, Overflow, Inexact, Rounded],
)
# Fiat codes, including the offshore RMB convention; not metal/crypto tickers.
_CURRENCIES = frozenset({
    "AED", "AFN", "ALL", "AMD", "AOA", "ARS", "AUD", "AWG", "AZN", "BAM", "BBD", "BDT",
    "BGN", "BHD", "BIF", "BMD", "BND", "BOB", "BRL", "BSD", "BTN", "BWP", "BYN", "BZD",
    "CAD", "CDF", "CHF", "CLP", "CNY", "CNH", "COP", "CRC", "CUP", "CVE", "CZK", "DJF",
    "DKK", "DOP", "DZD", "EGP", "ERN", "ETB", "EUR", "FJD", "FKP", "GBP", "GEL", "GHS",
    "GIP", "GMD", "GNF", "GTQ", "GYD", "HKD", "HNL", "HTG", "HUF", "IDR", "ILS", "INR",
    "IQD", "IRR", "ISK", "JMD", "JOD", "JPY", "KES", "KGS", "KHR", "KMF", "KPW", "KRW",
    "KWD", "KYD", "KZT", "LAK", "LBP", "LKR", "LRD", "LSL", "LYD", "MAD", "MDL", "MGA",
    "MKD", "MMK", "MNT", "MOP", "MRU", "MUR", "MVR", "MWK", "MXN", "MYR", "MZN", "NAD",
    "NGN", "NIO", "NOK", "NPR", "NZD", "OMR", "PAB", "PEN", "PGK", "PHP", "PKR", "PLN",
    "PYG", "QAR", "RON", "RSD", "RUB", "RWF", "SAR", "SBD", "SCR", "SDG", "SEK", "SGD",
    "SHP", "SLE", "SOS", "SRD", "SSP", "STN", "SVC", "SYP", "SZL", "THB", "TJS", "TMT",
    "TND", "TOP", "TRY", "TTD", "TWD", "TZS", "UAH", "UGX", "USD", "UYU", "UZS", "VES",
    "VND", "VUV", "WST", "XAF", "XCD", "XCG", "XOF", "XPF", "YER", "ZAR", "ZMW", "ZWG",
})
_ACTIVITY_FIELDS = frozenset({"trade_cash", "income", "expenses", "funding", "other"})
_METRICS = (
    "price_pnl_cny", "position_fx_pnl_cny", "cash_fx_pnl_cny", "net_fx_pnl_cny",
    "income_pnl_cny", "expense_pnl_cny", "computed_pnl_cny",
    "unexplained_cash_cny", "expected_pnl_cny", "bridge_residual_cny",
)
RATE_CONVENTION = (
    "end_of_day_cny_per_native; opening/closing session rates; "
    "all activities and funding translated at closing rate"
)


def _number(value: Any, label: str, *, limit: Decimal = MAX_AMOUNT) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{label}: Decimal required")
    parts = value.as_tuple()
    if (not value.is_finite() or len(parts.digits) > 80
            or not isinstance(parts.exponent, int) or not -36 <= parts.exponent <= 18
            or value.copy_abs() > limit):
        raise ValueError(f"{label}: non-finite or out-of-bounds Decimal")
    return value


def _keys(value: Any, label: str) -> set[str]:
    if not isinstance(value, dict) or len(value) > MAX_CURRENCIES:
        raise ValueError(f"{label}: dict of at most {MAX_CURRENCIES} currencies required")
    if any(not isinstance(key, str) or key not in _CURRENCIES for key in value):
        raise ValueError(f"{label}: unsupported currency code")
    return set(value)


def _fx(
    currency: str, values: dict, day: date, side: str,
) -> tuple[dict[str, Any], list[str]]:
    if currency not in values and currency == "CNY":
        return {"rate": _ONE, "as_of": day.isoformat(), "source": "cny_identity"}, []
    record = values.get(currency)
    if record is None:
        return {"rate": None, "as_of": None, "source": None}, [f"{side}_fx_missing"]
    if not isinstance(record, dict):
        raise TypeError(f"{currency}.{side}_fx: evidence dict required")
    rate = record.get("rate")
    if rate is not None:
        rate = _number(rate, f"{currency}.{side}_fx.rate", limit=MAX_RATE)
        if currency == "CNY" and rate != _ONE:
            raise ValueError("CNY FX identity must equal exactly 1")
        if rate < MIN_RATE:
            raise ValueError(f"{currency}.{side}_fx: positive bounded rate required")
    as_of = record.get("as_of")
    if as_of is not None and type(as_of) is not date:
        raise ValueError(f"{currency}.{side}_fx.as_of: date required")
    source = record.get("source")
    if source is not None and (not isinstance(source, str) or len(source) > 256):
        raise ValueError(f"{currency}.{side}_fx.source: bounded string required")
    reasons = []
    if rate is None:
        reasons.append(f"{side}_fx_missing_rate")
    if record.get("status") != "confirmed":
        reasons.append(f"{side}_fx_unconfirmed")
    if not source or not source.strip():
        reasons.append(f"{side}_fx_missing_source")
    if as_of is None:
        reasons.append(f"{side}_fx_missing_date")
    elif as_of != day:
        reasons.append(f"{side}_fx_{'future' if as_of > day else 'stale'}")
    return {
        "rate": rate if not reasons else None,
        "as_of": as_of.isoformat() if as_of else None,
        "source": source,
    }, reasons


def _status(reasons: list[str], has_known: bool) -> str:
    return "confirmed" if not reasons else "partial" if has_known else "pending"


def build_daily_fx_bridge(
    *, opening_date: date, closing_date: date,
    opening_positions: dict[str, Decimal], closing_positions: dict[str, Decimal],
    opening_cash: dict[str, Decimal], closing_cash: dict[str, Decimal],
    fx_opening: dict[str, dict[str, Any]], fx_closing: dict[str, dict[str, Any]],
    activities: dict[str, dict[str, Decimal]],
    official_pnl_cny: Decimal | None = None, evidence_complete: bool = True,
) -> dict[str, Any]:
    """Return Decimal diagnostics, not a replacement for official account NAV.

    Unsafe values raise ValueError; non-Decimal numerics/FX records raise
    TypeError. Missing/unconfirmed/wrong-session FX instead produces None and
    reason codes, including for zero exposures.
    State and activity currency sets must match exactly; FX may omit unknowns.
    Every activity needs all five fields; income >= 0 and expenses <= 0.

    computed excludes unexplained cash and other. expected is the cash+position
    balance change net of funding; bridge_residual = expected - computed -
    unexplained. official_residual = official - computed (never forced to zero).
    confirmed requires complete evidence, no unexplained native cash/other,
    exact-session FX, and abs(official_residual) <= 0.01 when official is supplied.
    Only confirmed exposes a headline. known_partial sums available values per
    metric, returning None rather than zero when none are available. All dates
    in the returned value are ISO strings. Inputs and caller context are untouched.
    """
    if type(opening_date) is not date or type(closing_date) is not date:
        raise ValueError("opening_date and closing_date must be dates")
    if opening_date >= closing_date:
        raise ValueError("opening_date must precede closing_date")
    if type(evidence_complete) is not bool:
        raise ValueError("evidence_complete must be bool")
    states = {
        "opening_positions": opening_positions, "closing_positions": closing_positions,
        "opening_cash": opening_cash, "closing_cash": closing_cash,
    }
    currencies = _keys(opening_positions, "opening_positions")
    for label, values in {**states, "activities": activities}.items():
        if _keys(values, label) != currencies:
            raise ValueError(f"{label}: explicit matching currency coverage required")
    for label, values in (("fx_opening", fx_opening), ("fx_closing", fx_closing)):
        if not _keys(values, label) <= currencies:
            raise ValueError(f"{label}: currency outside state coverage")

    # Fixed bounds need fewer than 110 significant digits even at the 32-row cap.
    # A fresh context also prevents inherited precision/traps/flags changing PnL.
    with localcontext(_CONTEXT):
        if official_pnl_cny is not None:
            _number(official_pnl_cny, "official_pnl_cny")
        rows = []
        for currency in sorted(currencies):
            row: dict[str, Any] = {"currency": currency}
            for label, values in states.items():
                row[label] = _number(values[currency], f"{currency}.{label}")
            activity = activities[currency]
            if not isinstance(activity, dict) or set(activity) != _ACTIVITY_FIELDS:
                raise ValueError(f"{currency}.activities: all five explicit fields required")
            row["activities"] = {
                key: _number(activity[key], f"{currency}.activities.{key}")
                for key in sorted(_ACTIVITY_FIELDS)
            }
            if activity["income"] < 0 or activity["expenses"] > 0:
                raise ValueError(f"{currency}.activities: income >= 0, expenses <= 0 required")
            row["opening_fx"], opening_reasons = _fx(currency, fx_opening, opening_date, "opening")
            row["closing_fx"], closing_reasons = _fx(currency, fx_closing, closing_date, "closing")
            reasons = opening_reasons + closing_reasons
            if not evidence_complete:
                reasons.append("evidence_incomplete")
            p0, p1 = row["opening_positions"], row["closing_positions"]
            c0, c1 = row["opening_cash"], row["closing_cash"]
            trade, income, expense, funding = (
                activity[key] for key in ("trade_cash", "income", "expenses", "funding")
            )
            row["unexplained_native"] = c1 - c0 - trade - income - expense - funding
            if row["unexplained_native"] != 0:
                reasons.append("native_cash_residual")
            if activity["other"] != 0:
                reasons.append("other_activity_unclassified")
            row.update(dict.fromkeys(_METRICS))
            r0, r1 = row["opening_fx"]["rate"], row["closing_fx"]["rate"]
            if r1 is not None:
                row["price_pnl_cny"] = (p1 - p0 + trade) * r1
                row["income_pnl_cny"] = income * r1
                row["expense_pnl_cny"] = expense * r1
                row["unexplained_cash_cny"] = row["unexplained_native"] * r1
            if r0 is not None and r1 is not None:
                row["position_fx_pnl_cny"] = p0 * (r1 - r0)
                row["cash_fx_pnl_cny"] = c0 * (r1 - r0)
                row["net_fx_pnl_cny"] = row["position_fx_pnl_cny"] + row["cash_fx_pnl_cny"]
                row["computed_pnl_cny"] = (
                    row["price_pnl_cny"] + row["net_fx_pnl_cny"]
                    + row["income_pnl_cny"] + row["expense_pnl_cny"]
                )
                row["expected_pnl_cny"] = (p1 + c1) * r1 - (p0 + c0) * r0 - funding * r1
                row["bridge_residual_cny"] = (
                    row["expected_pnl_cny"] - row["computed_pnl_cny"] - row["unexplained_cash_cny"]
                )
                if row["bridge_residual_cny"] != 0:
                    reasons.append("balance_bridge_residual")
            row["reasons"] = sorted(reasons)
            row["status"] = _status(reasons, any(row[key] is not None for key in _METRICS))
            rows.append(row)

        summary, known_partial = {}, {}
        for key in _METRICS:
            known = [row[key] for row in rows if row[key] is not None]
            known_partial[key] = sum(known, _ZERO) if known else None
            summary[key] = known_partial[key] if rows and len(known) == len(rows) else None
        summary["official_pnl_cny"] = official_pnl_cny
        summary["official_residual_cny"] = (
            official_pnl_cny - summary["computed_pnl_cny"]
            if official_pnl_cny is not None and summary["computed_pnl_cny"] is not None else None
        )
        account_reasons = {reason for row in rows for reason in row["reasons"]}
        if not evidence_complete:
            account_reasons.add("evidence_incomplete")
        if not rows:
            account_reasons.add("no_currencies")
        if (summary["official_residual_cny"] is not None
                and abs(summary["official_residual_cny"]) > OFFICIAL_TOLERANCE_CNY):
            account_reasons.add("official_residual_exceeds_tolerance")
        status = _status(sorted(account_reasons), any(value is not None for value in known_partial.values()))
        summary["headline_pnl_cny"] = summary["computed_pnl_cny"] if status == "confirmed" else None
        return {
            "opening_date": opening_date.isoformat(), "closing_date": closing_date.isoformat(),
            "rate_convention": RATE_CONVENTION, "status": status,
            "summary": summary, "known_partial": known_partial, "currencies": rows,
            "reasons": sorted(account_reasons),
            "coverage": {
                "currencies_total": len(rows),
                "currencies_with_opening_fx": sum(row["opening_fx"]["rate"] is not None for row in rows),
                "currencies_with_closing_fx": sum(row["closing_fx"]["rate"] is not None for row in rows),
                "currencies_with_complete_calculation": sum(row["computed_pnl_cny"] is not None for row in rows),
                "currencies_with_zero_cash_residual": sum(row["unexplained_native"] == 0 for row in rows),
                "all_fx_exact_dated": bool(rows) and all(
                    row["opening_fx"]["rate"] is not None and row["closing_fx"]["rate"] is not None
                    for row in rows
                ),
                "evidence_complete": evidence_complete,
                "official_pnl_available": official_pnl_cny is not None,
            },
        }
