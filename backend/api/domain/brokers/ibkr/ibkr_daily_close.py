"""Pure, fail-closed Flex daily NAV evidence, independent of audit/performance.

Requires a full EquitySummaryInBase component schema, not a selected-column
report. Dates in the public result are ISO strings; all amounts are Decimal.
period_start is inclusive. A requested but unproven period makes the entire
result unavailable. No file, DB, network, cache, or fallback calculation occurs.
"""
from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from decimal import Context, Decimal, InvalidOperation, localcontext
from typing import Any

MAX_XML_BYTES = 32 * 1024 * 1024
TOLERANCE = Decimal("0.00000001")
POSITION_FIELDS = ("stock", "options", "bonds", "commodities", "notes", "funds", "crypto")
OTHER_FIELDS = (
    "slbCashCollateral", "ipoSubscription", "slbDirectSecuritiesBorrowed", "slbDirectSecuritiesLent",
    "liteSurchargeAccruals", "cgtWithholdingAccruals", "incentiveCouponAccruals",
    "eventContractInterestAccruals", "marginFinancingChargeAccruals", "softDollars",
    "forexCfdUnrealizedPl", "cfdUnrealizedPl", "physDel",
)
COMPONENT_FIELDS = ("cash", *POSITION_FIELDS, "interestAccruals", "dividendAccruals", *OTHER_FIELDS)
SUBCOMPONENTS = {
    "cash": ("brokerCashComponent", "fdicInsuredBankSweepAccountCashComponent",
             "insuredBankDepositRedemptionCashComponent"),
    "interestAccruals": ("brokerInterestAccrualsComponent", "fdicInsuredAccountInterestAccrualsComponent",
                         "bondInterestAccrualsComponent", "brokerFeesAccrualsComponent"),
}
FLOW_FIELDS = ("depositsWithdrawals", "internalCashTransfers", "assetTransfers",
               "linkingAdjustments", "debitCardActivity", "billPay")
# These newer bridge categories need an explicit ownership interpretation before
# being classified as external capital. A nonzero value cannot become P&L.
UNSUPPORTED_FLOWS = ("paxosTransfers", "excessFundSweep", "grantActivity", "donations", "carbonCredits")
_ZERO = Decimal(0)


class _Unavailable(ValueError):
    pass


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise _Unavailable(reason)


def _tag(node: ET.Element) -> str:
    return node.tag.rsplit("}", 1)[-1]


def _rows(node: ET.Element, tag: str) -> list[ET.Element]:
    return [child for child in node.iter() if _tag(child) == tag]


def _day(value: str | None) -> date:
    try:
        _require(isinstance(value, str), "missing_date")
        assert value is not None
        if re.fullmatch(r"[0-9]{8}", value):
            return datetime.strptime(value, "%Y%m%d").date()
        _require(re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is not None, "invalid_date")
        return date.fromisoformat(value)
    except ValueError as exc:
        if isinstance(exc, _Unavailable):
            raise
        raise _Unavailable("invalid_date") from exc


def _number(row: ET.Element, field: str) -> Decimal:
    text = row.get(field)
    _require(text not in (None, ""), "missing_amount:" + field)
    assert text is not None
    text = text.strip()
    _require(len(text) <= 64 and re.fullmatch(
        r"[+-]?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?", text
    ) is not None, "invalid_amount:" + field)
    try:
        value = Decimal(text.replace(",", ""))
    except InvalidOperation as exc:
        raise _Unavailable("invalid_amount:" + field) from exc
    _require(value.is_finite() and abs(value.adjusted()) <= 64, "invalid_amount:" + field)
    return value


def _equal(left: Decimal, right: Decimal, reason: str) -> None:
    _require(abs(left - right) <= TOLERANCE, reason)


def _one(rows: list[ET.Element], section: str) -> ET.Element:
    _require(len(rows) == 1, ("missing_evidence:" if not rows else "ambiguous_evidence:") + section)
    return rows[0]


class _Evidence:
    def __init__(self, root: ET.Element, account: str):
        self.account = account
        self.parents = {child: parent for parent in root.iter() for child in parent}
        self.statements = [s for s in _rows(root, "FlexStatement") if s.get("accountId") == account]
        _require(bool(self.statements), "missing_account_statement")
        self.by_day: dict[date, list[ET.Element]] = {}
        for statement in self.statements:
            self.by_day.setdefault(_day(statement.get("toDate")), []).append(statement)

    def scope(self, row: ET.Element, statement: ET.Element, base: str, start: date, end: date,
              *, comparison: bool = False, currency: bool = True, leaf_account: bool = True) -> None:
        if leaf_account:
            _require(row.get("accountId") == self.account, "missing_or_conflicting_leaf_account")
        node: ET.Element | None = row
        while node is not None:
            _require(node.get("accountId") in (None, "", self.account) and not node.get("model"),
                     "conflicting_account_or_model_scope")
            _require(node.get("baseCurrency") in (None, "", base), "conflicting_base_currency")
            if currency or node is not row:
                _require(node.get("currency") in (None, "", base), "conflicting_base_currency")
            if node.get("reportDate") is not None:
                _require(start == end == _day(node.get("reportDate")), "conflicting_report_date")
            first, last = node.get("fromDate"), node.get("toDate")
            if first is not None or last is not None:
                low, high = _day(first), _day(last)
                _require(low <= high, "invalid_scope_range")
                # IBKR's prior-period comparison row intentionally predates its
                # FlexStatement. Other dated ancestors still constrain the row.
                if not (comparison and node is statement):
                    _require(low <= start <= end <= high, "conflicting_date_scope")
            node = self.parents.get(node)

    def base(self, statement: ET.Element) -> str:
        row = _one(_rows(statement, "AccountInformation"), "AccountInformation")
        base = row.get("currency", "")
        _require(re.fullmatch(r"[A-Z]{3}", base, flags=re.ASCII) is not None, "invalid_base_currency")
        first, last = _day(statement.get("fromDate")), _day(statement.get("toDate"))
        self.scope(row, statement, base, first, last)
        return base

    def nav(self, statement: ET.Element, day: date, base: str, *, comparison: bool = False) -> dict[str, Any]:
        rows = _rows(statement, "EquitySummaryByReportDateInBase")
        # Malformed dates cannot be discarded as if they were merely prior rows.
        selected = [r for r in rows if _day(r.get("reportDate")) == day]
        row = _one(selected, "current_nav" if not comparison else "opening_nav")
        _require(row.get("currency") == base, "missing_or_conflicting_nav_currency")
        self.scope(row, statement, base, day, day, comparison=comparison)
        components = {field: _number(row, field) for field in COMPONENT_FIELDS}
        total = _number(row, "total")
        known = {*COMPONENT_FIELDS, "total", *(f for group in SUBCOMPONENTS.values() for f in group)}
        metadata = {"accountId", "acctAlias", "model", "currency", "reportDate", "baseCurrency"}
        _require(all(key in metadata or key in known or (
            key.endswith(("Long", "Short")) and key[:-4 if key.endswith("Long") else -5] in known
        ) for key in row.attrib), "unsupported_nav_field")
        checks: dict[str, Any] = {}
        for field in known:
            if field + "Long" in row.attrib or field + "Short" in row.attrib:
                _require(field in row.attrib, "missing_amount:" + field)
            if field not in row.attrib:
                continue
            value = _number(row, field)
            long_key, short_key = field + "Long", field + "Short"
            if long_key in row.attrib or short_key in row.attrib:
                long_value, short_value = _number(row, long_key), _number(row, short_key)
                _require(long_value >= 0 and short_value <= 0, "invalid_long_short_sign:" + field)
                _equal(long_value + short_value, value, "long_short_component_mismatch:" + field)
                checks[field + "_long_short"] = {"long": long_value, "short": short_value, "sum": value}
        for field, children in SUBCOMPONENTS.items():
            if any(child in row.attrib for child in children):
                parts = {child: _number(row, child) for child in children}
                _equal(sum(parts.values(), _ZERO), components[field], "subcomponent_mismatch:" + field)
                checks[field] = parts
        component_sum = sum(components.values(), _ZERO)
        _equal(component_sum, total, "nav_component_sum_mismatch")
        return {
            "nav_base": total, "cash_base": components["cash"],
            "position_value_base": sum((components[f] for f in POSITION_FIELDS), _ZERO),
            "interest_accrual_base": components["interestAccruals"],
            "dividend_accrual_base": components["dividendAccruals"],
            "other_nav_components_base": sum((components[f] for f in OTHER_FIELDS), _ZERO),
            "component_proof": {"components": components, "sum_base": component_sum,
                                "reported_nav_base": total, "residual_base": total - component_sum,
                                "tolerance": TOLERANCE, "subcomponent_checks": checks},
        }

    def holdings(self, statement: ET.Element, day: date, base: str, nav: dict[str, Any]) -> None:
        section = _one(_rows(statement, "OpenPositions"), "OpenPositions")
        # Empty sections inherit their account and date from the statement.
        self.scope(section, statement, base, day, day, leaf_account=False)
        for row in _rows(section, "OpenPosition"):
            self.scope(row, statement, base, day, day, currency=False)
            _require(_day(row.get("reportDate")) == day, "off_scope_position_date")
        if not _rows(section, "OpenPosition"):
            fields = (*POSITION_FIELDS, "ipoSubscription", "slbDirectSecuritiesBorrowed",
                      "slbDirectSecuritiesLent", "forexCfdUnrealizedPl", "cfdUnrealizedPl", "physDel")
            parts = nav["component_proof"]["components"]
            checks = nav["component_proof"]["subcomponent_checks"]
            for field in fields:
                _require(parts[field] == 0, "empty_holdings_without_explicit_zero")
                proof = checks.get(field + "_long_short")
                _require(proof is not None and proof["long"] == proof["short"] == 0,
                         "empty_holdings_without_gross_zero_proof")

    def opening(self, statement: ET.Element, start: date, base: str) -> dict[str, Any]:
        prior = start - timedelta(days=1)
        earliest = prior
        while earliest.weekday() >= 5:
            earliest -= timedelta(days=1)
        candidates = sorted({_day(r.get("reportDate")) for r in _rows(statement, "EquitySummaryByReportDateInBase")
                             if earliest <= _day(r.get("reportDate")) <= prior})
        _require(bool(candidates), "missing_independent_opening_nav")
        return self.nav(statement, candidates[-1], base, comparison=True)

    def bridge(self, row: ET.Element, statement: ET.Element, base: str, start: date, end: date) -> dict[str, Decimal]:
        _require(row.get("currency") == base, "missing_or_conflicting_bridge_currency")
        self.scope(row, statement, base, start, end)
        result = {"opening": _number(row, "startingValue"), "closing": _number(row, "endingValue")}
        flows = {field: _number(row, field) for field in FLOW_FIELDS}
        for field in UNSUPPORTED_FLOWS:
            if field in row.attrib:
                _require(_number(row, field) == 0, "unsupported_external_flow:" + field)
        for field in row.attrib:
            if field == "transferredPnlAdjustments":
                _number(row, field)  # A named P&L adjustment, not external capital.
            elif field not in (*FLOW_FIELDS, *UNSUPPORTED_FLOWS) and any(
                part in field.lower() for part in ("transfer", "linking", "deposit", "withdraw", "capital")
            ):
                _require(_number(row, field) == 0, "unsupported_external_flow:" + field)
        result["flow"] = sum(flows.values(), _ZERO)
        return result

    def period(self, start: date, end: date, base: str, closing: Decimal) -> dict[str, Any]:
        entries = []
        for statement in self.statements:
            first, last = _day(statement.get("fromDate")), _day(statement.get("toDate"))
            if last < start or first > end:
                continue
            _require(self.base(statement) == base, "period_base_currency_changed")
            for row in _rows(statement, "ChangeInNAV"):
                low, high = _day(row.get("fromDate")), _day(row.get("toDate"))
                _require(low <= high, "invalid_bridge_range")
                if low <= end and high >= start:
                    entries.append((low, high, row, statement))
        exact = [entry for entry in entries if entry[:2] == (start, end)]
        _require(len(exact) <= 1, "ambiguous_exact_period_bridge")
        if exact:
            low, high, row, statement = exact[0]
            bridge = self.bridge(row, statement, base, low, high)
            opening = self.opening(statement, start, base)["nav_base"]
            independent_close = self.nav(statement, end, base)["nav_base"]
            _equal(bridge["opening"], opening, "opening_nav_bridge_mismatch")
            _equal(bridge["closing"], independent_close, "closing_nav_bridge_mismatch")
            flow = bridge["flow"]
            coverage = "exact_change_in_nav"
            count = 1
        else:
            _require(bool(entries), "missing_period_bridge")
            _require(all(low == high and start <= low <= end for low, high, _, _ in entries),
                     "non_daily_or_partial_period_bridge")
            entries.sort(key=lambda entry: entry[0])
            dates = [entry[0] for entry in entries]
            _require(len(set(dates)) == len(dates), "ambiguous_daily_period_bridge")
            # No inferred holiday calendar: absent weekdays are unavailable,
            # even when NAV happens not to change over the omitted day.
            day = start
            available = set(dates)
            while day <= end:
                _require(day.weekday() >= 5 or day in available, "missing_operational_day")
                if day == end:
                    break
                day += timedelta(days=1)
            _require(dates[-1] == end, "missing_period_closing_day")
            opening = self.opening(entries[0][3], start, base)["nav_base"]
            previous, flow = opening, _ZERO
            previous_components = None
            for low, high, row, statement in entries:
                _require(_day(statement.get("toDate")) == high, "daily_bridge_not_statement_close")
                bridge = self.bridge(row, statement, base, low, high)
                current = self.nav(statement, high, base)
                prior = self.opening(statement, low, base)
                _equal(prior["nav_base"], previous, "inconsistent_daily_nav_endpoint")
                if previous_components is not None:
                    _require(prior["component_proof"]["components"] == previous_components,
                             "inconsistent_daily_nav_components")
                _equal(bridge["opening"], previous, "opening_nav_bridge_mismatch")
                _equal(bridge["closing"], current["nav_base"], "closing_nav_bridge_mismatch")
                previous = current["nav_base"]
                previous_components = current["component_proof"]["components"]
                flow += bridge["flow"]
            independent_close = previous
            coverage, count = "complete_daily_change_in_nav", len(entries)
        _equal(independent_close, closing, "period_daily_close_mismatch")
        return {"from_date": start.isoformat(), "to_date": end.isoformat(),
                "opening_nav_base": opening, "closing_nav_base": closing,
                "net_external_flow_base": flow, "pnl_base": closing - opening - flow,
                "basis": "nav_change_less_external_flows", "evidence_basis": coverage,
                "bridge_count": count}


def _document(xml_text: str, account_id: str, expected_sha256: str | None
              ) -> tuple[_Evidence | None, str | None, str | None]:
    digest = None
    try:
        _require(isinstance(xml_text, str), "invalid_xml_input")
        _require(len(xml_text) <= MAX_XML_BYTES, "xml_size_limit")
        content = xml_text.encode("utf-8")
        _require(len(content) <= MAX_XML_BYTES, "xml_size_limit")
        digest = hashlib.sha256(content).hexdigest()
        _require(expected_sha256 is None or (
            isinstance(expected_sha256, str) and re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256) is not None
            and digest == expected_sha256.lower()), "source_sha256_mismatch")
        _require(re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", xml_text, re.IGNORECASE) is None,
                 "unsafe_xml_declaration")
        _require(isinstance(account_id, str) and bool(account_id) and account_id == account_id.strip(),
                 "invalid_account_id")
        root = ET.fromstring(xml_text)
        stack = [(root, 0)]
        while stack:
            node, depth = stack.pop()
            _require(depth <= 64, "xml_depth_limit")
            stack.extend((child, depth + 1) for child in node)
        return _Evidence(root, account_id), digest, None
    except _Unavailable as exc:
        reason = str(exc)
    except (ET.ParseError, UnicodeError, ValueError):
        reason = "malformed_xml"
    return None, digest, reason


def _daily_result(evidence: _Evidence, digest: str | None, as_of: date,
                  period_start: date | None = None) -> dict[str, Any]:
    try:
        _require(type(as_of) is date and as_of > date.min, "invalid_as_of")
        _require(period_start is None or (type(period_start) is date and date.min < period_start <= as_of),
                 "invalid_period_start")
        with localcontext(Context(prec=160, Emax=512, Emin=-512)):
            statement = _one(evidence.by_day.get(as_of, []), "daily_statement")
            base = evidence.base(statement)
            result = evidence.nav(statement, as_of, base)
            evidence.holdings(statement, as_of, base, result)
            if period_start is not None:
                result["period"] = evidence.period(period_start, as_of, base, result["nav_base"])
            return {"status": "ok", "reasons": [], "account_id": evidence.account, "as_of": as_of.isoformat(),
                    "base_currency": base, "source_sha256": digest, **result}
    except _Unavailable as exc:
        reason = str(exc)
    return {"status": "unavailable", "reasons": [reason], "source_sha256": digest}


def parse_daily_close_evidence(xml_text: str, *, account_id: str, as_of: date,
                               period_start: date | None = None,
                               expected_sha256: str | None = None) -> dict[str, Any]:
    """Return proven daily/optional period values, or unavailable with reasons.

    The digest is over the exact supplied text encoded as UTF-8, including a
    supplied BOM. Unavailable results deliberately contain no monetary values.
    """
    evidence, digest, reason = _document(xml_text, account_id, expected_sha256)
    if evidence is None:
        return {"status": "unavailable", "reasons": [reason], "source_sha256": digest}
    return _daily_result(evidence, digest, as_of, period_start)


def parse_daily_close_series(xml_text: str, *, account_id: str,
                             expected_sha256: str | None = None) -> dict[str, Any]:
    """Parse XML once; map statement closing dates to strict daily results.

    No dates are interpolated or omitted on failure. A malformed document or
    missing account has no daily_closes; any rejected day makes the batch
    unavailable while retaining individual date results for diagnostics.
    """
    evidence, digest, reason = _document(xml_text, account_id, expected_sha256)
    if evidence is None:
        return {"status": "unavailable", "reasons": [reason], "source_sha256": digest, "daily_closes": {}}
    results = {day.isoformat(): _daily_result(evidence, digest, day) for day in sorted(evidence.by_day)}
    complete = all(result["status"] == "ok" for result in results.values())
    return {"status": "ok" if complete else "unavailable", "reasons": [] if complete else ["unavailable_daily_close"],
            "source_sha256": digest, "daily_closes": results}
