"""Execute the actual field adapter and selected bindings without a browser shim."""
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "frontend/public/assets/js/holdings_categories.js").read_text(encoding="utf-8")


def function(name):
    return re.search(r"function " + name + r"\([^\n]*[\s\S]*?(?=\nfunction )", SOURCE).group()


def run(payload, extra=""):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required; mandatory in final CI")
    script = "\n".join(function(name) for name in ("isoDateKey", "isSameOrAfterDate", "confirmedIbkrAccountSummary"))
    script += "\nconst perf = " + json.dumps(payload) + ";\n"
    script += "const confirmedIbkrSummary = confirmedIbkrAccountSummary(perf, '2024-01-02');\n"
    script += extra or "console.log(JSON.stringify(confirmedIbkrSummary));"
    result = subprocess.run([node, "-e", script], text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


def payload():
    return {"status": "ok", "as_of": "2024-01-02", "net_liquidation_cny": 693,
        "total_pnl_cny": 203, "return_capital_cny": 490, "return_ratio": 29 / 70,
        "daily_close": {"status": "ok", "fx_status": "available", "source_account_id": "SYNTHETIC_ACCOUNT", "source_sha256": "a" * 64},
        "portfolio_account": {"status": "ok", "account_code": "ibkr", "ownership_basis": "owner_adjusted", "owner_ratio": 1,
            "currency": "CNY", "source_account_id": "SYNTHETIC_ACCOUNT", "source_sha256": "a" * 64, "as_of": "2024-01-02",
            "net_liquidation_cny": 693, "total_pnl_cny": 203, "return_capital_cny": 490,
            "cash_cny": 700, "position_value_cny": 0, "return_ratio": 29 / 70}}


def test_confirmed_values_are_direct_backend_fields_including_opening_capital():
    assert run(payload()) == {"totalValue": 693, "totalPnl": 203, "totalCost": 490,
                              "cashValue": 700, "positionValue": 0, "totalReturnRate": 29 / 70, "asOf": "2024-01-02"}


def test_real_page_bindings_do_not_recompute_cash_plus_positions_or_profit():
    bindings = "\n".join(re.search(r"const " + name + r" = [\s\S]*?;", SOURCE).group()
        for name in ("ibkrAccountValue", "ibkrDerivedTotalPnl", "ibkrAccountReturnRate"))
    extra = """const useIbkrAccountTotals=true, ibkrHasFlexCashValue=true,
        ibkrPositionValue=1000, ibkrCashValue=-500, ibkrNetDeposit=140,
        ibkrHasAccountNetDeposit=true, ibkrSummary={totalValue:1,totalPnl:2,totalReturnRate:3};
    """ + bindings + "\nconsole.log(JSON.stringify([ibkrAccountValue,ibkrDerivedTotalPnl,ibkrAccountReturnRate]));"
    assert run(payload(), extra) == [693, 203, 29 / 70]


@pytest.mark.parametrize("field,value", [("net_liquidation_cny", None), ("total_pnl_cny", ""),
    ("return_capital_cny", False), ("net_liquidation_cny", []), ("return_ratio", "0.1"),
    ("as_of", "2024-01-01"), ("as_of", "invalid")])
def test_missing_invalid_or_older_fields_do_not_turn_into_confirmed_zeros(field, value):
    data = payload()
    if field == "as_of":
        data[field] = value
    else:
        data["portfolio_account"][field] = value
    assert run(data) is None


@pytest.mark.parametrize("metadata", [None, {"status": "unavailable"}, {"status": "ok", "fx_status": "unavailable"}])
def test_legacy_or_unconfirmed_payload_does_not_activate_new_binding(metadata):
    assert run({**payload(), "daily_close": metadata}) is None


def test_undefined_rate_and_explicit_zero_capital_are_preserved():
    data = payload()
    data["portfolio_account"].update(return_capital_cny=0, return_ratio=None)
    result = run(data)
    assert result["totalCost"] == 0 and result["totalReturnRate"] is None
    block = SOURCE[SOURCE.index("function writeSectionFinancials"):SOURCE.index("function returnRateFromCapitalBase")]
    assert "summary.accountReturnRateUndefined === true" in block
    assert "hpElem.textContent = '--'" in block


def test_actual_section_writer_displays_undefined_rate_without_altering_amounts():
    extra = """const elements=Object.fromEntries(['value','pnl','daily','rate'].map(key=>[key,{style:{}}]));
    const document={getElementById:key=>elements[key]};
    const fmtCny=String, fmtSignedCny=String, setKpiLabel=()=>{};
    """ + function("writeSectionFinancials") + """
    writeSectionFinancials({...confirmedIbkrSummary,dailyPnl:3,accountReturnRateUndefined:true},'value','pnl','daily','rate');
    console.log(JSON.stringify(Object.fromEntries(Object.entries(elements).map(([key,value])=>[key,value.textContent]))));
    """
    data = payload()
    data["portfolio_account"].update(return_capital_cny=0, return_ratio=None)
    assert run(data, extra) == {"value": "693", "pnl": "203", "daily": "3", "rate": "--"}


def test_legacy_account_value_expression_is_unchanged_when_not_opted_in():
    bindings = "\n".join(re.search(r"const " + name + r" = [\s\S]*?;", SOURCE).group()
        for name in ("ibkrAccountValue", "ibkrDerivedTotalPnl", "ibkrAccountReturnRate"))
    data = payload()
    data.pop("daily_close")
    extra = """const useIbkrAccountTotals=true, ibkrHasFlexCashValue=true,
        ibkrPositionValue=1000, ibkrCashValue=-500, ibkrNetDeposit=100,
        ibkrHasAccountNetDeposit=true, ibkrSummary={totalValue:1,totalPnl:2,totalReturnRate:3};
    """ + bindings + "\nconsole.log(JSON.stringify([ibkrAccountValue,ibkrDerivedTotalPnl,ibkrAccountReturnRate]));"
    assert run(data, extra) == [500, 400, 4]


@pytest.mark.parametrize("amount", [693, 0, "693"])
def test_overview_uses_backend_cny_instead_of_current_browser_fx(amount):
    extra = function("fxRateForCurrency") + function("accountPrimaryBalanceCny")
    extra += "\nconst overview=" + json.dumps({"accounts": [{"account_code": "ibkr", "base_currency": "USD",
        "daily_close": {"status": "ok"}, "primary_balance": {"amount": 99, "currency": "USD", "amount_cny": amount}}]}) + ";"
    extra += "console.log(JSON.stringify(accountPrimaryBalanceCny(overview,'ibkr',{USD:99})));"
    assert run(payload(), extra)["amountCny"] == float(amount)


def test_supplemental_close_configuration_is_explicit_and_disabled_by_default():
    import yaml
    config = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    environment = config["services"]["pams-api"]["environment"]
    assert environment["PAMS_IBKR_DAILY_CLOSE_MANIFEST"] == "${PAMS_IBKR_DAILY_CLOSE_MANIFEST:-}"
    assert environment["IBKR_FLEX_POSITIONS_QUERY_ID"] == "${IBKR_FLEX_POSITIONS_QUERY_ID:-}"
    assert environment["IBKR_FLEX_TOKEN"] == "${IBKR_FLEX_TOKEN:-}"


@pytest.mark.parametrize("field,value", [("account_code", "other"), ("source_account_id", "OTHER"),
    ("source_sha256", "b" * 64), ("ownership_basis", "full_account"), ("owner_ratio", None),
    ("owner_ratio", -1), ("owner_ratio", False), ("as_of", "2024-01-01"), ("currency", "USD")])
def test_foreign_account_or_unbound_ownership_cannot_be_confirmed(field, value):
    data = payload()
    data["portfolio_account"][field] = value
    assert run(data) is None


@pytest.mark.parametrize("ratio,nav,pnl,capital,cash,rate", [
    (0, 0, 0, 0, 0, None), (0.25, 173.25, 50.75, 122.5, 175, 29 / 70), (1, 693, 203, 490, 700, 29 / 70)])
def test_personal_values_are_bound_once_without_using_full_broker_values(ratio, nav, pnl, capital, cash, rate):
    data = payload()
    data["portfolio_account"].update(owner_ratio=ratio, net_liquidation_cny=nav, total_pnl_cny=pnl,
        return_capital_cny=capital, cash_cny=cash, return_ratio=rate)
    result = run(data)
    assert result["totalValue"] == nav and result["totalPnl"] == pnl
    assert result["totalCost"] == capital and result["cashValue"] == cash
    assert result["totalReturnRate"] == rate
