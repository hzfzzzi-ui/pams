"""Synthetic official evidence only; no production balances or identities."""
import ast
import hashlib
import json
import sys
import xml.etree.ElementTree as ET
from copy import deepcopy
from datetime import date
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend/api"))
from services import ibkr_account_audit as audit

DAY = date(2024, 4, 8)
START = date(2024, 4, 1)


def statement(account="SYNTHETIC", day=DAY, base="USD"):
    root = ET.Element("FlexStatement", accountId=account, fromDate=START.strftime("%Y%m%d"),
                      toDate=day.strftime("%Y%m%d"))
    ET.SubElement(root, "AccountInformation", accountId=account, currency=base)
    cash = ET.SubElement(root, "CashReport")
    for currency, value, level in (("BASE_SUMMARY", "-20", "BaseCurrency"), (base, "-20", "Currency")):
        ET.SubElement(cash, "CashReportCurrency", accountId=account, currency=currency,
                      fromDate=day.strftime("%Y%m%d"), toDate=day.strftime("%Y%m%d"),
                      endingCash=value, endingSettledCash="-25", levelOfDetail=level)
    positions = ET.SubElement(root, "OpenPositions")
    ET.SubElement(positions, "OpenPosition", accountId=account, conid="synthetic-contract", symbol="SYN",
                  currency=base, reportDate=day.strftime("%Y%m%d"), assetCategory="STK",
                  position="2", positionValue="100", fxRateToBase="1", levelOfDetail="SUMMARY")
    # Accruals make NAV different from cash + positions. This is intentional.
    ET.SubElement(root, "EquitySummaryByReportDateInBase", accountId=account,
                  reportDate=day.strftime("%Y%m%d"), total="83", cash="-20", stock="100", interestAccruals="3")
    ET.SubElement(root, "ChangeInNAV", accountId=account, fromDate=START.strftime("%Y%m%d"),
                  toDate=day.strftime("%Y%m%d"), startingValue="50", endingValue="83",
                  depositsWithdrawals="20", internalCashTransfers="0", assetTransfers="0",
                  linkingAdjustments="0", debitCardActivity="0", billPay="0")
    return root


def state(account="SYNTHETIC", day=DAY, base="USD"):
    return {"account_id": account, "as_of": day.isoformat(), "base_currency": base,
            "ownership_basis": "full_account", "valuation_basis": "ibkr_statement_close",
            "cash_basis": "ending_cash", "position_basis": "signed_market_value", "nav_basis": "official_nav_total",
            "cash_base": "-20", "position_value_base": "100", "net_liquidation_base": "83",
            "cash_by_currency": {base: "-20"}, "fx_basis": "statement_date_to_base", "fx_rates_to_base": {base: "1"},
            "positions": [{"conid": "synthetic-contract", "currency": base, "quantity": "2", "position_value": "100"}],
            "period": {"from_date": START.isoformat(), "to_date": day.isoformat(),
                       "basis": "nav_change_less_external_flows", "opening_nav_base": "50",
                       "net_external_flow_base": "20", "closing_nav_base": "83", "pnl_base": "13"}}


def run(root=None, internal=None, **kwargs):
    return audit.audit_ibkr_account(xml_text=ET.tostring(root if root is not None else statement(), encoding="unicode"),
        account_id=kwargs.pop("account_id", "SYNTHETIC"), as_of=kwargs.pop("as_of", DAY),
        base_currency=kwargs.pop("base_currency", "USD"), internal_state=internal if internal is not None else state(),
        period_start=kwargs.pop("period_start", START), **kwargs)


def check(result, key):
    return next(c for c in result["checks"] if c["id"] == key)


def reasons(result, key):
    return set(check(result, key)["reason_counts"])


def remove(root, tag):
    for parent in root.iter():
        for child in list(parent):
            if child.tag == tag:
                parent.remove(child)


def test_independent_cash_positions_nav_period_and_no_financing_double_deduction():
    internal = state()
    before = deepcopy(internal)
    result = run(internal=internal)
    assert internal == before and result["read_only"] and not result["executable"]
    assert result["status"] == "review"  # Historical series and percentage returns are not certified.
    for key in ("cash", "positions", "nav", "fx", "period_return"):
        assert check(result, key)["status"] == "ok"
        assert check(result, key)["compared"] > 0
    assert check(result, "nav")["evidence"]["net_liquidation_base"] == "83"
    assert check(result, "nav")["evidence"]["additional_financing_deduction"] is False
    assert check(result, "cash")["evidence"]["cash_base"] == "-20"
    assert check(result, "period_return")["evidence"]["pnl_base"] == "13"
    assert check(result, "period_return")["evidence"]["return_ratio"] is None
    assert not check(result, "period_return")["evidence"]["twr_mwr_verified"]
    assert json.loads(json.dumps(result)) == result


@pytest.mark.parametrize("field,value,key,delta", [
    ("cash_base", "-40", "cash", "-20"),
    ("position_value_base", "120", "positions", "20"),
    ("net_liquidation_base", "63", "nav", "-20"),
])
def test_adequate_evidence_reports_actual_financial_delta(field, value, key, delta):
    internal = state()
    internal[field] = value
    result = run(internal=internal)
    item = next(i for i in check(result, key)["issues"] if i["reason"] == "financial_delta")
    assert item["delta"] == delta and result["status"] == "error"


@pytest.mark.parametrize("field,value,reason", [
    ("account_id", "SOMEONE_ELSE", "semantic_mismatch"),
    ("base_currency", "EUR", "semantic_mismatch"),
    ("ownership_basis", "personal_share", "semantic_mismatch"),
    ("valuation_basis", "live_market", "semantic_mismatch"),
    ("ownership_basis", None, "missing_basis"),
    ("as_of", "2024-04-07", "stale_internal_state"),
    ("as_of", "2024-04-09", "internal_date_mismatch"),
    ("as_of", None, "internal_date_mismatch"),
])
def test_noncomparable_scope_never_compares_money(field, value, reason):
    internal = state()
    internal[field], internal["cash_base"] = value, "99999"
    result = run(internal=internal)
    assert reason in reasons(result, "source")
    assert all(c["compared"] == 0 for c in result["checks"])
    assert result["status"] == "review"


@pytest.mark.parametrize("basis,key,value", [
    ("cash_basis", "cash", "ending_settled_cash"),
    ("position_basis", "positions", "gross_exposure"),
    ("nav_basis", "nav", "cash_plus_stock_minus_financing"),
    ("fx_basis", "fx", "current_fx"),
])
def test_metric_semantic_mismatch_is_not_a_financial_delta(basis, key, value):
    internal = state()
    internal[basis] = value
    result = run(internal=internal)
    assert "semantic_mismatch" in reasons(result, key)
    assert check(result, key)["compared"] == 0


@pytest.mark.parametrize("bad", [None, "", "NaN", "Infinity", "-Infinity", "wrong", "1,2", True, "1e999999"])
@pytest.mark.parametrize("key,field", [("cash", "endingCash"), ("positions", "positionValue"), ("nav", "total")])
def test_missing_or_invalid_official_values_never_become_zero(bad, key, field):
    root = statement()
    tag = {"cash": "CashReportCurrency", "positions": "OpenPosition", "nav": "EquitySummaryByReportDateInBase"}[key]
    for row in root.iter(tag):
        if bad is None:
            row.attrib.pop(field)
        else:
            row.set(field, str(bad))
    result = run(root)
    assert {"missing_value", "invalid_value"} & reasons(result, key)
    assert check(result, key)["compared"] == 0
    assert "financial_delta" not in reasons(result, key)


def test_explicit_zero_is_present_and_empty_positions_section_proves_zero_positions():
    root, internal = statement(), state()
    for row in root.iter("CashReportCurrency"):
        row.set("endingCash", "0")
    remove(root, "OpenPosition")
    internal.update(cash_base="0", cash_by_currency={"USD": "0"}, position_value_base="0", positions=[])
    result = run(root, internal)
    assert check(result, "cash")["status"] == "ok"
    assert check(result, "cash")["evidence"]["cash_base"] == "0"
    assert check(result, "positions")["status"] == "ok"
    assert check(result, "positions")["evidence"]["position_value_base"] == "0"
    remove(root, "OpenPositions")
    result = run(root, internal)
    assert check(result, "positions")["evidence"]["position_value_base"] is None
    assert "missing_evidence" in reasons(result, "positions")


@pytest.mark.parametrize("tag", ["CashReportCurrency", "OpenPosition", "EquitySummaryByReportDateInBase", "ChangeInNAV"])
def test_duplicate_evidence_refuses_arbitrary_selection(tag):
    root = statement()
    for parent in root.iter():
        found = parent.find(tag)
        if found is not None:
            parent.append(deepcopy(found))
            break
    result = run(root)
    key = {"CashReportCurrency": "cash", "OpenPosition": "positions",
           "EquitySummaryByReportDateInBase": "nav", "ChangeInNAV": "period_return"}[tag]
    assert check(result, key)["status"] == "review"
    assert check(result, key)["compared"] == 0


@pytest.mark.parametrize("tag", ["AccountInformation", "CashReportCurrency", "OpenPosition", "ChangeInNAV"])
@pytest.mark.parametrize("attribute,value", [("accountId", "OTHER"), ("model", "MODEL")])
def test_mixed_account_or_model_cannot_claim_full_account(tag, attribute, value):
    root = statement()
    next(root.iter(tag)).set(attribute, value)
    result = run(root)
    assert "official_account_or_ownership_mismatch" in reasons(result, "source")
    assert all(c["compared"] == 0 for c in result["checks"])


def test_select_exact_account_date_not_latest_or_largest_account():
    wrapper = ET.Element("FlexQueryResponse")
    wrapper.extend([statement("OTHER"), statement(day=date(2024, 4, 9)), statement()])
    assert check(run(wrapper), "cash")["status"] == "ok"
    wrapper.append(statement())
    result = run(wrapper)
    assert "ambiguous_evidence" in reasons(result, "source")
    assert check(result, "cash")["compared"] == 0


@pytest.mark.parametrize("quality", [
    {"status": "stale", "statement_as_of": DAY.isoformat()},
    {"status": "review", "statement_as_of": DAY.isoformat()},
    {"status": "ok", "statement_as_of": DAY.isoformat(), "legacy_unverified": True},
    {"status": "ok", "statement_as_of": DAY.isoformat(), "using_previous": True},
    {"status": "ok", "statement_as_of": DAY.isoformat(), "expected_statement_as_of": "2024-04-09"},
])
def test_publication_quality_does_not_turn_stale_report_into_delta(quality):
    result = run(quality=quality)
    assert "source_unverified" in reasons(result, "source")
    assert all(c["compared"] == 0 for c in result["checks"])
    assert check(result, "cash")["evidence"]["cash_base"] == "-20"


def test_no_date_fallback_for_report_cash_or_positions():
    assert "stale_report" in reasons(run(as_of=date(2024, 4, 9)), "source")
    root = statement()
    for row in root.iter("CashReportCurrency"):
        row.set("toDate", "20240407")
    next(root.iter("OpenPosition")).set("reportDate", "20240407")
    result = run(root)
    assert check(result, "cash")["evidence"]["cash_base"] is None
    assert "position_date_mismatch" in reasons(result, "positions")
    assert check(result, "positions")["compared"] == 0


def foreign_case(rate="0.1"):
    root, internal = statement(), state()
    next(root.iter("OpenPosition")).set("currency", "HKD")
    next(root.iter("OpenPosition")).set("positionValue", "1000")
    next(root.iter("OpenPosition")).attrib.pop("fxRateToBase")
    conversions = ET.SubElement(root, "ConversionRates")
    ET.SubElement(conversions, "ConversionRate", reportDate="20240408", fromCurrency="HKD", toCurrency="USD", rate=rate)
    internal["positions"][0].update(currency="HKD", position_value="1000")
    internal["fx_rates_to_base"]["HKD"] = rate
    return root, internal


def test_statement_date_explicit_fx_and_option_multiplier_use_official_position_value():
    root, internal = foreign_case()
    row = next(root.iter("OpenPosition"))
    row.set("assetCategory", "OPT")
    row.set("markPrice", "5")
    row.set("multiplier", "100")
    result = run(root, internal)
    assert check(result, "positions")["status"] == "ok"
    assert check(result, "positions")["evidence"]["position_value_base"] == "100.0"
    assert check(result, "fx")["status"] == "ok"


@pytest.mark.parametrize("change,reason", [("missing", "missing_historical_fx"), ("stale", "missing_historical_fx"),
    ("inverse", "missing_historical_fx"), ("zero", "invalid_fx"), ("negative", "invalid_fx"),
    ("nonfinite", "invalid_fx"), ("conflict", "conflicting_fx")])
def test_no_default_one_current_inverse_or_conflicting_fx(change, reason):
    root, internal = foreign_case()
    row = next(root.iter("ConversionRate"))
    if change == "missing":
        remove(root, "ConversionRate")
    elif change == "stale":
        row.set("reportDate", "20240407")
    elif change == "inverse":
        row.set("fromCurrency", "USD")
        row.set("toCurrency", "HKD")
    elif change == "conflict":
        next(root.iter("OpenPosition")).set("fxRateToBase", "0.2")
    else:
        row.set("rate", {"zero": "0", "negative": "-0.1", "nonfinite": "NaN"}[change])
    result = run(root, internal)
    assert reason in reasons(result, "fx")
    assert check(result, "positions")["evidence"]["position_value_base"] is None
    assert check(result, "positions")["compared"] == 0


def test_base_summary_or_complete_native_cash_explicit_fx_only():
    root, internal = foreign_case()
    base_row = next(r for r in root.iter("CashReportCurrency") if r.get("currency") == "BASE_SUMMARY")
    root.find("CashReport").remove(base_row)
    native = next(root.iter("CashReportCurrency"))
    native.set("currency", "HKD")
    native.set("endingCash", "-200")
    internal["cash_by_currency"] = {"HKD": "-200"}
    result = run(root, internal)
    assert check(result, "cash")["status"] == "ok"
    assert Decimal(check(result, "cash")["evidence"]["cash_base"]) == Decimal(-20)
    remove(root, "ConversionRate")
    result = run(root, internal)
    assert check(result, "cash")["evidence"]["cash_base"] is None
    assert check(result, "cash")["compared"] == 0


def test_summary_and_lots_are_not_double_counted_and_short_value_stays_signed():
    root, internal = statement(), state()
    row = next(root.iter("OpenPosition"))
    row.set("position", "-2")
    row.set("positionValue", "-100")
    lot = deepcopy(row)
    lot.set("levelOfDetail", "LOT")
    root.find("OpenPositions").append(lot)
    internal["position_value_base"] = "-100"
    internal["positions"][0].update(quantity="-2", position_value="-100")
    result = run(root, internal)
    assert check(result, "positions")["status"] == "ok"
    assert check(result, "positions")["evidence"]["position_count"] == 1
    row.set("levelOfDetail", "LOT")
    assert "missing_position_summary" in reasons(run(root, internal), "positions")


def test_a_lot_only_contract_cannot_disappear_beside_another_contract_summary():
    root = statement()
    lot = deepcopy(next(root.iter("OpenPosition")))
    lot.set("levelOfDetail", "LOT")
    lot.set("conid", "another-contract")
    root.find("OpenPositions").append(lot)
    result = run(root)
    assert "missing_position_summary" in reasons(result, "positions")
    assert check(result, "positions")["evidence"]["position_value_base"] is None
    assert check(result, "positions")["compared"] == 0


def test_base_identity_fx_is_exact_even_below_numeric_comparison_tolerance():
    root = statement()
    next(root.iter("OpenPosition")).set("fxRateToBase", "1.000000001")
    result = run(root)
    assert "conflicting_fx" in reasons(result, "fx")
    assert check(result, "positions")["compared"] == 0


def test_position_quantity_delta_cannot_hide_behind_equal_aggregate():
    internal = state()
    internal["positions"][0]["quantity"] = "3"
    result = run(internal=internal)
    assert check(result, "positions")["status"] == "error"
    assert any(i["field"].endswith(":quantity") for i in check(result, "positions")["issues"])


@pytest.mark.parametrize("change,reason", [("duplicate", "ambiguous_internal_position_identity"),
    ("missing", "position_set_mismatch"), ("extra", "position_set_mismatch"),
    ("old", "internal_position_scope_mismatch"), ("wrong_account", "internal_position_scope_mismatch")])
def test_position_identity_mismatch_is_not_coerced_to_zero(change, reason):
    internal = state()
    if change == "duplicate":
        internal["positions"] *= 2
    elif change == "missing":
        internal["positions"] = []
    elif change == "extra":
        internal["positions"].append({"conid": "EXTRA", "currency": "USD", "quantity": "0", "position_value": "0"})
    elif change == "old":
        internal["positions"][0]["as_of"] = "2024-04-07"
    else:
        internal["positions"][0]["account_id"] = "OTHER"
    result = run(internal=internal)
    assert reason in reasons(result, "positions")
    assert "financial_delta" not in reasons(result, "positions")


@pytest.mark.parametrize("field", list(audit.FLOW_FIELDS) + ["startingValue", "endingValue"])
def test_period_requires_explicit_opening_all_flows_and_closing(field):
    root = statement()
    next(root.iter("ChangeInNAV")).attrib.pop(field)
    result = run(root)
    assert check(result, "period_return")["evidence"]["pnl_base"] is None
    assert check(result, "period_return")["compared"] == 0


@pytest.mark.parametrize("change,reason", [("range", "period_mismatch"), ("basis", "semantic_mismatch"),
                                        ("internal_pnl", "financial_delta")])
def test_period_comparison_respects_exact_range_and_definition(change, reason):
    internal = state()
    field, value = {"range": ("from_date", "2024-03-31"), "basis": ("basis", "pnl_over_ytd_deposits"),
                    "internal_pnl": ("pnl_base", "33")}[change]
    internal["period"][field] = value
    result = run(internal=internal)
    assert reason in reasons(result, "period_return")
    assert (check(result, "period_return")["compared"] > 0) == (change == "internal_pnl")


def test_external_asset_transfers_withdrawals_and_linking_not_investment_profit():
    root, internal = statement(), state()
    row = next(root.iter("ChangeInNAV"))
    row.set("depositsWithdrawals", "-5")
    row.set("assetTransfers", "10")
    row.set("linkingAdjustments", "15")
    result = run(root, internal)
    assert check(result, "period_return")["evidence"]["net_external_flow_base"] == "20"
    assert check(result, "period_return")["status"] == "ok"
    row.set("linkingAdjustmentsPaxos", "0")
    result = run(root, internal)
    assert "unsupported_period_flow_fields" in reasons(result, "period_return")
    assert check(result, "period_return")["evidence"]["pnl_base"] is None


@pytest.mark.parametrize("field,value", [("base_currency", "EUR"), ("account_id", "OTHER"),
    ("ownership_basis", "personal_share"), ("valuation_basis", "live_market")])
def test_period_child_cannot_override_common_comparable_scope(field, value):
    internal = state()
    internal["period"][field] = value
    result = run(internal=internal)
    assert "semantic_mismatch" in reasons(result, "period_return")
    assert check(result, "period_return")["compared"] == 0


def test_official_closing_nav_conflict_does_not_generate_internal_period_delta():
    root = statement()
    next(root.iter("ChangeInNAV")).set("endingValue", "84")
    result = run(root)
    assert "official_closing_nav_conflict" in reasons(result, "period_return")
    assert check(result, "period_return")["evidence"]["pnl_base"] is None
    assert check(result, "period_return")["compared"] == 0


def test_zero_period_and_zero_opening_do_not_invent_percentage_return():
    root, internal = statement(), state()
    for field in ("startingValue", "endingValue", "depositsWithdrawals"):
        next(root.iter("ChangeInNAV")).set(field, "0")
    next(root.iter("EquitySummaryByReportDateInBase")).set("total", "0")
    internal["net_liquidation_base"] = "0"
    for field in ("opening_nav_base", "closing_nav_base", "net_external_flow_base", "pnl_base"):
        internal["period"][field] = "0"
    result = run(root, internal)
    assert check(result, "period_return")["status"] == "ok"
    assert check(result, "period_return")["evidence"]["pnl_base"] == "0"
    assert not check(result, "period_return")["evidence"]["return_ratio_verified"]


def test_cash_and_positions_ytd_funding_do_not_prove_nav_or_period_return():
    root = statement()
    remove(root, "EquitySummaryByReportDateInBase")
    remove(root, "ChangeInNAV")
    next(root.iter("CashReportCurrency")).set("depositWithdrawalsYTD", "50")
    result = run(root)
    assert check(result, "cash")["status"] == "ok"
    assert check(result, "positions")["status"] == "ok"
    assert check(result, "nav")["evidence"]["net_liquidation_base"] is None
    assert check(result, "period_return")["evidence"]["pnl_base"] is None
    assert not check(result, "history_coverage")["evidence"]["all_dates_verified"]


def test_one_wide_report_or_many_daily_reports_cannot_certify_nav_history():
    requested = [date(2024, 4, 1), DAY]
    result = run(history_dates=requested)
    evidence = check(result, "history_coverage")["evidence"]
    assert evidence["missing_statement_dates"] == ["2024-04-01"]
    wrapper = ET.Element("FlexStatements")
    wrapper.extend([statement(day=requested[0]), statement()])
    result = run(wrapper, history_dates=requested)
    evidence = check(result, "history_coverage")["evidence"]
    assert evidence["missing_statement_date_count"] == 0
    assert not evidence["all_dates_verified"] and check(result, "history_coverage")["status"] == "review"


@pytest.mark.parametrize("base", ["EUR", "CNY", "JPY"])
def test_base_currency_is_evidence_not_hardcoded_usd(base):
    assert check(run(statement(base=base), state(base=base), base_currency=base), "nav")["status"] == "ok"
    result = run(base_currency=base)
    assert "base_currency_mismatch" in reasons(result, "source")


@pytest.mark.parametrize("tag", ["AccountInformation", "EquitySummaryByReportDateInBase", "OpenPositions", "ChangeInNAV"])
def test_missing_section_is_review_not_silent_zero_or_success(tag):
    root = statement()
    remove(root, tag)
    result = run(root)
    assert result["status"] == "review"


def test_namespace_determinism_immutable_inputs_and_decimal_precision():
    root, internal = statement(), state()
    for node in root.iter():
        node.tag = "{urn:synthetic:flex}" + node.tag
    before = deepcopy(internal)
    expected = run(root, internal)
    with localcontext() as context:
        context.prec = 2
        assert run(root, internal) == expected
    assert internal == before


def test_hash_binding_invalid_xml_and_no_external_calculation_imports(monkeypatch):
    root = statement()
    content = ET.tostring(root, encoding="unicode")
    digest = hashlib.sha256(content.encode()).hexdigest()
    assert run(root, expected_sha256=digest)["status"] == "review"
    result = run(root, expected_sha256="0" * 64)
    assert result["status"] == "error" and "report_hash_mismatch" in reasons(result, "source")
    for xml in ("<broken>", '<!DOCTYPE x [<!ENTITY a "boom">]><FlexStatement/>'):
        result = audit.audit_ibkr_account(xml_text=xml, account_id="SYNTHETIC", as_of=DAY, base_currency="USD")
        assert result["status"] == "error"
    # Poison the audited services: this module must not use any of them.
    for name in ("services.ibkr_performance", "services.investment_return_ibkr", "collectors.ibkr_flex"):
        monkeypatch.setitem(sys.modules, name, None)
    assert run(root)["status"] == "review"
    tree = ast.parse(Path(audit.__file__).read_text(encoding="utf-8"))
    modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not any(module and module.startswith(("services", "collectors", "endpoints")) for module in modules)


@pytest.mark.parametrize("value", [None, "", "NaN", "Infinity", False])
def test_missing_invalid_internal_value_not_zero(value):
    internal = state()
    internal["cash_base"] = value
    result = run(internal=internal)
    assert {"missing_internal_value", "invalid_internal_value"} & reasons(result, "cash")
    assert "financial_delta" not in reasons(result, "cash")


def test_empty_fx_map_is_not_a_successful_complete_comparison():
    internal = state()
    internal["fx_rates_to_base"] = {}
    result = run(internal=internal)
    assert "missing_internal_value" in reasons(result, "fx")
    assert check(result, "fx")["compared"] == 0


def test_result_limits_do_not_limit_financial_audit_or_hide_tail_delta():
    root, internal = statement(), state()
    remove(root, "OpenPosition")
    internal["positions"] = []
    for index in range(audit.ISSUE_LIMIT + 1):
        conid = "contract-" + str(index)
        ET.SubElement(root.find("OpenPositions"), "OpenPosition", conid=conid, symbol="SYN",
            currency="USD", reportDate="20240408", assetCategory="STK", levelOfDetail="SUMMARY",
            position="1", positionValue="1")
        internal["positions"].append({"conid": conid, "currency": "USD", "quantity": "2", "position_value": "1"})
    internal["position_value_base"] = str(audit.ISSUE_LIMIT + 1)
    result = run(root, internal)
    item = check(result, "positions")
    assert item["status"] == "error" and item["issue_count"] == audit.ISSUE_LIMIT + 1
    assert len(item["issues"]) == audit.ISSUE_LIMIT and item["truncated"]
    assert item["evidence"]["position_count"] == audit.ISSUE_LIMIT + 1
    assert item["compared"] == 1 + (audit.ISSUE_LIMIT + 1) * 2
