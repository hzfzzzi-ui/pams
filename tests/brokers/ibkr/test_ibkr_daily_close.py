"""Generalized synthetic Flex evidence. No private report fixtures/identities."""
import hashlib
import sys
import xml.etree.ElementTree as ET
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_UP, Subnormal, Underflow, localcontext
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "backend/api"))
from domain.brokers.ibkr import ibkr_daily_close as close

ACCOUNT = "SYNTHETIC_ACCOUNT"
DAY = date(2024, 4, 8)
START = date(2024, 4, 1)
D = Decimal


def compact(day):
    return day.strftime("%Y%m%d")


def nav_row(parent, day, *, cash="-20", stock="100", interest="-3", dividend="2", **extra):
    fields = {key: "0" for key in close.COMPONENT_FIELDS}
    fields.update(cash=cash, stock=stock, interestAccruals=interest, dividendAccruals=dividend, **extra)
    total = sum(map(D, fields.values()), D(0))
    fields["total"] = str(total)
    for key, value in list(fields.items()):
        amount = D(value)
        fields[key + "Long"] = str(max(amount, D(0)))
        fields[key + "Short"] = str(min(amount, D(0)))
    return ET.SubElement(parent, "EquitySummaryByReportDateInBase", accountId=ACCOUNT,
                         currency="USD", reportDate=compact(day), **fields)


def statement(day=DAY, start=START, *, zero=False, opening="50", **amounts):
    root = ET.Element("FlexStatement", accountId=ACCOUNT, fromDate=compact(start), toDate=compact(day))
    ET.SubElement(root, "AccountInformation", accountId=ACCOUNT, currency="USD")
    equity = ET.SubElement(root, "EquitySummaryInBase")
    prior = start - timedelta(days=1)
    while prior.weekday() >= 5:
        prior -= timedelta(days=1)
    nav_row(equity, prior, cash=opening, stock="0", interest="0", dividend="0")
    if zero:
        amounts.update(cash="0", stock="0", interest="0", dividend="0")
    current = nav_row(equity, day, **amounts)
    holdings = ET.SubElement(root, "OpenPositions")
    if not zero:
        ET.SubElement(holdings, "OpenPosition", accountId=ACCOUNT, reportDate=compact(day),
                      currency="EUR", conid="synthetic_contract", levelOfDetail="SUMMARY")
    fields = {key: "0" for key in close.FLOW_FIELDS}
    fields.update(startingValue=opening, endingValue=current.get("total"), depositsWithdrawals="20")
    ET.SubElement(root, "ChangeInNAV", accountId=ACCOUNT, currency="USD",
                  fromDate=compact(start), toDate=compact(day), **fields)
    return root


def daily_sequence(start=START, end=DAY, *, initial_zero_days=0):
    root = ET.Element("FlexQueryResponse")
    container = ET.SubElement(root, "FlexStatements")
    day, balance, previous = start, D(0), None
    index = 0
    while day <= end:
        if day.weekday() < 5:
            zero = index < initial_zero_days
            next_balance = D(0) if zero else balance + 3
            row = statement(day, day, zero=zero, opening=str(balance), cash=str(next_balance),
                            stock="0", interest="0", dividend="0")
            bridge = row.find("ChangeInNAV")
            bridge.set("depositsWithdrawals", "0" if zero else "1")
            if previous is not None:
                equity = row.find("EquitySummaryInBase")
                equity.remove(equity[0])
                equity.insert(0, deepcopy(previous))
            previous = deepcopy(list(row.iter("EquitySummaryByReportDateInBase"))[-1])
            container.append(row)
            balance = next_balance
            index += 1
        day += timedelta(days=1)
    return root


def run(root=None, *, day=DAY, start=None, **kwargs):
    text = ET.tostring(root if root is not None else statement(), encoding="unicode")
    return close.parse_daily_close_evidence(text, account_id=ACCOUNT, as_of=day, period_start=start, **kwargs)


def current(root):
    return list(root.iter("EquitySummaryByReportDateInBase"))[-1]


def unavailable(result, reason):
    assert result["status"] == "unavailable", result
    assert reason in result["reasons"]
    assert "nav_base" not in result and "period" not in result


def test_authoritative_nav_negative_cash_accruals_and_exact_period():
    result = run(start=START)
    assert result["status"] == "ok" and result["reasons"] == []
    assert result["account_id"] == ACCOUNT and result["as_of"] == DAY.isoformat()
    assert result["base_currency"] == "USD"
    assert result["nav_base"] == D(79)
    assert result["cash_base"] == D(-20)
    assert result["position_value_base"] == D(100)
    assert result["interest_accrual_base"] == D(-3)
    assert result["dividend_accrual_base"] == D(2)
    assert result["other_nav_components_base"] == D(0)
    assert result["component_proof"]["sum_base"] == result["nav_base"]
    assert result["component_proof"]["residual_base"] == 0
    period = result["period"]
    assert period["from_date"] == START.isoformat() and period["to_date"] == DAY.isoformat()
    assert period["opening_nav_base"] == 50 and period["closing_nav_base"] == 79
    assert period["net_external_flow_base"] == 20 and period["pnl_base"] == 9
    assert period["basis"] == "nav_change_less_external_flows"
    assert period["evidence_basis"] == "exact_change_in_nav"
    assert all(isinstance(result[key], D) for key in (
        "nav_base", "cash_base", "position_value_base", "interest_accrual_base",
        "dividend_accrual_base", "other_nav_components_base"))


def test_optional_period_is_not_invented_and_input_is_not_mutated():
    root = statement()
    before = ET.tostring(root)
    assert "period" not in run(root)
    assert ET.tostring(root) == before


@pytest.mark.parametrize("reverse", [False, True])
def test_current_row_not_prior_row_regardless_of_document_order(reverse):
    root = statement()
    equity = root.find("EquitySummaryInBase")
    if reverse:
        equity[:] = list(reversed(equity))
    assert run(root)["nav_base"] == 79


@pytest.mark.parametrize("field", close.COMPONENT_FIELDS + ("total",))
def test_full_component_schema_required_even_for_zero_fields(field):
    root = statement()
    current(root).attrib.pop(field)
    unavailable(run(root), "missing_amount:" + field)


@pytest.mark.parametrize("value", ["", "NaN", "Infinity", "-Infinity", "1,23", "1_000", "--3", "1e999", "9" * 65])
def test_strict_finite_decimal_not_missing_to_zero(value):
    root = statement()
    current(root).set("cash", value)
    reason = "missing_amount:cash" if not value else "invalid_amount:cash"
    unavailable(run(root), reason)


def test_correctly_grouped_and_scientific_decimal_are_supported():
    root = statement(cash="1000")
    current(root).set("cash", "1,000")
    current(root).set("stock", "1e2")
    assert run(root)["nav_base"] == 1099


@pytest.mark.parametrize("tag", ["FlexStatement", "AccountInformation", "EquitySummaryInBase", "OpenPositions"])
@pytest.mark.parametrize("field,value", [("accountId", "OTHER_ACCOUNT"), ("model", "PARTITION"),
                                         ("baseCurrency", "EUR")])
def test_leaf_and_ancestor_scope_conflicts(tag, field, value):
    root = statement()
    next(root.iter(tag)).set(field, value)
    assert run(root)["status"] == "unavailable"


@pytest.mark.parametrize("tag", ["AccountInformation", "EquitySummaryByReportDateInBase", "OpenPosition", "ChangeInNAV"])
def test_leaf_account_must_be_explicit(tag):
    root = statement()
    row = current(root) if tag == "EquitySummaryByReportDateInBase" else next(root.iter(tag))
    row.attrib.pop("accountId")
    unavailable(run(root, start=START), "missing_or_conflicting_leaf_account")


@pytest.mark.parametrize("attribute,value", [("reportDate", "20240407"), ("fromDate", "20240409"),
                                            ("toDate", "20240407")])
def test_ancestor_date_scope_cannot_be_overridden_by_leaf(attribute, value):
    root = statement()
    root.find("EquitySummaryInBase").set(attribute, value)
    assert run(root)["status"] == "unavailable"


def test_nav_currency_must_match_account_base():
    root = statement()
    current(root).set("currency", "EUR")
    unavailable(run(root), "missing_or_conflicting_nav_currency")


def test_foreign_account_and_other_day_reports_do_not_contaminate_close():
    root = ET.Element("FlexQueryResponse")
    foreign = statement()
    foreign.set("accountId", "OTHER_ACCOUNT")
    root.extend([foreign, statement(DAY - timedelta(days=3)), statement()])
    assert run(root)["nav_base"] == 79


@pytest.mark.parametrize("section", ["AccountInformation", "OpenPositions", "EquitySummaryByReportDateInBase"])
def test_duplicate_selected_evidence_is_ambiguous(section):
    root = statement()
    row = current(root) if section == "EquitySummaryByReportDateInBase" else next(root.iter(section))
    root.append(deepcopy(row))
    assert run(root)["status"] == "unavailable"


def test_duplicate_current_statement_even_if_equal_is_ambiguous():
    root = ET.Element("FlexStatements")
    root.extend([statement(), statement()])
    unavailable(run(root), "ambiguous_evidence:daily_statement")


def test_nav_sum_failure_cannot_be_replaced_by_cash_plus_positions():
    root = statement()
    row = current(root)
    row.set("total", "80")
    row.set("totalLong", "103")
    row.set("totalShort", "-23")
    unavailable(run(root), "nav_component_sum_mismatch")


def test_unknown_nav_component_does_not_disappear_even_if_zero():
    root = statement()
    current(root).set("unrecognizedNewAccrual", "0")
    unavailable(run(root), "unsupported_nav_field")


def test_nested_cash_and_interest_components_and_long_short_are_not_added_twice():
    root = statement()
    row = current(root)
    for parent, fields in close.SUBCOMPONENTS.items():
        for index, field in enumerate(fields):
            row.set(field, row.get(parent) if index == 0 else "0")
    result = run(root)
    assert result["status"] == "ok" and result["nav_base"] == 79
    assert result["component_proof"]["subcomponent_checks"]["cash"]["brokerCashComponent"] == -20


@pytest.mark.parametrize("parent", ["cash", "interestAccruals"])
def test_incomplete_or_inconsistent_subcomponent_group_is_not_accepted(parent):
    root = statement()
    fields = close.SUBCOMPONENTS[parent]
    current(root).set(fields[0], "0")
    unavailable(run(root), "missing_amount:" + fields[1])
    for field in fields[1:]:
        current(root).set(field, "0")
    unavailable(run(root), "subcomponent_mismatch:" + parent)


def test_long_short_must_prove_parent():
    root = statement()
    current(root).set("stockShort", "-1")
    unavailable(run(root), "long_short_component_mismatch:stock")


def test_other_nav_components_are_preserved_without_misclassifying_cash_or_stock():
    root = statement(slbCashCollateral="7", slbDirectSecuritiesLent="-5", liteSurchargeAccruals="-1")
    result = run(root)
    assert result["status"] == "ok" and result["other_nav_components_base"] == 1
    assert result["nav_base"] == 80 and result["position_value_base"] == 100


def test_accrual_reversal_does_not_recharge_settled_interest():
    root = statement(cash="-23", interest="0")
    ET.SubElement(root, "InterestAccrualsCurrency", accountId=ACCOUNT, currency="BASE_SUMMARY",
                  fromDate=compact(DAY), toDate=compact(DAY), startingAccrualBalance="-3",
                  interestAccrued="0", accrualReversal="3", endingAccrualBalance="0")
    result = run(root)
    assert result["cash_base"] == -23 and result["interest_accrual_base"] == 0 and result["nav_base"] == 79


def test_genuine_zero_with_empty_holdings_is_available():
    result = run(statement(zero=True, opening="0"))
    assert result["status"] == "ok" and result["nav_base"] == 0


def test_empty_holdings_not_implied_by_missing_section():
    root = statement(zero=True)
    root.remove(root.find("OpenPositions"))
    unavailable(run(root), "missing_evidence:OpenPositions")


def test_empty_holdings_cannot_hide_nonzero_or_net_zero_gross_positions():
    root = statement()
    root.find("OpenPositions").clear()
    unavailable(run(root), "empty_holdings_without_explicit_zero")
    root = statement(zero=True)
    current(root).set("stockLong", "10")
    current(root).set("stockShort", "-10")
    unavailable(run(root), "empty_holdings_without_gross_zero_proof")


def test_signed_cash_asset_transfers_and_withdrawals_are_external_capital():
    root = statement()
    bridge = root.find("ChangeInNAV")
    bridge.set("depositsWithdrawals", "-7")
    bridge.set("internalCashTransfers", "4")
    bridge.set("assetTransfers", "12")
    result = run(root, start=START)
    assert result["period"]["net_external_flow_base"] == 9
    assert result["period"]["pnl_base"] == 20


@pytest.mark.parametrize("field", close.FLOW_FIELDS + ("startingValue", "endingValue"))
def test_period_requires_every_signed_flow_and_endpoint(field):
    root = statement()
    root.find("ChangeInNAV").attrib.pop(field)
    unavailable(run(root, start=START), "missing_amount:" + field)


def test_ytd_cash_report_not_a_period_bridge():
    root = statement()
    root.remove(root.find("ChangeInNAV"))
    ET.SubElement(root, "CashReportCurrency", depositsYTD="20", endingCash="-20", currency="BASE_SUMMARY")
    unavailable(run(root, start=START), "missing_period_bridge")


@pytest.mark.parametrize("field", ["startingValue", "endingValue"])
def test_period_endpoints_have_independent_nav_proof(field):
    root = statement()
    root.find("ChangeInNAV").set(field, "999")
    reason = "opening_nav_bridge_mismatch" if field == "startingValue" else "closing_nav_bridge_mismatch"
    unavailable(run(root, start=START), reason)


def test_missing_opening_nav_is_not_an_assumed_zero():
    root = statement()
    equity = root.find("EquitySummaryInBase")
    equity.remove(equity[0])
    unavailable(run(root, start=START), "missing_independent_opening_nav")


def test_unclassified_nonzero_capital_category_cannot_silently_become_pnl():
    root = statement()
    root.find("ChangeInNAV").set("paxosTransfers", "1")
    unavailable(run(root, start=START), "unsupported_external_flow:paxosTransfers")


@pytest.mark.parametrize("field", ["newAssetTransfer", "newLinkingAdjustment", "newDeposits",
                                   "withdrawalCorrection", "mysteryCAPITAL"])
@pytest.mark.parametrize("amount", ["1", "-1"])
@pytest.mark.parametrize("daily", [False, True])
def test_unknown_external_flow_attributes_cannot_silently_become_profit(field, amount, daily):
    root = daily_sequence() if daily else statement()
    next(root.iter("ChangeInNAV")).set(field, amount)
    unavailable(run(root, start=START), "unsupported_external_flow:" + field)


@pytest.mark.parametrize("amount", ["", "NaN", "Infinity", "invalid"])
def test_unknown_capital_attribute_requires_an_explicit_finite_zero(amount):
    root = statement()
    root.find("ChangeInNAV").set("mysteryCapital", amount)
    result = run(root, start=START)
    assert result["status"] == "unavailable" and "period" not in result


def test_unknown_zero_flow_has_no_financial_effect_but_named_pnl_adjustment_is_not_capital():
    root = statement()
    bridge = root.find("ChangeInNAV")
    bridge.set("newAssetTransfer", "0")
    bridge.set("transferredPnlAdjustments", "-4")
    result = run(root, start=START)
    assert result["status"] == "ok"
    assert result["period"]["net_external_flow_base"] == 20
    assert result["period"]["pnl_base"] == 9


def test_complete_daily_sequence_with_explicit_zero_initial_days_and_weekend():
    root = daily_sequence(initial_zero_days=2)
    result = run(root, start=START)
    assert result["status"] == "ok", result
    assert result["period"]["bridge_count"] == 6
    assert result["period"]["opening_nav_base"] == 0
    assert result["period"]["net_external_flow_base"] == 4
    assert result["period"]["closing_nav_base"] == 12
    assert result["period"]["pnl_base"] == 8
    assert result["period"]["evidence_basis"] == "complete_daily_change_in_nav"


def test_operational_hole_rejected_even_if_balances_are_zero_on_both_sides():
    root = daily_sequence(initial_zero_days=20)
    container = root.find("FlexStatements")
    container.remove(container[1])
    unavailable(run(root, start=START), "missing_operational_day")


def test_hole_not_hidden_by_skipping_bridge_but_retaining_nav():
    root = daily_sequence(initial_zero_days=20)
    row = root.find("FlexStatements")[2]
    row.remove(row.find("ChangeInNAV"))
    unavailable(run(root, start=START), "missing_operational_day")


def test_daily_bridge_endpoint_discontinuity_is_unavailable():
    root = daily_sequence()
    root.find("FlexStatements")[2].find("ChangeInNAV").set("startingValue", "-1")
    unavailable(run(root, start=START), "opening_nav_bridge_mismatch")


def test_daily_prior_comparison_cannot_contradict_previous_day_components():
    root = daily_sequence()
    row = root.find("FlexStatements")[2].find("EquitySummaryInBase")[0]
    row.set("cash", "5")
    row.set("cashLong", "5")
    row.set("stock", "1")
    row.set("stockLong", "1")
    unavailable(run(root, start=START), "inconsistent_daily_nav_components")


def test_partial_aggregate_cannot_be_used_as_complete_period():
    root = statement(start=START + timedelta(days=1))
    unavailable(run(root, start=START), "non_daily_or_partial_period_bridge")


def test_digest_binds_exact_input_and_invalid_digest_is_unavailable():
    root = statement()
    text = ET.tostring(root, encoding="unicode")
    digest = hashlib.sha256(text.encode()).hexdigest()
    assert run(root, expected_sha256=digest.upper())["source_sha256"] == digest
    unavailable(run(root, expected_sha256="0" * 64), "source_sha256_mismatch")
    unavailable(run(root, expected_sha256="invalid"), "source_sha256_mismatch")


@pytest.mark.parametrize("text", ["<broken>", "<FlexStatement>\x00</FlexStatement>", "\ud800"])
def test_malformed_xml_is_unavailable(text):
    result = close.parse_daily_close_evidence(text, account_id=ACCOUNT, as_of=DAY)
    unavailable(result, "malformed_xml")


@pytest.mark.parametrize("text", [
    '<!DOCTYPE a [<!ENTITY x "expanded">]><a>&x;</a>',
    '<!DOCTYPE a SYSTEM "file:///private"><a/>',
    '<!ENTITY x SYSTEM "https://invalid.example/private"><a/>',
])
def test_entity_and_doctype_are_rejected_before_parsing(text):
    unavailable(close.parse_daily_close_evidence(text, account_id=ACCOUNT, as_of=DAY), "unsafe_xml_declaration")


def test_size_and_depth_are_bounded(monkeypatch):
    monkeypatch.setattr(close, "MAX_XML_BYTES", 10)
    unavailable(close.parse_daily_close_evidence("<a>12345</a>", account_id=ACCOUNT, as_of=DAY), "xml_size_limit")
    monkeypatch.setattr(close, "MAX_XML_BYTES", 10000)
    text = "<a>" * 66 + "</a>" * 66
    unavailable(close.parse_daily_close_evidence(text, account_id=ACCOUNT, as_of=DAY), "xml_depth_limit")


@pytest.mark.parametrize("kwargs,reason", [
    ({"account_id": ""}, "invalid_account_id"),
    ({"as_of": datetime(2024, 4, 8)}, "invalid_as_of"),
    ({"period_start": DAY + timedelta(days=1)}, "invalid_period_start"),
    ({"period_start": "2024-04-01"}, "invalid_period_start"),
])
def test_invalid_arguments_are_unavailable(kwargs, reason):
    inputs = {"account_id": ACCOUNT, "as_of": DAY, **kwargs}
    unavailable(close.parse_daily_close_evidence(ET.tostring(statement(), encoding="unicode"), **inputs), reason)


def test_decimal_context_is_not_inherited_from_caller():
    with localcontext() as context:
        context.prec = 2
        context.rounding = ROUND_UP
        context.Emax = 2
        context.Emin = -2
        context.traps[Subnormal] = True
        context.traps[Underflow] = True
        result = run(start=START)
    assert result["nav_base"] == 79 and result["period"]["pnl_base"] == 9


def test_module_has_no_dependency_on_audit_performance_or_io():
    source = Path(close.__file__).read_text(encoding="utf-8")
    assert "from services" not in source and "import services" not in source
    assert "ibkr_account_audit" not in source and "ibkr_performance" not in source


def test_series_parses_xml_once_and_matches_single_day_results(monkeypatch):
    root = daily_sequence(initial_zero_days=2)
    text = ET.tostring(root, encoding="unicode")
    parse = close.ET.fromstring
    calls = []

    def counted(value):
        calls.append(value)
        return parse(value)

    monkeypatch.setattr(close.ET, "fromstring", counted)
    result = close.parse_daily_close_series(text, account_id=ACCOUNT)
    assert result["status"] == "ok" and len(calls) == 1
    assert len(result["daily_closes"]) == 6
    assert result["daily_closes"][START.isoformat()]["nav_base"] == 0
    assert result["daily_closes"][DAY.isoformat()] == run(root)
    assert all("period" not in daily for daily in result["daily_closes"].values())


def test_series_retains_failed_day_and_marks_batch_unavailable():
    root = daily_sequence()
    broken = root.find("FlexStatements")[2]
    current(broken).attrib.pop("interestAccruals")
    result = close.parse_daily_close_series(ET.tostring(root, encoding="unicode"), account_id=ACCOUNT)
    assert result["status"] == "unavailable"
    assert len(result["daily_closes"]) == 6
    unavailable(result["daily_closes"]["2024-04-03"], "missing_amount:interestAccruals")
    assert result["daily_closes"][DAY.isoformat()]["status"] == "ok"


def test_series_document_failure_has_no_invented_dates():
    result = close.parse_daily_close_series("<broken>", account_id=ACCOUNT)
    assert result["daily_closes"] == {} and result["status"] == "unavailable"


@pytest.mark.parametrize("field,value", [("accountId", "OTHER_ACCOUNT"), ("model", "PARTITION"),
                                       ("baseCurrency", "EUR"), ("reportDate", "20240407")])
def test_empty_holdings_container_cannot_override_scope(field, value):
    root = statement(zero=True)
    root.find("OpenPositions").set(field, value)
    assert run(root)["status"] == "unavailable"


def test_impossible_long_short_signs_are_not_a_valid_component_proof():
    root = statement()
    current(root).set("stockLong", "-1")
    current(root).set("stockShort", "101")
    unavailable(run(root), "invalid_long_short_sign:stock")


def test_orphan_subcomponent_long_short_cannot_hide_partial_columns():
    root = statement()
    current(root).set("brokerCashComponentLong", "0")
    current(root).set("brokerCashComponentShort", "-20")
    unavailable(run(root), "missing_amount:brokerCashComponent")


@pytest.mark.parametrize("base,day", [("EUR", date(2022, 11, 2)), ("HKD", date(2025, 7, 17)),
                                     ("MXN", date(2023, 5, 4))])
def test_schema_generalizes_across_base_currency_and_business_date(base, day):
    root = statement(day=day, start=day)
    for row in root.iter():
        if row.get("currency") == "USD":
            row.set("currency", base)
    result = run(root, day=day, start=day)
    assert result["status"] == "ok" and result["base_currency"] == base
    assert result["nav_base"] == 79 and result["period"]["pnl_base"] == 9


def test_missing_empty_holding_gross_zero_field_is_not_accepted():
    root = statement(zero=True)
    current(root).attrib.pop("stockLong")
    current(root).attrib.pop("stockShort")
    unavailable(run(root), "empty_holdings_without_gross_zero_proof")


def test_duplicate_exact_period_bridge_is_ambiguous():
    root = statement()
    root.append(deepcopy(root.find("ChangeInNAV")))
    unavailable(run(root, start=START), "ambiguous_exact_period_bridge")


@pytest.mark.parametrize("tag", ["EquitySummaryByReportDateInBase", "ChangeInNAV", "OpenPosition"])
def test_malformed_selected_dates_cannot_be_discarded_as_old_evidence(tag):
    root = statement()
    row = current(root) if tag == "EquitySummaryByReportDateInBase" else next(root.iter(tag))
    row.set("fromDate" if tag == "ChangeInNAV" else "reportDate", "20240230")
    assert run(root, start=START)["status"] == "unavailable"


def test_xml_namespaces_do_not_change_scope_or_values():
    root = statement()
    for row in root.iter():
        row.tag = "{urn:synthetic:flex}" + row.tag
    assert run(root, start=START)["period"]["pnl_base"] == 9


def test_source_digest_is_over_exact_text_including_bom():
    text = "\ufeff" + ET.tostring(statement(), encoding="unicode")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    result = close.parse_daily_close_evidence(text, account_id=ACCOUNT, as_of=DAY, expected_sha256=digest)
    assert result["status"] == "ok" and result["source_sha256"] == digest
