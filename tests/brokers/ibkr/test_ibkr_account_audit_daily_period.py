"""Independent audit oracle: synthetic reports; optional private subject JSON/XML.

PAMS_IBKR_AUDIT_PRIVATE_XML and PAMS_IBKR_AUDIT_PRIVATE_STATE opt into a real
file comparison. STATE is the audited subject (account/as_of/base/period/SCOPE),
not official oracle data. Neither private values nor production parser imports
belong in these fixtures.
"""
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal, ROUND_UP, localcontext
import hashlib
import json
import os
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "backend/api"))
from domain.brokers.ibkr import ibkr_account_audit as audit  # noqa: E402

ACCOUNT = "SYNTHETIC_DAILY_ACCOUNT"
START, END = date(2024, 2, 2), date(2024, 2, 6)
D = Decimal


def compact(day):
    return day.strftime("%Y%m%d")


def fixture():
    root = ET.Element("FlexQueryResponse")
    container = ET.SubElement(root, "FlexStatements")
    previous, prior_day = D(50), START - timedelta(days=1)
    for day in (START, START + timedelta(days=3), END):
        statement = ET.SubElement(container, "FlexStatement", accountId=ACCOUNT,
                                  fromDate=compact(day), toDate=compact(day))
        ET.SubElement(statement, "AccountInformation", accountId=ACCOUNT, currency="USD")
        equity = ET.SubElement(statement, "EquitySummaryInBase")
        current = previous + 3
        for nav_day, value in ((prior_day, previous), (day, current)):
            ET.SubElement(equity, "EquitySummaryByReportDateInBase", accountId=ACCOUNT, currency="USD",
                          reportDate=compact(nav_day), total=str(value))
        flows = {key: "0" for key in (*audit.FLOW_FIELDS, *audit.DAILY_UNSUPPORTED_FLOWS)}
        flows["depositsWithdrawals"] = "1"
        ET.SubElement(statement, "ChangeInNAV", accountId=ACCOUNT, currency="USD", fromDate=compact(day),
                      toDate=compact(day), startingValue=str(previous), endingValue=str(current), **flows)
        ET.SubElement(statement, "OpenPositions")
        cash = ET.SubElement(statement, "CashReport")
        for unit in ("BASE_SUMMARY", "USD"):
            ET.SubElement(cash, "CashReportCurrency", accountId=ACCOUNT, currency=unit,
                          fromDate=compact(day), toDate=compact(day), endingCash=str(current))
        previous, prior_day = current, day
    state = {"account_id": ACCOUNT, "as_of": END.isoformat(), "base_currency": "USD", **audit.SCOPE,
             "cash_basis": "ending_cash", "position_basis": "signed_market_value", "nav_basis": "official_nav_total",
             "cash_base": "59", "position_value_base": "0", "net_liquidation_base": "59",
             "period": {"from_date": START.isoformat(), "to_date": END.isoformat(),
                        "basis": "nav_change_less_external_flows", "opening_nav_base": "50",
                        "closing_nav_base": "59", "net_external_flow_base": "3", "pnl_base": "6"}}
    return root, state


def statements(root):
    return list(root.iter("FlexStatement"))


def run(root, state):
    return audit.audit_ibkr_account(xml_text=ET.tostring(root, encoding="unicode"), account_id=ACCOUNT,
        as_of=END, base_currency="USD", internal_state=state, period_start=START,
        quality={"status": "ok", "statement_as_of": END.isoformat()})


def item(result, name="period_return"):
    return next(row for row in result["checks"] if row["id"] == name)


def blocked(root, state):
    check = item(run(root, state))
    assert check["status"] == "review" and check["compared"] == 0
    assert check["evidence"].get("pnl_base") is None
    assert "financial_delta" not in check["reason_counts"]
    return check


def test_complete_weekday_chain_independently_verifies_period_not_fx_or_history():
    root, state = fixture()
    before = deepcopy(state)
    result = run(root, state)
    check = item(result)
    assert check["status"] == "ok" and check["compared"] == 4
    assert check["evidence"]["evidence_basis"] == "complete_daily"
    assert check["evidence"]["bridge_count"] == 3
    for key, value in state["period"].items():
        if key not in {"basis", "from_date", "to_date"}:
            assert D(check["evidence"][key]) == D(value)
    assert check["evidence"]["basis"] == "nav_change_less_external_flows"
    assert check["evidence"]["return_ratio_verified"] is False
    assert check["evidence"]["twr_mwr_verified"] is False
    assert item(result, "fx")["status"] == item(result, "history_coverage")["status"] == "review"
    assert result["status"] == "review" and state == before
    assert json.loads(json.dumps(result)) == result


@pytest.mark.parametrize("field", audit.FLOW_FIELDS)
def test_each_native_flow_is_summed_signed_not_treated_as_pnl(field):
    root, state = fixture()
    bridge = statements(root)[1].find("ChangeInNAV")
    bridge.set("depositsWithdrawals", "0")
    bridge.set(field, "-4")
    state["period"].update(net_external_flow_base="-2", pnl_base="11")
    check = item(run(root, state))
    assert check["status"] == "ok"
    assert D(check["evidence"]["pnl_base"]) == 11


@pytest.mark.parametrize("tag", ["FlexStatement", "AccountInformation", "ChangeInNAV",
                                 "EquitySummaryByReportDateInBase"])
def test_duplicate_middle_evidence_cannot_be_arbitrarily_selected(tag):
    root, state = fixture()
    middle = statements(root)[1]
    if tag == "FlexStatement":
        root.find("FlexStatements").append(deepcopy(middle))
    elif tag == "EquitySummaryByReportDateInBase":
        equity = middle.find("EquitySummaryInBase")
        equity.append(deepcopy(equity[-1]))
    else:
        middle.append(deepcopy(middle.find(tag)))
    blocked(root, state)


@pytest.mark.parametrize("kind", ["missing_weekday", "missing_first_day", "prior", "duplicate_prior", "current",
    "shift_prior_date", "shift_statement", "shift_bridge", "partial_bridge", "partial_statement",
    "prior_conflict", "start_conflict", "end_conflict", "chain_conflict", "invalid_nav_date", "orphan"])
def test_incomplete_or_conflicting_daily_chain_never_compares_internal_money(kind):
    root, state = fixture()
    middle = statements(root)[1]
    equity, bridge = middle.find("EquitySummaryInBase"), middle.find("ChangeInNAV")
    if kind in {"missing_weekday", "missing_first_day"}:
        root.find("FlexStatements").remove(middle if kind == "missing_weekday" else statements(root)[0])
    elif kind in {"prior", "current"}:
        equity.remove(equity[0 if kind == "prior" else 1])
    elif kind == "duplicate_prior":
        equity.append(deepcopy(equity[0]))
    elif kind == "shift_prior_date":
        equity[0].set("reportDate", "20240131")
    elif kind == "shift_statement":
        middle.set("toDate", "20240204")
    elif kind in {"shift_bridge", "partial_bridge"}:
        bridge.set("fromDate", "20240204")
        if kind == "shift_bridge":
            bridge.set("toDate", "20240204")
    elif kind == "partial_statement":
        middle.set("fromDate", "20240204")
    elif kind == "prior_conflict":
        equity[0].set("total", "999")
    elif kind in {"start_conflict", "end_conflict"}:
        bridge.set("startingValue" if kind == "start_conflict" else "endingValue", "999")
    elif kind == "chain_conflict":
        equity[0].set("total", "70")
        bridge.set("startingValue", "70")
    elif kind == "invalid_nav_date":
        equity[0].set("reportDate", "not-a-day")
    else:
        root.append(deepcopy(bridge))
    blocked(root, state)


@pytest.mark.parametrize("tag", ["FlexStatement", "AccountInformation", "ChangeInNAV",
                                 "EquitySummaryByReportDateInBase", "EquitySummaryInBase", "root"])
@pytest.mark.parametrize("field,value", [("accountId", "OTHER_ACCOUNT"), ("model", "personal-model"),
                                         ("currency", "EUR"), ("baseCurrency", "EUR")])
def test_each_daily_leaf_and_ancestor_must_prove_requested_full_account_base(tag, field, value):
    root, state = fixture()
    middle = statements(root)[1]
    target = root if tag == "root" else middle if tag == "FlexStatement" else middle.find(".//" + tag)
    target.set(field, value)
    blocked(root, state)


@pytest.mark.parametrize("tag", ["AccountInformation", "ChangeInNAV", "EquitySummaryByReportDateInBase"])
@pytest.mark.parametrize("field", ["accountId", "currency"])
def test_daily_leaf_scope_cannot_be_inferred_from_manifest_or_statement(tag, field):
    root, state = fixture()
    statements(root)[1].find(".//" + tag).attrib.pop(field)
    blocked(root, state)


@pytest.mark.parametrize("field,value", [("reportDate", "20240204"), ("fromDate", "20240206"),
                                         ("toDate", "20240204")])
def test_unknown_ancestor_dates_constrain_consumed_evidence(field, value):
    root, state = fixture()
    wrapper = statements(root)[1].find("EquitySummaryInBase")
    wrapper.set("fromDate", "20240201")
    wrapper.set("toDate", "20240205")
    wrapper.set(field, value)
    blocked(root, state)


@pytest.mark.parametrize("field", [*audit.FLOW_FIELDS, *audit.DAILY_UNSUPPORTED_FLOWS, "startingValue", "endingValue"])
@pytest.mark.parametrize("value", [None, "NaN"])
def test_all_capital_fields_and_bridge_endpoints_must_be_explicit_finite(field, value):
    root, state = fixture()
    bridge = statements(root)[1].find("ChangeInNAV")
    if value is None:
        bridge.attrib.pop(field)
    else:
        bridge.set(field, value)
    blocked(root, state)


@pytest.mark.parametrize("field", audit.DAILY_UNSUPPORTED_FLOWS)
@pytest.mark.parametrize("amount", ["1", "-1"])
def test_uninterpreted_known_capital_categories_must_be_zero(field, amount):
    root, state = fixture()
    statements(root)[1].find("ChangeInNAV").set(field, amount)
    blocked(root, state)


@pytest.mark.parametrize("field", ["mysteryCapital", "newTransfer", "newFunding", "unknown"])
def test_unknown_capital_variants_block_even_zero(field):
    root, state = fixture()
    statements(root)[1].find("ChangeInNAV").set(field, "0")
    assert "unsupported_period_flow_fields" in blocked(root, state)["reason_counts"]


def test_unknown_nested_capital_evidence_is_not_silently_ignored():
    root, state = fixture()
    ET.SubElement(statements(root)[1].find("ChangeInNAV"), "NewFunding", amount="0")
    assert "unsupported_period_flow_fields" in blocked(root, state)["reason_counts"]


def test_independent_period_delta_still_detects_wrong_subject_profit():
    root, state = fixture()
    state["period"]["pnl_base"] = "99"
    check = item(run(root, state))
    assert check["status"] == "error" and "financial_delta" in check["reason_counts"]


def test_exact_period_path_is_unchanged_and_never_falls_back_on_bad_exact_evidence():
    root, state = fixture()
    current = statements(root)[-1]
    row = current.find("ChangeInNAV")
    row.set("fromDate", compact(START))
    row.set("startingValue", "50")
    row.set("depositsWithdrawals", "3")
    for field in audit.DAILY_UNSUPPORTED_FLOWS:
        row.attrib.pop(field)
    without, with_root = audit._check("period_return"), audit._check("period_return")
    assert audit._period(without, current, START, END, "USD", D(59)) == audit._period(
        with_root, current, START, END, "USD", D(59), root=root, account_id=ACCOUNT)
    assert without == with_root and "evidence_basis" not in with_root["evidence"]
    row.attrib.pop("billPay")
    assert item(run(root, state))["evidence"]["pnl_base"] is None
    assert "evidence_basis" not in item(run(root, state))["evidence"]


def test_independent_auditor_does_not_import_production_close_or_performance():
    import ast
    source = Path(audit.__file__).read_text(encoding="utf-8")
    imports = [node.module or "" for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ImportFrom)]
    assert not any("daily_close" in name or "performance" in name for name in imports)


def test_hostile_decimal_context_cannot_change_daily_period_proof():
    root, state = fixture()
    expected = run(root, state)
    with localcontext() as context:
        context.prec = 2
        context.rounding = ROUND_UP
        context.Emax = 1
        context.Emin = -1
        assert run(root, state) == expected


def test_optional_real_private_subject_is_independently_verified():
    xml_path, state_path = os.environ.get("PAMS_IBKR_AUDIT_PRIVATE_XML"), os.environ.get("PAMS_IBKR_AUDIT_PRIVATE_STATE")
    if not xml_path or not state_path:
        pytest.skip("Set PAMS_IBKR_AUDIT_PRIVATE_XML and PAMS_IBKR_AUDIT_PRIVATE_STATE; private files never committed")
    content = Path(xml_path).read_bytes()
    subject = json.loads(Path(state_path).read_text(encoding="utf-8"))
    result = audit.audit_ibkr_account(xml_text=content.decode("utf-8"), account_id=subject["account_id"],
        as_of=date.fromisoformat(subject["as_of"]), base_currency=subject["base_currency"], internal_state=subject,
        period_start=date.fromisoformat(subject["period"]["from_date"]), expected_sha256=hashlib.sha256(content).hexdigest())
    check = item(result)
    assert check["status"] == "ok" and check["compared"] == 4
    assert check["evidence"]["evidence_basis"] == "complete_daily"
    assert item(result, "history_coverage")["status"] == "review"
    if "fx_rates_to_base" not in subject:
        assert item(result, "fx")["status"] == "review"
