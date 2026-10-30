"""Read-only daily FX bridge over an already verified, published Flex report.

This is explanatory evidence, never a replacement for NAV or ledger facts.
Native cash retains its sign. Accruals/other NAV components remain a separate
bridge item because their native-currency attribution is not proven here.
"""
from __future__ import annotations

import copy
from collections import OrderedDict
from datetime import date
from decimal import Context, Decimal, localcontext
import hashlib
import re
from threading import RLock
from typing import Any

from domain.brokers.ibkr import ibkr_daily_close as close_parser
from domain.brokers.ibkr import ibkr_daily_close_read as close_read
from domain.investments.daily_fx_bridge import build_daily_fx_bridge

ZERO = Decimal(0)
INCOME = ("dividends", "insuredDepositInterest", "brokerInterest", "bondInterest", "paymentInLieu", "otherIncome")
EXPENSES = ("commissions", "clientFees", "referralFee", "billableCommissions", "brokerFees", "advisorFees",
            "otherFees", "transactionTax", "withholdingTax", "withholding871m", "withholdingCollectedTax",
            "salesTax", "billableSalesTax")
FUNDING = ("depositWithdrawals", "accountTransfers", "linkingAdjustments", "internalTransfers")
PRINCIPAL = ("netTradesSales", "netTradesPurchases")
_PAIRS: OrderedDict[tuple[str, str, date], dict[str, Any]] = OrderedDict()
_PAIR_LOCK = RLock()


def unavailable(reason: str) -> dict[str, Any]:
    return {"status": "pending", "status_label": "待核准", "reasons": [reason],
            "net_fx_pnl_cny": None, "currencies": [], "read_only": True,
            "affects_nav": False, "scope": "positions_and_signed_cash", "as_of": None}


def _currency(value: str) -> str:
    close_parser._require(re.fullmatch(r"[A-Z]{3}", value, re.ASCII) is not None, "invalid_currency")
    return value


def _native_state(evidence, day: date) -> dict[str, Any]:
    statement = close_parser._one(evidence.by_day.get(day, []), "daily_statement")
    base = evidence.base(statement)
    close_parser._require(close_parser._day(statement.get("fromDate")) == day, "not_daily_statement")
    nav = evidence.nav(statement, day, base)
    evidence.holdings(statement, day, base, nav)
    positions: dict[str, Decimal] = {}
    identities = set()
    for row in close_parser._rows(statement, "OpenPosition"):
        currency = _currency(row.get("currency", ""))
        identity = (row.get("conid"), currency)
        close_parser._require(identity[0] and identity not in identities
                              and row.get("levelOfDetail") == "SUMMARY", "ambiguous_positions")
        identities.add(identity)
        value = close_parser._number(row, "positionValue")
        if row.get("marketValue") is not None:
            close_parser._equal(value, close_parser._number(row, "marketValue"), "conflicting_position_value")
        positions[currency] = positions.get(currency, ZERO) + value
    section = close_parser._one(close_parser._rows(statement, "CashReport"), "CashReport")
    evidence.scope(section, statement, base, day, day, leaf_account=False)
    cash, activity, cash_start = {}, {}, {}
    unclassified_fields: set[str] = set()
    summaries = []
    for row in close_parser._rows(section, "CashReportCurrency"):
        evidence.scope(row, statement, base, day, day, currency=False)
        close_parser._require(close_parser._day(row.get("fromDate")) == day
                              and close_parser._day(row.get("toDate")) == day, "cash_date_mismatch")
        currency = row.get("currency", "")
        if currency == "BASE_SUMMARY":
            summaries.append(row)
            continue
        _currency(currency)
        close_parser._require(currency not in cash and row.get("levelOfDetail") == "Currency", "ambiguous_cash")
        cash[currency] = close_parser._number(row, "endingCash")
        cash_start[currency] = close_parser._number(row, "startingCash")
        values = {field: close_parser._number(row, field) for field in (*INCOME, *EXPENSES, *FUNDING, *PRINCIPAL)}
        activity[currency] = {
            "trade_cash": sum((values[f] for f in PRINCIPAL), ZERO),
            "income": sum((max(values[f], ZERO) for f in INCOME), ZERO),
            "expenses": sum((values[f] for f in EXPENSES), ZERO)
                        + sum((min(values[f], ZERO) for f in INCOME), ZERO),
            "funding": sum((values[f] for f in FUNDING), ZERO),
            "other": ZERO,
        }
        metadata = {"accountId", "acctAlias", "model", "currency", "levelOfDetail", "fromDate", "toDate"}
        balances = {"startingCash", "endingCash", "endingSettledCash", "slbNetCash", "slbNetSettledCash",
                    "deposits", "withdrawals"}
        for field in row.attrib:
            if (field in metadata or field in balances or field in values
                    or field.endswith(("Sec", "Com", "MTD", "YTD"))):
                continue
            if close_parser._number(row, field) != 0:
                unclassified_fields.add(field)
    summary = close_parser._one(summaries, "BASE_SUMMARY")
    close_parser._equal(close_parser._number(summary, "endingCash"), nav["cash_base"], "cash_nav_mismatch")
    close_parser._require(bool(cash) and set(positions) <= set(cash), "missing_native_cash_coverage")
    rates = {base: Decimal(1)}
    for row in close_parser._rows(statement, "ConversionRate"):
        if row.get("toCurrency") != base:
            continue
        evidence.scope(row, statement, base, day, day, currency=False, leaf_account=False)
        close_parser._require(close_parser._day(row.get("reportDate")) == day, "conversion_date_mismatch")
        currency = _currency(row.get("fromCurrency", ""))
        rate = close_parser._number(row, "rate")
        close_parser._require(rate > 0, "invalid_fx")
        close_parser._require(currency not in rates or rates[currency] == rate, "conflicting_fx")
        rates[currency] = rate
    close_parser._require(set(cash) <= set(rates), "missing_statement_fx")
    for row in close_parser._rows(statement, "OpenPosition"):
        close_parser._equal(close_parser._number(row, "fxRateToBase"), rates[row.get("currency")],
                            "conflicting_position_fx")
    for currency in cash:
        positions.setdefault(currency, ZERO)
    # Native amounts must reconcile to the official base magnitudes, not merely
    # share a date. Normal report rounding is exposed, never corrected in facts.
    cash_delta = nav["cash_base"] - sum((cash[c] * rates[c] for c in cash), ZERO)
    position_delta = nav["position_value_base"] - sum((positions[c] * rates[c] for c in positions), ZERO)
    trades_section = close_parser._one(close_parser._rows(statement, "Trades"), "Trades")
    evidence.scope(trades_section, statement, base, day, day, leaf_account=False)
    principals = {currency: {field: ZERO for field in PRINCIPAL} for currency in cash}
    trades_supported = True
    trade_ids: set[str] = set()
    for row in close_parser._rows(trades_section, "Trade"):
        evidence.scope(row, statement, base, day, day, currency=False)
        currency = row.get("currency", "")
        identity = row.get("tradeID") or row.get("transactionID")
        if (row.get("assetCategory") not in ("STK", "FUND", "ETF")
                or row.get("levelOfDetail") != "EXECUTION" or currency not in principals
                or not identity or identity in trade_ids
                or close_parser._day(row.get("tradeDate")) != day):
            trades_supported = False
            continue
        trade_ids.add(identity)
        amount = close_parser._number(row, "proceeds")
        side = "netTradesSales" if amount > 0 else "netTradesPurchases"
        principals[currency][side] += amount
    for row in close_parser._rows(section, "CashReportCurrency"):
        currency = row.get("currency", "")
        if currency in principals:
            trades_supported = trades_supported and all(
                abs(close_parser._number(row, field) - principals[currency][field]) <= Decimal("0.00000001")
                for field in PRINCIPAL)
    return {"base": base, "positions": positions, "cash": cash, "cash_start": cash_start,
            "activities": activity, "rates": rates, "nav": nav, "cash_delta": cash_delta,
            "position_delta": position_delta, "trades_supported": trades_supported,
            "unclassified_fields": sorted(unclassified_fields)}


def _parse_report_pair(xml_text: str, account: str, digest: str, as_of: date) -> dict[str, Any]:
    evidence, _, reason = close_parser._document(xml_text, account, digest)
    if evidence is None:
        return unavailable(reason or "invalid_evidence")
    try:
        prior = max(day for day in evidence.by_day if day < as_of)
        with localcontext(Context(prec=160, Emax=512, Emin=-512)):
            statement = close_parser._one(evidence.by_day.get(as_of, []), "daily_statement")
            opening_days = {close_parser._day(row.get("reportDate"))
                            for row in close_parser._rows(statement, "EquitySummaryByReportDateInBase")
                            if close_parser._day(row.get("reportDate")) < as_of}
            close_parser._require(opening_days == {prior}, "opening_valuation_date_gap")
            opening = _native_state(evidence, prior)
            closing = _native_state(evidence, as_of)
            close_parser._require(opening["base"] == closing["base"], "base_currency_changed")
            period = evidence.period(as_of, as_of, closing["base"], closing["nav"]["nav_base"])
            close_parser._equal(period["opening_nav_base"], opening["nav"]["nav_base"], "opening_period_mismatch")
            close_parser._require(set(opening["cash"]) == set(closing["cash"]), "currency_coverage_changed")
            for currency, amount in closing["cash_start"].items():
                close_parser._equal(amount, opening["cash"][currency], "opening_native_cash_mismatch")
            close_parser._require(closing["trades_supported"], "unproven_security_trade_principal")
            return {"status": "ok", "opening_date": prior, "closing_date": as_of,
                    "opening": opening, "closing": closing, "period": period, "source_sha256": digest}
    except (ValueError, KeyError, TypeError) as exc:
        return unavailable(str(exc) if isinstance(exc, close_parser._Unavailable) else "missing_daily_pair")


def _report_pair(xml_text: str, account: str, digest: str, as_of: date) -> dict[str, Any]:
    if (not isinstance(xml_text, str) or len(xml_text) > close_parser.MAX_XML_BYTES
            or hashlib.sha256(xml_text.encode("utf-8")).hexdigest() != digest):
        return unavailable("source_sha256_mismatch")
    key = (digest, account, as_of)
    with _PAIR_LOCK:
        if key not in _PAIRS:
            _PAIRS[key] = _parse_report_pair(xml_text, account, digest, as_of)
        _PAIRS.move_to_end(key)
        while len(_PAIRS) > 2:
            _PAIRS.popitem(last=False)
        return copy.deepcopy(_PAIRS[key])


def build_ibkr_daily_fx(cur, *, evidence: dict[str, Any] | None = None,
                        owner_ratio: Decimal = Decimal(1), requested_as_of: date | None = None) -> dict[str, Any]:
    """Explain the last published day without labeling it as today's account PnL."""
    evidence = evidence if evidence is not None else close_read.load_evidence()
    if evidence.get("status") != "ok":
        return unavailable("missing_published_daily_close")
    try:
        day = evidence["as_of"]
        day = date.fromisoformat(day) if isinstance(day, str) else day
        if type(day) is not date or day > date.today() or (requested_as_of and day > requested_as_of):
            return unavailable("future_daily_close")
        if not owner_ratio.is_finite() or not ZERO < owner_ratio <= 1:
            return unavailable("invalid_owner_ratio")
        pair = _report_pair(evidence["xml_text"], evidence["account_id"], evidence["sha256"], day)
        if pair["status"] != "ok":
            return copy.deepcopy(pair)
        opening, closing = pair["opening"], pair["closing"]
        low, high = pair["opening_date"], pair["closing_date"]
        from domain.investments.valuation_nav import daily_close_account_identity
        identities = {"opening": daily_close_account_identity(cur, source_account_id=evidence["account_id"], as_of=low),
                      "closing": daily_close_account_identity(cur, source_account_id=evidence["account_id"], as_of=high)}
        if any(item.get("status") != "ok" for item in identities.values()):
            return {**unavailable("unproven_account_identity"), "identity_evidence": identities,
                    "as_of": high.isoformat(), "opening_as_of": low.isoformat()}
        base = closing["base"]
        r0 = close_read.historical_fx(cur, currency=base, base_currency="CNY", as_of=low)
        r1 = close_read.historical_fx(cur, currency=base, base_currency="CNY", as_of=high)
        with localcontext(Context(prec=160, Emax=512, Emin=-512)):
            def rates(state, fx, when):
                return {currency: ({"status": "confirmed", "rate": Decimal(1), "source": "currency_identity", "as_of": when}
                                   if currency == "CNY" else
                                   {**fx, "status": "confirmed", "as_of": when,
                                    "rate": rate * fx["rate"], "source": "flex_conversion_and_" + fx["source"]}
                                   if fx.get("status") == "ok" else {**fx, "as_of": None})
                        for currency, rate in state["rates"].items() if currency in state["cash"]}

            known_fx = r0.get("status") == r1.get("status") == "ok"
            nav_change = (closing["nav"]["nav_base"] * r1["rate"] - opening["nav"]["nav_base"] * r0["rate"]
                          if known_fx else None)
            flow = pair["period"]["net_external_flow_base"] * r1["rate"] if known_fx else None
            accrual_change = None
            if known_fx:
                fields = ("interest_accrual_base", "dividend_accrual_base", "other_nav_components_base")
                accrual_change = (sum((closing["nav"][f] for f in fields), ZERO) * r1["rate"]
                                  - sum((opening["nav"][f] for f in fields), ZERO) * r0["rate"])
            official = (nav_change - flow - accrual_change
                        if nav_change is not None and flow is not None and accrual_change is not None else None)
            complete = not closing["unclassified_fields"] and all(
                abs(state[key]) <= Decimal("0.00000001") for state in (opening, closing)
                for key in ("cash_delta", "position_delta"))
            result = build_daily_fx_bridge(opening_date=low, closing_date=high,
                opening_positions=opening["positions"], closing_positions=closing["positions"],
                opening_cash=opening["cash"], closing_cash=closing["cash"],
                fx_opening=rates(opening, r0, low), fx_closing=rates(closing, r1, high),
                activities=closing["activities"], official_pnl_cny=official, evidence_complete=complete)
            for row in result["currencies"]:
                for field in ("opening_fx", "closing_fx"):
                    row[field]["source_label"] = ("人民币恒等汇率" if row["currency"] == "CNY" else
                                                  "IBKR日结与内部历史汇率" if row[field]["rate"] is not None else "来源待核准")
            result.update({"account_code": "ibkr", "as_of": high.isoformat(), "opening_as_of": low.isoformat(),
                "requested_as_of": (requested_as_of or date.today()).isoformat(),
                "read_only": True, "affects_nav": False, "source_sha256": evidence["sha256"],
                "identity_evidence": identities,
                "scope": "positions_and_signed_cash", "ownership_basis": "full_account",
                "accrual_other_change_cny": accrual_change,
                "official_nav_change_cny": nav_change, "net_external_flow_cny": flow,
                "official_total_pnl_cny": nav_change - flow if nav_change is not None and flow is not None else None,
                "fx_basis": "same_day_statement_cross_fx_and_exact_internal_base_fx",
                "unclassified_cash_fields": closing["unclassified_fields"],
                "accrual_fx_status": "not_separately_attributed", "owner_ratio": owner_ratio,
                "source_rounding_base": {"opening_cash": opening["cash_delta"],
                                         "closing_cash": closing["cash_delta"],
                                         "opening_positions": opening["position_delta"],
                                         "closing_positions": closing["position_delta"]}})
            # Ownership is a read-side scale only; never infer capital from NAV.
            if owner_ratio != 1:
                for key, value in list(result.items()):
                    if key.endswith("_cny") and isinstance(value, Decimal):
                        result[key] = value * owner_ratio
                for row in result.get("currencies", []):
                    for key, value in list(row.items()):
                        if (key.endswith("_cny") or key in ("opening_positions", "closing_positions",
                            "opening_cash", "closing_cash", "unexplained_native")) and isinstance(value, Decimal):
                            row[key] = value * owner_ratio
                    row["activities"] = {key: value * owner_ratio for key, value in row["activities"].items()}
                for group in ("summary", "known_partial"):
                    result[group] = {key: value * owner_ratio if isinstance(value, Decimal) else value
                                     for key, value in result[group].items()}
                result["ownership_basis"] = "owner_adjusted"
            result["net_fx_pnl_cny"] = result["summary"]["net_fx_pnl_cny"]
            result["status_label"] = {"confirmed": "已核对", "partial": "部分可核对", "pending": "待核准"}.get(
                result["status"], "待核准")
            return result
    except (ValueError, KeyError, TypeError, ArithmeticError):
        return unavailable("daily_fx_evidence_invalid")
