"""Execute the existing JS renderers offline; no API or production fixtures."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
JS = ROOT / "frontend/public/assets/js"
FX_LABEL = "\u8bc1\u5238\u6c47\u5151\u4f30\u7b97\uff08\u4eba\u6c11\u5e01\uff09"
ACCOUNT_LABEL = "\u8d26\u6237\u51c0\u6c47\u5151\uff08\u8bc1\u5238\u53ca\u73b0\u91d1\uff09"
PENDING = "\u5f85\u6838\u51c6"
PARTIAL = "\u90e8\u5206\uff0c\u5f85\u6838\u51c6"
SCRIPT = r"""
const fs = require('node:fs'), vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const before = JSON.stringify(input);
function load(file, globals) {
  let source = require('./tests/frontend/holdings_renderer_fixture.cjs').sourceFor(file)
    .replace(/^\uFEFF/, '').replace(/^import .*;\r?$/gm, '')
    .replace(/\r?\nbindEvents\(\);\s*observeAnalysisVisibility\(\);\s*$/, '')
    .split(/\r?\nmain\(\)\./)[0];
  if (file === 'lookthrough.js') {
    source = ['constants', 'state', 'legacy_model', 'charts', 'templates', 'view'].map(name =>
      fs.readFileSync('frontend/public/assets/js/features/analysis/' + name + '.js', 'utf8')
        .replace(/^import .*;\r?$/gm, '').replace(/^export \{.*\};\r?$/gm, '').replace(/^export /gm, '')
    ).join('\n') + `
      const analysisState = createAnalysisState(window);
      const model = createAnalysisLegacyModel({state: analysisState});
      const charts = createAnalysisCharts({root: document, state: analysisState, model});
      const templates = createAnalysisTemplates({state: analysisState, model});
      const view = createAnalysisView({root: document, state: analysisState, model, charts, templates});
      Object.assign(globalThis, model, templates, view);
      for (const key of ['returnAttributionData', 'activeReturnAttributionDimension']) {
        Object.defineProperty(globalThis, key, {get: () => analysisState[key], set: value => {analysisState[key] = value;}});
      }
    `;
  }
  const context = vm.createContext(globals);
  vm.runInContext(fs.readFileSync('frontend/public/assets/js/shared/chart_theme.js', 'utf8').replace(/^export /gm, ''), context);
  vm.runInContext("'use strict';\n" + source, context, {filename: file});
  return context;
}
function element() {
  return {innerHTML: '', textContent: '', style: {}, children: [], dataset: {}, listeners: {},
    classList: {toggle() {}}, appendChild(node) {this.children.push(node);},
    addEventListener(name, listener) {this.listeners[name] = listener;}};
}
const summaryElement = element(), badge = element();
const attribution = load('lookthrough.js', {
  window: {location: {hash: ''}}, payload: input.payload,
  dimension: input.dimension, holdingSummary: input.holdingSummary,
  document: {getElementById: id => ({returnAttributionSummary: summaryElement,
    returnAttributionBadge: badge})[id]},
});
vm.runInContext('returnAttributionData = payload; activeReturnAttributionDimension = dimension;' +
  'setReturnAttributionHoldingDailySummary(holdingSummary);', attribution);
const detail = attribution.returnAttributionDetailHtml(input.row);
const rowHtml = attribution.returnAttributionRowHtml(input.row, 0, 777);
const bridge = attribution.dailyFxAccountBridgeHtml(input.row);
attribution.renderReturnAttributionSummary();
const headline = summaryElement.innerHTML;
vm.runInContext('returnAttributionData = {...payload, daily_fx_attribution: undefined};', attribution);
attribution.renderReturnAttributionSummary();
const headlineUnchanged = headline === summaryElement.innerHTML;
const table = element(), mobile = element(), button = element();
button.dataset.mobileHoldingIndex = '0';
mobile.querySelectorAll = () => [button];
let sheet = '';
const holdings = load('holdings.js', {
  URLSearchParams, window: {location: {search: ''}, matchMedia: () => ({matches: false})},
  document: {createElement: element,
    getElementById: id => ({rows: table, mobileHoldingList: mobile})[id]},
  typeLabel: value => value, accountLabel: value => value, pnlPct: () => 0,
  openBottomSheet: (_title, html) => {sheet = html;},
});
holdings.renderRows([input.holding]);
button.listeners.click();
const noteParent = {note: null, querySelector() {return this.note;},
  appendChild(node) {this.note = node;}};
const safeNum = value => Number.isFinite(Number(value)) ? Number(value) : 0;
const categories = load('holdings_categories.js', {
  safeNum, fmt2: value => safeNum(value).toFixed(2),
  window: {addEventListener() {}},
  document: {createElement: element, getElementById: () => ({parentElement: noteParent})},
});
categories.setKpiFxNote('daily', 3);
const chips = categories.DailyReturnBreakdown([
  {label: 'USD holdings', priceDailyPnl: 10, fxDailyPnl: 3, dailyTotalPnl: 13},
]);
console.log(JSON.stringify({detail, rowHtml, bridge, sheet, headline,
  holdingFx: holdings.dailyFxText(input.holding), chips, note: noteParent.note.textContent,
  headlineUnchanged, unchanged: before === JSON.stringify(input)}));
"""


def row(**fields):
    return {
        "name": "Asset", "symbol": "TEST", "currency": "USD", "pnl_cny": 777,
        "market_value_cny": 1200, "daily_pnl_cny": 55, "daily_price_pnl_cny": 42,
        "daily_fx_pnl_cny": 9999, "exposure_weight": 0.1, "pnl_contribution": 0.2,
        "source_details": [{"name": "Existing source", "source_symbol": "TEST", "account": "ibkr", "pnl_cny": 777}],
        "fx_display": {"show": True, "status": "estimated", "value_cny": 13,
                       "label": FX_LABEL, "date_status": "not_independently_verified",
                       "scope": "instrument_only_not_account_net_fx"}, **fields,
    }


def account(**fields):
    return {
        "status": "confirmed", "status_label": "\u5df2\u786e\u8ba4",
        "as_of": "2026-10-27", "opening_as_of": "2026-10-26",
        "read_only": True, "affects_nav": False, "scope": "positions_and_signed_cash",
        "ownership_basis": "full_account",
        "net_fx_pnl_cny": "-1234.56",
        "summary": {"position_fx_pnl_cny": "-5000", "cash_fx_pnl_cny": "3000",
                    "net_fx_pnl_cny": "-2000", "price_pnl_cny": "70",
                    "income_pnl_cny": "14", "expense_pnl_cny": "-7",
                    "official_residual_cny": "0.02", "unexplained_cash_cny": "0.03"},
        "accrual_other_change_cny": "8.25", "official_total_pnl_cny": "-1000",
        "currencies": [{"currency": "USD", "status": "confirmed",
                        "opening_positions": "50000", "opening_cash": "-30000",
                        "position_fx_pnl_cny": "-5000", "cash_fx_pnl_cny": "3000",
                        "net_fx_pnl_cny": "-2000",
                        "opening_fx": {"as_of": "2026-10-26", "rate": "7.1", "source": "open-proof", "source_label": "Opening FX evidence"},
                        "closing_fx": {"as_of": "2026-10-27", "rate": "7", "source": "close-proof", "source_label": "Closing FX evidence"}}],
        "coverage": {}, "reasons": [], **fields,
    }


def render(*, item=None, bridge=None, dimension="holding", holding=None):
    item = row() if item is None else item
    payload = {"as_of": "2026-10-29", "summary": {"pnl_cny": 777, "daily_pnl_cny": 55},
               "dimensions": {"holding": [item]}, "daily_fx_attribution": bridge}
    holding = {
        "symbol": "TEST", "name": "Asset", "account": "ibkr", "currency": "USD",
        "major_category": "\u7f8e\u80a1", "minor_category": "\u7f8e\u80a1",
        "market_value_cny": 1200, "cost_cny": 1000, "holding_pnl_cny": 250,
        "daily_pnl_cny": 10, "daily_total_pnl_cny": 13, "daily_pnl_native": 1,
        "daily_fx_pnl_cny": 3, "daily_return_status": "confirmed",
        "quantity": 100, "current_price": 12, "previous_price": 11, "buy_price": 10,
        **(holding or {}),
    }
    node = shutil.which("node")
    assert node, "Node is required for real JS rendering"
    result = subprocess.run(
        [node, "-e", SCRIPT], cwd=ROOT, text=True, encoding="utf-8", capture_output=True, check=False,
        input=json.dumps({"payload": payload, "row": item, "dimension": dimension,
                          "holdingSummary": {"children": [{"symbol": "TEST", "daily_pnl": 55}]},
                          "holding": holding}), timeout=20,
    )
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["unchanged"], "Readonly rendering must not overwrite legacy amounts"
    assert output["headlineUnchanged"], "Account bridge must not enter the global headline"
    return output


def metric_value(html, label):
    match = re.search(rf"<strong>{re.escape(label)}</strong></div><div><b>(.*?)</b>", html)
    assert match, label
    return match.group(1)


@pytest.mark.parametrize("dimension", ["holding", "industry", "region", "theme", "underlying"])
def test_row_and_detail_use_server_fx_metadata_not_legacy_or_forced_zero(dimension):
    output = render(dimension=dimension)
    assert FX_LABEL in output["detail"] and FX_LABEL in output["rowHtml"]
    assert "+\u00a513.00" in output["detail"] and "+\u00a513.00" in output["rowHtml"]
    assert "+\u00a59,999.00" not in output["detail"]
    assert "\u4ef7\u683c\u6536\u76ca\uff08\u6298\u4eba\u6c11\u5e01\uff09" in output["detail"]
    assert "+\u00a542.00" in output["detail"] and "+\u00a542.00" in output["rowHtml"]
    for key in ("detail", "rowHtml"):
        assert 'title="\u4ef7\u683c\u6536\u76ca\u5f52\u56e0\u65e5\u671f\uff1a2026-10-29"' in output[key]
    assert "\u65e5\u671f\u672a\u72ec\u7acb\u6838\u9a8c" in output["detail"]
    assert "\u4ec5\u8bc1\u5238\uff0c\u4e0d\u542b\u73b0\u91d1" in output["detail"]


def test_cny_identity_hides_fx_cell_metadata_and_holding_legacy_fx():
    item = row(currency="CNY", fx_display={"show": False, "status": "identity", "value_cny": 0})
    output = render(item=item, holding={"currency": "CNY", "daily_fx_pnl_cny": 9999})
    assert "\u6c47\u5151" not in output["detail"] and "\u6c47\u5151" not in output["rowHtml"]
    assert "\u65e5\u671f\u672a\u72ec\u7acb\u6838\u9a8c" not in output["detail"]
    assert output["holdingFx"] == ""
    assert "\u8bc1\u5238\u6c47\u5151\u4f30\u7b97" not in output["sheet"]


@pytest.mark.parametrize("legacy", [None, 0, 9999])
def test_legacy_foreign_metadata_missing_is_pending_not_inferred_zero(legacy):
    item = row(daily_fx_pnl_cny=legacy)
    del item["fx_display"]
    output = render(item=item)
    assert FX_LABEL in output["detail"] and PENDING in output["detail"]
    assert "+\u00a50.00" not in output["detail"] and "+\u00a59,999.00" not in output["detail"]


def test_pending_foreign_split_and_price_are_not_zero():
    item = row(daily_price_pnl_cny=None)
    item["fx_display"].update(status="pending", value_cny=None)
    output = render(item=item, dimension="industry")
    assert output["detail"].count(PENDING) == 2
    assert "+\u00a50.00" not in output["detail"].split('<div class="sheetSection">', 1)[0]
    assert PENDING in output["rowHtml"]


def test_account_bridge_is_dated_after_sources_and_binds_net_without_summing():
    output = render(bridge=account())
    assert output["detail"].index("Existing source") < output["detail"].index(ACCOUNT_LABEL)
    assert metric_value(output["bridge"], ACCOUNT_LABEL) == "-\u00a51,234.56"
    assert metric_value(output["bridge"], "\u8bc1\u5238\u6c47\u5151") == "-\u00a55,000.00"
    assert metric_value(output["bridge"], "\u73b0\u91d1\u6c47\u5151\uff08\u542b\u878d\u8d44\u8d1f\u73b0\u91d1\uff09") == "+\u00a53,000.00"
    assert metric_value(output["bridge"], "\u5e94\u8ba1\u53ca\u5176\u4ed6 NAV \u53d8\u5316") == "+\u00a58.25"
    assert metric_value(output["bridge"], "\u5269\u4f59\u5dee\u989d\uff08\u5b98\u65b9\u6536\u76ca\u6865\u63a5\uff09") == "+\u00a50.02"
    assert "2026-10-26" in output["bridge"] and "2026-10-27" in output["bridge"]
    assert "2026-10-29" not in output["bridge"] and "\u4eca\u65e5" not in output["bridge"]
    assert "\u53ea\u8bfb\uff0c\u4e0d\u5f71\u54cd NAV" in output["bridge"]
    assert "Opening FX evidence" in output["bridge"] and "Closing FX evidence" in output["bridge"]
    assert "open-proof" not in output["bridge"] and "close-proof" not in output["bridge"]
    assert "-30000" in output["bridge"]
    assert ACCOUNT_LABEL not in output["headline"] and ACCOUNT_LABEL not in output["rowHtml"]


def test_partial_known_values_are_never_presented_as_confirmed():
    output = render(bridge=account(status="partial", status_label="\u5df2\u786e\u8ba4", net_fx_pnl_cny=None))
    assert metric_value(output["bridge"], ACCOUNT_LABEL) == PENDING
    assert PARTIAL in metric_value(output["bridge"], "\u8bc1\u5238\u6c47\u5151")
    assert "\u5df2\u786e\u8ba4" not in output["bridge"]


def test_pending_null_account_fields_are_not_green_zero():
    bridge = account(status="pending", net_fx_pnl_cny=None, currencies=[],
                     accrual_other_change_cny=None, official_total_pnl_cny=None)
    bridge["summary"] = {key: None for key in bridge["summary"]}
    output = render(bridge=bridge)
    assert output["bridge"].count(PENDING) >= 10
    assert "+\u00a50.00" not in output["bridge"]


@pytest.mark.parametrize("value", [None, "", "NaN", "Infinity", True])
def test_malformed_account_values_are_pending_not_coerced(value):
    output = render(bridge=account(net_fx_pnl_cny=value))
    assert metric_value(output["bridge"], ACCOUNT_LABEL) == PENDING


def test_instrument_and_account_fx_scopes_cannot_be_interchanged():
    item = row()
    item["fx_display"]["scope"] = "positions_and_signed_cash"
    output = render(item=item, bridge=account(scope="instrument_only_not_account_net_fx"))
    assert output["bridge"] == ""
    assert FX_LABEL in output["detail"] and PENDING in output["detail"]
    assert "+\u00a513.00" not in output["detail"]


def test_existing_holding_detail_and_category_notes_are_estimates():
    output = render()
    label = "\u8bc1\u5238\u6c47\u5151\u4f30\u7b97"
    assert output["holdingFx"] == "\u8bc1\u5238\u6c47\u5151 +\u00a53.00"
    assert f"<strong>{label}</strong>" in output["sheet"]
    assert re.search(rf"<strong>{label}</strong><span[^>]*>\+\u00a53.00</span>", output["sheet"])
    assert label in output["chips"] and label in output["note"]
    assert "+\u00a513.00" in output["chips"]


def test_account_currency_evidence_is_bounded_and_escaped():
    bridge = account(currencies=[{"currency": f"CC{index}", "status": "pending"} for index in range(33)])
    bridge["currencies"][0]["opening_fx"] = {"source_label": "<script>bad</script>", "as_of": "<bad>"}
    output = render(bridge=bridge)
    assert "CC31" in output["bridge"] and "CC32" not in output["bridge"]
    assert "<script>bad</script>" not in output["bridge"]
    assert "&lt;script&gt;bad&lt;/script&gt;" in output["bridge"]


@pytest.mark.parametrize("account_code", ["fund_platform", "esop", "", None])
def test_non_ibkr_cny_details_never_inherit_the_account_bridge(account_code):
    item = row(currency="CNY", account=account_code,
               source_details=[{"account": account_code, "name": "Other account"}],
               fx_display={"show": False, "status": "identity", "value_cny": 0})
    output = render(item=item, bridge=account())
    assert output["bridge"] == "" and ACCOUNT_LABEL not in output["detail"]


@pytest.mark.parametrize("direct,source", [
    ({"account": "ibkr"}, {}), ({"account_code": "ibkr"}, {}),
    ({}, {"account": "ibkr"}), ({}, {"account_code": " IBKR "}),
])
def test_explicit_ibkr_row_or_source_allows_readonly_bridge(direct, source):
    output = render(item=row(source_details=[source], **direct), bridge=account())
    assert ACCOUNT_LABEL in output["bridge"]
    assert "IBKR \u5168\u8d26\u6237" in output["bridge"]
    assert "\u72ec\u7acb\u65e5\u7ec8\u8bc1\u636e" in output["bridge"]
    assert 'data-daily-fx-account-bridge' in output["bridge"]
    assert 'data-daily-fx-field="net_fx_pnl_cny"' in output["bridge"]


def test_exact_account_bridge_values_remain_separate_from_instrument_estimate():
    bridge = account(net_fx_pnl_cny="-110.533266363120", accrual_other_change_cny="-62.98347")
    bridge["summary"].update(position_fx_pnl_cny="425.797174286382",
                             cash_fx_pnl_cny="-536.330440649502", official_residual_cny="-1.9e-9")
    output = render(bridge=bridge)
    assert metric_value(output["bridge"], ACCOUNT_LABEL) == "-\u00a5110.53"
    assert metric_value(output["bridge"], "\u8bc1\u5238\u6c47\u5151") == "+\u00a5425.80"
    assert "-1.9e-9 CNY" in output["bridge"]
    assert "-\u00a562.98" in output["bridge"]
    assert FX_LABEL in output["detail"] and "+\u00a513.00" in output["detail"]


@pytest.mark.parametrize("basis,label", [
    ("owner_adjusted", "\u672c\u4eba\u5f52\u5c5e"),
    ("full_account", "\u5168\u8d26\u6237"), (None, "\u5f52\u5c5e\u5f85\u6838\u51c6"),
])
def test_account_header_uses_backend_ownership_basis(basis, label):
    output = render(bridge=account(ownership_basis=basis))
    assert f"IBKR {label}" in output["bridge"]
    if basis != "full_account":
        assert "IBKR \u5168\u8d26\u6237" not in output["bridge"]


def test_fx_source_machine_enums_stay_api_only():
    bridge = account()
    for key in ("opening_fx", "closing_fx"):
        bridge["currencies"][0][key].update(
            source="flex_conversion_and_internal_exact_historical_currency_rate",
            source_label="IBKR\u65e5\u7ed3\u4e0e\u5185\u90e8\u5386\u53f2\u6c47\u7387",
        )
    output = render(bridge=bridge)
    assert "flex_conversion_and_" not in output["detail"]
    assert "IBKR\u65e5\u7ed3\u4e0e\u5185\u90e8\u5386\u53f2\u6c47\u7387" in output["bridge"]


def test_sidecar_has_no_financial_formulas_or_global_amount_writes():
    source = "\n".join((JS / "features/analysis" / f"{name}.js").read_text(encoding="utf-8") for name in ("legacy_model", "templates", "view"))
    sidecar = source.split("function dailyFxAccountBridgeHtml(row)", 1)[1].split("function returnAttributionDetailHtml", 1)[0]
    for forbidden in (".reduce(", "Math.", "parseFloat(", "Number(", "returnAttributionData =", "document.", "jget("):
        assert forbidden not in sidecar
    assert "bridge.net_fx_pnl_cny" in sidecar and "summary.official_residual_cny" in sidecar
    assert "useHoldingDaily ? 0" not in source
    assert "activeReturnAttributionDimension === 'holding' ? 0" not in source
    assert "dailyFxAccountBridgeHtml(" not in source.split("function renderReturnAttributionSummary()", 1)[1].split("function returnAttributionRowHtml", 1)[0]
    html = (ROOT / "frontend/public/investments.html").read_text(encoding="utf-8")
    for name in ("holdings.js", "holdings_categories.js", "lookthrough.js"):
        version = "20261031-modules-v1" if name in ("holdings.js", "holdings_categories.js") else "20261031-modules-v1" if name == "lookthrough.js" else "20261031-modules-v1"
        assert f'assets/js/{name}?v={version}' in html
