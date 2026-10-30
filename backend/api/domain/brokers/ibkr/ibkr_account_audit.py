"""Independent, deterministic Flex evidence audit. No DB, files, network or writes.

The caller selects the published XML and supplies provenance-qualified internal
state. Never import the performance/return calculation being audited here.
"""
from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from decimal import Context, Decimal, InvalidOperation, localcontext
from typing import Any

TOLERANCE = Decimal("0.00000001")
ISSUE_LIMIT = 30
MAX_XML_BYTES = 32 * 1024 * 1024  # Resource bound, not an evidence-coverage threshold.
SCOPE = {"ownership_basis": "full_account", "valuation_basis": "ibkr_statement_close"}
FLOW_FIELDS = ("depositsWithdrawals", "internalCashTransfers", "assetTransfers",
               "linkingAdjustments", "debitCardActivity", "billPay")
DAILY_UNSUPPORTED_FLOWS = ("paxosTransfers", "excessFundSweep", "grantActivity", "donations", "carbonCredits")
# Named ChangeInNAV P&L categories are not external capital. Unrecognized
# columns cannot silently become P&L, even when their supplied value is zero.
DAILY_PNL_FIELDS = {
    "advisorFees", "billableSalesTax", "brokerFees", "changeInBrokerFeeAccruals",
    "changeInCGTWithholdingAccruals", "changeInDividendAccruals", "changeInIncentiveCouponAccruals",
    "changeInInterestAccruals", "changeInLiteSurchargeAccruals", "changeInUnrealized", "clientFees",
    "commissionCreditsRedemption", "commissionReceivables", "commissions", "commissionsAtPaxos",
    "corporateActionProceeds", "costAdjustments", "dividends", "feesReceivables", "forexCommissions",
    "fxTranslation", "interest", "mtm", "mtmAtPaxos", "netFxTrading", "other", "otherFees", "otherIncome",
    "realized", "referralFee", "salesTax", "softDollars", "taxReceivables", "transactionTax",
    "transferredPnlAdjustments", "twr", "withholding871m", "withholdingTax", "withholdingTaxCollected",
}


def _currency(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Z]{3}", value, flags=re.ASCII) is not None


def _day(value: Any) -> date | None:
    if isinstance(value, datetime):
        return None
    if isinstance(value, date):
        return value
    text = str(value or "")
    try:
        return datetime.strptime(text, "%Y%m%d").date() if re.fullmatch(r"[0-9]{8}", text) else date.fromisoformat(text)
    except ValueError:
        return None


def _number(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    # Plain broker decimals, correctly grouped thousands, or bounded scientific
    # notation from an adapter; malformed grouping must not change an amount.
    if len(text) > 64 or not re.fullmatch(
        r"[+-]?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?", text
    ):
        return None
    try:
        result = Decimal(text.replace(",", ""))
        return result if result.is_finite() and abs(result.adjusted()) <= 64 else None
    except InvalidOperation:
        return None


def _text(number: Decimal | None) -> str | None:
    return format(number, "f") if number is not None else None


def _elements(root: ET.Element, tag: str) -> list[ET.Element]:
    return [node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == tag]


def _evidence_scope(check: dict[str, Any], root: ET.Element, statement: ET.Element, account: str) -> None:
    tags = {"FlexStatement", "AccountInformation", "CashReport", "CashReportCurrency", "OpenPositions",
            "OpenPosition", "EquitySummaryByReportDateInBase", "ChangeInNAV", "ConversionRate", "ConversionRates"}
    parents = {child: parent for parent in root.iter() for child in parent}
    checked: set[ET.Element] = set()
    for evidence in statement.iter():
        if evidence.tag.rsplit("}", 1)[-1] not in tags:
            continue
        node: ET.Element | None = evidence
        while node is not None and node not in checked:
            checked.add(node)
            if node.get("accountId") not in (None, "", account) or node.get("model"):
                _issue(check, "official_account_or_ownership_mismatch", section=node.tag.rsplit("}", 1)[-1])
            node = parents.get(node)


def _check(key: str) -> dict[str, Any]:
    return {"id": key, "status": "ok", "compared": 0, "issue_count": 0,
            "reason_counts": {}, "issues": [], "truncated": False, "evidence": {}}


def _issue(check: dict[str, Any], reason: str, *, status: str = "review", **detail: Any) -> None:
    if check["status"] != "error":
        check["status"] = status
    check["issue_count"] += 1
    check["reason_counts"][reason] = check["reason_counts"].get(reason, 0) + 1
    if len(check["issues"]) < ISSUE_LIMIT:
        check["issues"].append({"reason": reason, "status": status, **detail})
    check["truncated"] = check["issue_count"] > len(check["issues"])


def _required(check: dict[str, Any], row: Mapping[str, Any], field: str) -> Decimal | None:
    value = _number(row.get(field))
    if value is None:
        _issue(check, "missing_value" if row.get(field) in (None, "") else "invalid_value", field=field)
    return value


def _one(check: dict[str, Any], rows: list[ET.Element], section: str) -> ET.Element | None:
    if len(rows) != 1:
        _issue(check, "missing_evidence" if not rows else "ambiguous_evidence", section=section, count=len(rows))
        return None
    return rows[0]


def _compare(check: dict[str, Any], field: str, expected: Decimal | None, actual: Any) -> None:
    if expected is None:
        return
    value = _number(actual)
    if value is None:
        _issue(check, "missing_internal_value" if actual in (None, "") else "invalid_internal_value", field=field)
        return
    check["compared"] += 1
    delta = value - expected
    if abs(delta) > TOLERANCE:
        _issue(check, "financial_delta", status="error", field=field,
               official=_text(expected), internal=_text(value), delta=_text(delta), tolerance=str(TOLERANCE))


def _basis(check: dict[str, Any], state: Mapping[str, Any], field: str, expected: str) -> bool:
    if state.get(field) != expected:
        _issue(check, "missing_basis" if not state.get(field) else "semantic_mismatch",
               field=field, expected=expected)
        return False
    return True


def _scope(source: dict[str, Any], state: Mapping[str, Any] | None,
           account_id: str, as_of: date, base_currency: str) -> bool:
    if state is None:
        _issue(source, "missing_internal_state")
        return False
    matched = True
    for field, expected in {"account_id": account_id, "base_currency": base_currency, **SCOPE}.items():
        if state.get(field) != expected:
            _issue(source, "missing_basis" if not state.get(field) else "semantic_mismatch", field=field, expected=expected)
            matched = False
    observed = _day(state.get("as_of"))
    if observed != as_of:
        reason = "stale_internal_state" if observed and observed < as_of else "internal_date_mismatch"
        _issue(source, reason, expected=as_of.isoformat(), actual=observed.isoformat() if observed else None)
        matched = False
    return matched


def _rate(check: dict[str, Any], row: ET.Element, rates: list[ET.Element],
          currency: str, base: str, as_of: date) -> Decimal | None:
    if not _currency(currency) or not _currency(base):
        _issue(check, "invalid_currency", currency=currency, base_currency=base)
        return None
    explicit = row.get("fxRateToBase")
    value = _number(explicit) if explicit not in (None, "") else None
    if explicit not in (None, "") and (value is None or value <= 0):
        _issue(check, "invalid_fx", currency=currency)
        return None
    invalid_pair = False
    for quote in rates:
        if _day(quote.get("reportDate")) != as_of:
            continue
        for field in ("fromCurrency", "toCurrency"):
            if not _currency(quote.get(field)):
                _issue(check, "invalid_currency", section="ConversionRate", field=field, currency=quote.get(field))
                invalid_pair = True
    if invalid_pair and currency != base:
        return None
    candidates = [r for r in rates if r.get("fromCurrency") == currency
                  and r.get("toCurrency") == base and _day(r.get("reportDate")) == as_of]
    parsed = [_number(r.get("rate")) for r in candidates]
    if any(r is None or r <= 0 for r in parsed):
        _issue(check, "invalid_fx", currency=currency)
        return None
    values = {r for r in parsed if r is not None}
    if value is not None:
        values.add(value)
    if currency == base:
        if any(v != 1 for v in values):
            _issue(check, "conflicting_fx", currency=currency)
            return None
        values.add(Decimal(1))
    if not values:
        _issue(check, "missing_historical_fx", currency=currency, as_of=as_of.isoformat())
        return None
    if max(values) - min(values) > TOLERANCE:
        _issue(check, "conflicting_fx", currency=currency)
        return None
    return value if value is not None else min(values)


def _cash(check: dict[str, Any], fx: dict[str, Any], statement: ET.Element,
          as_of: date, base: str) -> tuple[Decimal | None, dict[str, Decimal], dict[str, Decimal]]:
    all_rows = _elements(statement, "CashReportCurrency")
    rows = [r for r in all_rows if _day(r.get("toDate")) == as_of]
    base_rows = [r for r in rows if r.get("currency") == "BASE_SUMMARY"]
    native: dict[str, Decimal] = {}
    conversions: dict[str, Decimal] = {}
    converted: list[Decimal] = []
    native_rows = [r for r in rows if r.get("currency") != "BASE_SUMMARY"]
    currencies = {r.get("currency") for r in native_rows}
    observed_currencies = {r.get("currency") for r in all_rows if r.get("currency") != "BASE_SUMMARY"}
    missing_currencies = observed_currencies - currencies
    coverage_complete = bool(native_rows) and not missing_currencies and all(_currency(c) for c in currencies)
    complete = coverage_complete
    for uncovered in all_rows:
        if uncovered.get("currency") not in missing_currencies:
            continue
        raw_date, day = uncovered.get("toDate"), _day(uncovered.get("toDate"))
        reason = ("missing_cash_date" if not raw_date else "invalid_cash_date") if day is None else (
            "stale_cash_date" if day < as_of else "cash_date_mismatch")
        _issue(check, reason, currency=uncovered.get("currency"), actual=raw_date, expected=as_of.isoformat())
    for currency in sorted(currencies, key=str):
        if not currency or not _currency(currency):
            _issue(check, "invalid_currency", field="currency", currency=currency)
            complete = False
            continue
        group = [r for r in native_rows if r.get("currency") == currency]
        row = _one(check, group, "CashReportCurrency:" + str(currency))
        if row is None or row.get("levelOfDetail") not in (None, "", "Currency"):
            _issue(check, "unsupported_cash_scope")
            complete = False
            continue
        value = _required(check, row.attrib, "endingCash")
        rate = _rate(fx, row, _elements(statement, "ConversionRate"), currency, base, as_of)
        if value is not None:
            native[currency] = value
        if rate is not None:
            conversions[currency] = rate
        if value is None or rate is None:
            complete = False
        else:
            converted.append(value * rate)
    cash = None
    if base_rows:
        row = _one(check, base_rows, "BASE_SUMMARY")
        if row is not None:
            if row.get("levelOfDetail") not in (None, "", "BaseCurrency"):
                _issue(check, "unsupported_cash_scope")
            else:
                cash = _required(check, row.attrib, "endingCash")
        basis = "official_base_summary"
    elif complete:
        cash = sum(converted, Decimal(0))
        basis = "native_cash_at_explicit_statement_fx"
    else:
        _issue(check, "missing_evidence", section="BASE_SUMMARY_or_complete_native_cash_fx")
        basis = None
    if complete and cash is not None and abs(sum(converted, Decimal(0)) - cash) > TOLERANCE:
        # Rounding/translation scope needs review; this is not an internal ledger delta.
        _issue(check, "official_cash_fx_difference", delta=_text(sum(converted, Decimal(0)) - cash))
    check["evidence"] = {"cash_base": _text(cash), "basis": basis, "cash_is_signed": True,
                         "additional_financing_deduction": False,
                         "native_cash": {k: _text(v) for k, v in sorted(native.items())[:ISSUE_LIMIT]},
                         "native_currency_count": len(native), "native_fx_complete": complete,
                         "native_currency_coverage_complete": coverage_complete,
                         "observed_native_currency_count": len(observed_currencies),
                         "missing_native_currencies": sorted(str(c) for c in missing_currencies)[:ISSUE_LIMIT],
                         "missing_native_currency_count": len(missing_currencies),
                         "truncated": len(native) > ISSUE_LIMIT or len(missing_currencies) > ISSUE_LIMIT}
    return cash, native, conversions


def _positions(check: dict[str, Any], fx: dict[str, Any], statement: ET.Element,
               as_of: date, base: str) -> tuple[Decimal | None, dict[str, Decimal], list[ET.Element], set[str]]:
    all_rows = _elements(statement, "OpenPosition")
    dated = [r for r in all_rows if _day(r.get("reportDate")) == as_of]
    rows = [r for r in dated if r.get("levelOfDetail") == "SUMMARY"]
    complete = True
    if not _elements(statement, "OpenPositions"):
        _issue(check, "missing_evidence", section="OpenPositions")
        complete = False
    if len(dated) != len(all_rows):
        _issue(check, "position_date_mismatch")
        complete = False
    summary_keys = {(r.get("conid"), r.get("currency")) for r in rows}
    if any((r.get("conid"), r.get("currency")) not in summary_keys for r in dated):
        _issue(check, "missing_position_summary")
        complete = False
    total = Decimal(0)
    identities: set[tuple[str | None, str | None]] = set()
    evidence: list[dict[str, Any]] = []
    conversions: dict[str, Decimal] = {}
    conflicts: set[str] = set()
    rates = _elements(statement, "ConversionRate")
    for row in rows:
        currency, conid = row.get("currency"), row.get("conid")
        identity = (conid, currency)
        valid_identity = bool(conid and _currency(currency) and row.get("symbol")) and identity not in identities
        identities.add(identity)
        if not _currency(currency):
            _issue(check, "invalid_currency", field="currency", currency=currency)
            complete = False
        if not valid_identity or row.get("assetCategory") in (None, "", "CASH"):
            _issue(check, "ambiguous_position_identity" if not valid_identity else "unsupported_position_category")
            complete = False
        quantity = _required(check, row.attrib, "position")
        value = _required(check, row.attrib, "positionValue")
        rate = _rate(fx, row, rates, currency, base, as_of) if currency else None
        if rate is None:
            _issue(check, "position_fx_unverified", currency=currency)
        if quantity is None or value is None or rate is None:
            complete = False
        if value is not None and rate is not None:
            total += value * rate
        if currency and rate is not None:
            if currency in conversions and abs(conversions[currency] - rate) > TOLERANCE:
                _issue(fx, "conflicting_fx", currency=currency)
                conflicts.add(currency)
                complete = False
            conversions[currency] = rate
        if len(evidence) < ISSUE_LIMIT:
            evidence.append({"conid": conid, "symbol": row.get("symbol"), "currency": currency,
                             "quantity": _text(quantity), "position_value": _text(value),
                             "fx_rate_to_base": _text(rate)})
    check["evidence"] = {"position_value_base": _text(total) if complete else None,
                         "position_count": len(rows), "positions": evidence,
                         "truncated": len(rows) > len(evidence), "basis": "signed_market_value",
                         "is_complete_nav": False, "detail_comparison": "not_supplied"}
    for currency in conflicts:
        conversions.pop(currency, None)
    return (total if complete else None), conversions, rows, conflicts


def _position_details(check: dict[str, Any], rows: list[ET.Element], actual: Any,
                      account_id: str, as_of: date) -> None:
    if not isinstance(actual, list) or any(not isinstance(r, Mapping) for r in actual):
        _issue(check, "invalid_internal_positions")
        return
    official = {(r.get("conid"), r.get("currency")): r for r in rows}
    supplied: dict[tuple[Any, Any], Mapping[str, Any]] = {}
    for row in actual:
        if not _currency(row.get("currency")):
            _issue(check, "invalid_internal_currency", field="positions.currency", currency=str(row.get("currency")))
            return
        key = (row.get("conid"), row.get("currency"))
        if not all(isinstance(v, str) and v for v in key) or key in supplied:
            _issue(check, "ambiguous_internal_position_identity")
            return
        if (row.get("account_id", account_id) != account_id
                or _day(row.get("as_of", as_of)) != as_of
                or any(row.get(field, expected) != expected for field, expected in SCOPE.items())):
            _issue(check, "internal_position_scope_mismatch")
            return
        supplied[key] = row
    for key in sorted(official.keys() | supplied.keys()):
        if key not in official or key not in supplied:
            _issue(check, "position_set_mismatch", conid=key[0], currency=key[1],
                   missing_from="official" if key not in official else "internal")
        else:
            for field, raw in (("quantity", "position"), ("position_value", "positionValue")):
                _compare(check, ":".join((*key, field)), _number(official[key].get(raw)), supplied[key].get(field))
    check["evidence"]["detail_comparison"] = "compared"


def _nav(check: dict[str, Any], statement: ET.Element, as_of: date, base: str) -> Decimal | None:
    rows = [r for r in _elements(statement, "EquitySummaryByReportDateInBase") if _day(r.get("reportDate")) == as_of]
    row = _one(check, rows, "EquitySummaryByReportDateInBase")
    value = None
    if row is not None:
        if row.get("currency") not in (None, "", "BASE_SUMMARY", base):
            _issue(check, "semantic_mismatch", field="currency")
        else:
            value = _required(check, row.attrib, "total")
    check["evidence"] = {"net_liquidation_base": _text(value), "basis": "official_nav_total",
                         "cash_plus_positions_is_not_nav": True, "additional_financing_deduction": False}
    return value


def _daily_period_scope(check: dict[str, Any], parents: Mapping[ET.Element, ET.Element],
                        row: ET.Element, statement: ET.Element, account: str, base: str,
                        day: date, *, comparison: bool = False) -> None:
    if row.get("accountId") != account or row.get("currency") != base:
        _issue(check, "daily_period_account_or_base_mismatch")
    node: ET.Element | None = row
    while node is not None:
        if (node.get("accountId") not in (None, "", account) or node.get("model")
                or node.get("currency") not in (None, "", base)
                or node.get("baseCurrency") not in (None, "", base)):
            _issue(check, "daily_period_ancestor_scope_mismatch")
        if node.get("reportDate") is not None and _day(node.get("reportDate")) != day:
            _issue(check, "daily_period_ancestor_date_mismatch")
        if node.get("fromDate") is not None or node.get("toDate") is not None:
            first, last = _day(node.get("fromDate")), _day(node.get("toDate"))
            if first is None or last is None or first > last:
                _issue(check, "daily_period_invalid_scope_range")
            # Only the containing statement may supply a prior-period NAV
            # comparison outside its current-day reporting interval.
            elif not (comparison and node is statement) and not first <= day <= last:
                _issue(check, "daily_period_ancestor_date_mismatch")
        node = parents.get(node)


def _complete_daily_period(check: dict[str, Any], root: ET.Element, start: date, end: date,
                           account: str, base: str, closing_nav: Decimal | None
                           ) -> tuple[dict[str, Decimal | None], int]:
    empty: dict[str, Decimal | None] = {"opening_nav_base": None, "net_external_flow_base": None,
                                      "closing_nav_base": None, "pnl_base": None}
    parents = {child: parent for parent in root.iter() for child in parent}
    statements = _elements(root, "FlexStatement")
    by_day: dict[date, list[ET.Element]] = {}
    bound_bridges = {r for s in statements for r in _elements(s, "ChangeInNAV")}
    for row in _elements(root, "ChangeInNAV"):
        if row not in bound_bridges and row.get("accountId") in (None, "", account):
            first, last = _day(row.get("fromDate")), _day(row.get("toDate"))
            if first is None or last is None or first <= end and last >= start:
                _issue(check, "unbound_daily_period_evidence")
    for statement in statements:
        bridges = _elements(statement, "ChangeInNAV")
        if statement.get("accountId") != account and not any(r.get("accountId") == account for r in bridges):
            continue
        first, last = _day(statement.get("fromDate")), _day(statement.get("toDate"))
        if first is None or last is None or first > last:
            _issue(check, "daily_period_invalid_statement_date")
            continue
        if last < start or first > end:
            continue
        if first != last or not start <= first <= end:
            _issue(check, "partial_daily_period")
            continue
        by_day.setdefault(first, []).append(statement)
    day = start
    while day <= end:
        if day.weekday() < 5 and day not in by_day:
            _issue(check, "missing_daily_period_weekday", date=day.isoformat())
        if day == end:
            break
        day += timedelta(days=1)
    if end not in by_day:
        _issue(check, "missing_daily_period_close")
    if check["issue_count"] or not by_day or min(by_day) == date.min:
        if not check["issue_count"]:
            _issue(check, "invalid_daily_period")
        return empty, len(by_day)
    opening: Decimal | None = None
    previous: Decimal | None = None
    net_flow = Decimal(0)
    for day, candidates in sorted(by_day.items()):
        daily_statement = _one(check, candidates, "daily_period_statement:" + day.isoformat())
        if daily_statement is None:
            continue
        info = _one(check, _elements(daily_statement, "AccountInformation"), "daily_period_account")
        if info is not None:
            _daily_period_scope(check, parents, info, daily_statement, account, base, day)
        nav_rows = _elements(daily_statement, "EquitySummaryByReportDateInBase")
        if any(_day(row.get("reportDate")) is None for row in nav_rows):
            _issue(check, "daily_period_invalid_nav_date")
        current = _one(check, [r for r in nav_rows if _day(r.get("reportDate")) == day], "daily_period_current_nav")
        prior_day = day - timedelta(days=1)
        earliest = prior_day
        while earliest.weekday() >= 5:
            earliest -= timedelta(days=1)
        prior_rows = [r for r in nav_rows if (d := _day(r.get("reportDate"))) is not None
                      and earliest <= d <= prior_day]
        prior = _one(check, prior_rows, "daily_period_independent_prior_nav")
        bridge = _one(check, _elements(daily_statement, "ChangeInNAV"), "daily_period_bridge")
        if info is None or current is None or prior is None or bridge is None:
            continue
        _daily_period_scope(check, parents, current, daily_statement, account, base, day)
        observed_prior = _day(prior.get("reportDate"))
        assert observed_prior is not None
        _daily_period_scope(check, parents, prior, daily_statement, account, base, observed_prior, comparison=True)
        _daily_period_scope(check, parents, bridge, daily_statement, account, base, day)
        if _day(bridge.get("fromDate")) != day or _day(bridge.get("toDate")) != day:
            _issue(check, "partial_daily_period")
        known = {*FLOW_FIELDS, *DAILY_UNSUPPORTED_FLOWS, *DAILY_PNL_FIELDS,
                 "accountId", "acctAlias", "model", "currency", "baseCurrency", "reportDate",
                 "fromDate", "toDate", "startingValue", "endingValue"}
        if set(bridge.attrib) - known or len(bridge):
            _issue(check, "unsupported_period_flow_fields")
        for field in DAILY_UNSUPPORTED_FLOWS:
            value = _required(check, bridge.attrib, field)
            if value is not None and value != 0:
                _issue(check, "unsupported_period_flow_fields", field=field)
        before, after = _required(check, prior.attrib, "total"), _required(check, current.attrib, "total")
        begin, finish = _required(check, bridge.attrib, "startingValue"), _required(check, bridge.attrib, "endingValue")
        flows = [_required(check, bridge.attrib, field) for field in FLOW_FIELDS]
        if before is None or after is None or begin is None or finish is None or any(v is None for v in flows):
            continue
        if abs(before - begin) > TOLERANCE or abs(after - finish) > TOLERANCE:
            _issue(check, "daily_period_nav_bridge_conflict")
        if previous is not None and abs(previous - before) > TOLERANCE:
            _issue(check, "daily_period_nav_chain_conflict")
        if opening is None:
            opening = before
        previous = after
        net_flow += sum((v for v in flows if v is not None), Decimal(0))
    if closing_nav is None or previous is None or abs(previous - closing_nav) > TOLERANCE:
        _issue(check, "official_closing_nav_conflict")
    if check["issue_count"] or opening is None or previous is None:
        return empty, len(by_day)
    return {"opening_nav_base": opening, "net_external_flow_base": net_flow,
            "closing_nav_base": previous, "pnl_base": previous - opening - net_flow}, len(by_day)


def _period(check: dict[str, Any], statement: ET.Element, start: date | None,
            end: date, base: str, closing_nav: Decimal | None, *, root: ET.Element | None = None,
            account_id: str | None = None) -> dict[str, Decimal | None]:
    values: dict[str, Decimal | None] = {"opening_nav_base": None, "net_external_flow_base": None,
                                       "closing_nav_base": None, "pnl_base": None}
    daily_count = None
    if start is None:
        _issue(check, "period_not_requested")
    elif start > end:
        _issue(check, "invalid_period")
    else:
        rows = [r for r in _elements(statement, "ChangeInNAV")
                if _day(r.get("fromDate")) == start and _day(r.get("toDate")) == end]
        if not rows and root is not None and account_id is not None:
            values, daily_count = _complete_daily_period(check, root, start, end, account_id, base, closing_nav)
            row = None
        else:
            row = _one(check, rows, "ChangeInNAV_exact_period")
        if row is not None:
            if row.get("currency") not in (None, "", "BASE_SUMMARY", base):
                _issue(check, "semantic_mismatch", field="currency")
            else:
                opening = _required(check, row.attrib, "startingValue")
                closing = _required(check, row.attrib, "endingValue")
                flows = [_required(check, row.attrib, field) for field in FLOW_FIELDS]
                # New linking/transfer variants are not silently classified as return.
                unknown = [key for key in row.attrib if key not in FLOW_FIELDS
                           and any(part in key.lower() for part in ("transfer", "linking", "deposit", "withdraw"))]
                if unknown:
                    _issue(check, "unsupported_period_flow_fields")
                if opening is not None and closing is not None and all(v is not None for v in flows) and not unknown:
                    net_flow = sum((v for v in flows if v is not None), Decimal(0))
                    values = {"opening_nav_base": opening, "net_external_flow_base": net_flow,
                              "closing_nav_base": closing, "pnl_base": closing - opening - net_flow}
                    if closing_nav is not None and abs(closing_nav - closing) > TOLERANCE:
                        _issue(check, "official_closing_nav_conflict")
                        values["pnl_base"] = None
    check["evidence"] = {**{k: _text(v) for k, v in values.items()},
                         "from_date": start.isoformat() if start else None, "to_date": end.isoformat(),
                         "basis": "nav_change_less_external_flows", "return_ratio": None,
                         "return_ratio_verified": False, "twr_mwr_verified": False}
    if daily_count is not None:
        check["evidence"].update(evidence_basis="complete_daily", bridge_count=daily_count)
    return values


def _history(check: dict[str, Any], statements: list[ET.Element], dates: Sequence[date],
             account_id: str, base: str) -> None:
    # Dates present in a document are observations, not evidence of every day in
    # between, or proof that a stock-only series represents an entire account.
    observed = sorted({day for r in statements if r.get("accountId") == account_id
                       if (day := _day(r.get("toDate"))) is not None})
    requested = sorted(set(dates))
    missing = [d for d in requested if d not in observed]
    _issue(check, "history_coverage_unverified")
    check["evidence"] = {"all_dates_verified": False, "base_currency": base,
                         "observed_statement_dates": [d.isoformat() for d in observed[:ISSUE_LIMIT]],
                         "observed_date_count": len(observed), "requested_date_count": len(requested),
                         "missing_statement_dates": [d.isoformat() for d in missing[:ISSUE_LIMIT]],
                         "missing_statement_date_count": len(missing),
                         "truncated": len(observed) > ISSUE_LIMIT or len(missing) > ISSUE_LIMIT,
                         "observed_dates_are_not_verified_nav_history": True}


def audit_ibkr_account(*, xml_text: str, account_id: str, as_of: date, base_currency: str,
                       internal_state: Mapping[str, Any] | None = None,
                       period_start: date | None = None, history_dates: Sequence[date] = (),
                       expected_sha256: str | None = None,
                       quality: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Audit one full broker account/date/base currency, never personal ownership.

    Internal state must explicitly provide account_id/as_of/base_currency and
    SCOPE. Metric basis tags are documented in docs/BROKER_SYNC_AND_RECONCILIATION.md.
    period_start is inclusive: ChangeInNAV.startingValue is the value immediately
    before that statement period. P&L evidence is not a percentage return audit.
    History remains unverified; this function does not certify a historical series.
    """
    checks = {key: _check(key) for key in ("source", "cash", "positions", "nav", "fx", "period_return", "history_coverage")}
    source = checks["source"]
    content = xml_text.encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    result = {"status": "review", "read_only": True, "executable": False,
              "scope": {"account_id": account_id, "as_of": as_of.isoformat(), "base_currency": base_currency, **SCOPE},
              "report": {"sha256": digest, "publication_verified": False},
              "checks": list(checks.values()), "counts": {}}

    def finish() -> dict[str, Any]:
        for check in checks.values():
            if check["status"] == "ok" and not check["evidence"] and not check["compared"]:
                _issue(check, "scope_unavailable")
        counts = {s: sum(c["status"] == s for c in checks.values()) for s in ("ok", "review", "error")}
        result["counts"] = counts
        result["status"] = "error" if counts["error"] else "review" if counts["review"] else "ok"
        return result

    if not account_id or not _currency(base_currency):
        _issue(source, "invalid_scope")
        return finish()
    if expected_sha256 is not None and digest != expected_sha256:
        _issue(source, "report_hash_mismatch", status="error")
        return finish()
    if len(content) > MAX_XML_BYTES or re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", xml_text, re.IGNORECASE):
        _issue(source, "unsupported_xml", status="error")
        return finish()
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        _issue(source, "invalid_xml", status="error")
        return finish()
    statements = _elements(root, "FlexStatement")
    _history(checks["history_coverage"], statements, history_dates, account_id, base_currency)
    candidates = [s for s in statements if s.get("accountId") == account_id and _day(s.get("toDate")) == as_of]
    if not candidates:
        dates = [d for s in statements if s.get("accountId") == account_id if (d := _day(s.get("toDate"))) is not None]
        reason = "stale_report" if dates and max(dates) < as_of else "missing_account_date"
        _issue(source, reason)
        return finish()
    statement = _one(source, candidates, "FlexStatement_exact_account_date")
    if statement is None:
        return finish()
    info = _elements(statement, "AccountInformation")
    if not info or {r.get("currency") for r in info} != {base_currency}:
        _issue(source, "missing_base_currency" if not info else "base_currency_mismatch")
    _evidence_scope(source, root, statement, account_id)
    if source["issue_count"]:
        return finish()
    comparable = _scope(source, internal_state, account_id, as_of, base_currency)
    if quality is not None and (quality.get("status") != "ok" or quality.get("legacy_unverified")
                                or quality.get("using_previous") or _day(quality.get("statement_as_of")) != as_of
                                or (_day(quality.get("expected_statement_as_of")) or as_of) > as_of):
        _issue(source, "source_unverified")
        comparable = False
    source["evidence"] = {"selected_statement_date": as_of.isoformat(), "internal_comparable": comparable}
    # Parsed magnitudes are bounded to 10**+/-64; products and sums fit these
    # independent exponent/precision bounds without inheriting caller traps.
    with localcontext(Context(prec=160, Emax=512, Emin=-512)):
        cash, native, cash_rates = _cash(checks["cash"], checks["fx"], statement, as_of, base_currency)
        positions, position_rates, position_rows, position_conflicts = _positions(
            checks["positions"], checks["fx"], statement, as_of, base_currency)
        nav = _nav(checks["nav"], statement, as_of, base_currency)
        period = _period(checks["period_return"], statement, period_start, as_of, base_currency, nav,
                         root=root, account_id=account_id)
        rates = dict(cash_rates)
        conflicts = set(position_conflicts)
        for currency, rate in position_rates.items():
            if currency in rates and abs(rates[currency] - rate) > TOLERANCE:
                _issue(checks["fx"], "conflicting_fx", currency=currency)
                conflicts.add(currency)
            rates[currency] = rate
        for currency in sorted(conflicts):
            rates.pop(currency, None)
            _issue(checks["positions"], "conflicting_fx", currency=currency)
            positions = None
            checks["positions"]["evidence"]["position_value_base"] = None
            if currency in cash_rates:
                checks["cash"]["evidence"]["native_fx_complete"] = False
                if checks["cash"]["evidence"]["basis"] == "native_cash_at_explicit_statement_fx":
                    _issue(checks["cash"], "conflicting_fx", currency=currency)
                    cash = None
                    checks["cash"]["evidence"]["cash_base"] = None
        checks["fx"]["evidence"] = {"rates_to_base": {k: _text(v) for k, v in sorted(rates.items())[:ISSUE_LIMIT]},
                                    "currency_count": len(rates), "historical_only": True,
                                    "conflicting_currencies": sorted(conflicts)[:ISSUE_LIMIT],
                                    "conflicting_currency_count": len(conflicts),
                                    "truncated": len(rates) > ISSUE_LIMIT or len(conflicts) > ISSUE_LIMIT}
        if not comparable:
            for key in ("cash", "positions", "nav", "fx", "period_return"):
                _issue(checks[key], "internal_not_comparable")
            return finish()
        assert internal_state is not None
        for key, basis, expected, field, value in (
            ("cash", "cash_basis", "ending_cash", "cash_base", cash),
            ("positions", "position_basis", "signed_market_value", "position_value_base", positions),
            ("nav", "nav_basis", "official_nav_total", "net_liquidation_base", nav),
        ):
            if checks[key]["issue_count"]:
                _issue(checks[key], "official_evidence_incomplete")
            elif _basis(checks[key], internal_state, basis, expected):
                _compare(checks[key], field, value, internal_state.get(field))
        if ("positions" in internal_state and positions is not None
                and internal_state.get("position_basis") == "signed_market_value"):
            _position_details(checks["positions"], position_rows, internal_state["positions"], account_id, as_of)
        if ("cash_by_currency" in internal_state and checks["cash"]["evidence"].get("cash_base") is not None
                and not checks["cash"]["reason_counts"].get("official_evidence_incomplete")
                and _basis(checks["cash"], internal_state, "cash_basis", "ending_cash")):
            actual_cash = internal_state["cash_by_currency"]
            if isinstance(actual_cash, Mapping):
                for currency in sorted(native.keys() | actual_cash.keys(), key=str):
                    if not _currency(currency):
                        _issue(checks["cash"], "invalid_internal_currency", field="cash_by_currency", currency=str(currency))
                    elif currency not in native:
                        _issue(checks["cash"], "currency_not_covered", currency=str(currency))
                    else:
                        _compare(checks["cash"], "cash:" + currency, native[currency], actual_cash.get(currency))
            else:
                _issue(checks["cash"], "invalid_internal_value", field="cash_by_currency")
        if "fx_rates_to_base" in internal_state:
            if _basis(checks["fx"], internal_state, "fx_basis", "statement_date_to_base"):
                actual_rates = internal_state["fx_rates_to_base"]
                if isinstance(actual_rates, Mapping):
                    official_fx_complete = not checks["fx"]["issue_count"]
                    for currency in sorted(rates.keys() | actual_rates.keys(), key=str):
                        if not _currency(currency):
                            _issue(checks["fx"], "invalid_internal_currency", field="fx_rates_to_base", currency=str(currency))
                            continue
                        actual_rate = _number(actual_rates.get(currency))
                        if actual_rate is not None and actual_rate <= 0:
                            _issue(checks["fx"], "invalid_internal_fx", field="fx:" + str(currency), actual=_text(actual_rate))
                        elif actual_rate is not None and currency == base_currency and actual_rate != 1:
                            _issue(checks["fx"], "invalid_internal_base_fx", field="fx:" + str(currency), actual=_text(actual_rate))
                        elif currency not in rates:
                            _issue(checks["fx"], "currency_not_covered", currency=str(currency))
                        elif official_fx_complete:
                            _compare(checks["fx"], "fx:" + str(currency), rates[currency], actual_rates.get(currency))
                else:
                    _issue(checks["fx"], "invalid_internal_value", field="fx_rates_to_base")
        else:
            _issue(checks["fx"], "missing_internal_fx")
        actual_period = internal_state.get("period")
        if isinstance(actual_period, Mapping) and period["pnl_base"] is not None:
            if (_day(actual_period.get("from_date")) != period_start or _day(actual_period.get("to_date")) != as_of):
                _issue(checks["period_return"], "period_mismatch")
            elif any(actual_period.get(field, expected) != expected for field, expected in {
                "account_id": account_id, "base_currency": base_currency, **SCOPE
            }.items()):
                _issue(checks["period_return"], "semantic_mismatch")
            elif _basis(checks["period_return"], actual_period, "basis", "nav_change_less_external_flows"):
                for field, expected_value in period.items():
                    _compare(checks["period_return"], field, expected_value, actual_period.get(field))
        elif period_start is not None:
            _issue(checks["period_return"], "missing_internal_period" if not isinstance(actual_period, Mapping) else "period_evidence_incomplete")
    return finish()
