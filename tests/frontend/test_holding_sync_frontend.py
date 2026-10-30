"""Exercise timestamp provenance and late hero initialization in the real JS."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = r"""
const fs = require('node:fs'), vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const before = JSON.stringify(input);
function load(file, globals) {
  const source = fs.readFileSync('frontend/public/assets/js/' + file, 'utf8')
    .replace(/^\uFEFF/, '').replace(/^import .*;\r?$/gm, '').split(/\r?\nmain\(\)\./)[0];
  const ctx = vm.createContext(globals); vm.runInContext(source, ctx); return ctx;
}
const rows = load('holdings.js', {
  URLSearchParams, window: {location: {search: ''}, matchMedia: () => ({matches: false})},
});
const rendered = rows.holdingSyncTime(input.row);
let choice = '\u5168\u90e8', events = {};
const hero = {innerHTML: '', classList: {toggle() {}}};
const ctx = load('holdings_categories.js', {
  safeNum: v => Number(v) || 0, fmt2: v => Number(v).toFixed(2),
  document: {getElementById: id => id === 'holdingGroupFilter' ? {value: choice} : hero},
  window: {addEventListener: (name, fn) => {events[name] = fn;}},
});
choice = '\u7f8e\u80a1/\u6d77\u5916';
events['pams:holding-group-change']({detail: {group: choice}});
vm.runInContext(`mobileHeroContext = {asOf:'2026-10-28', profiles: {
  '\u5168\u90e8': {identity: 'ALL', value: 100, miniMetrics: []},
  '\u7f8e\u80a1/\u6d77\u5916': {identity: 'IBKR', value: 100, miniMetrics: []}
}}; updateMobilePortfolioHero();`, ctx);
const late = hero.innerHTML;
choice = '\u5168\u90e8'; ctx.updateMobilePortfolioHero();
console.log(JSON.stringify({rendered, late, all: hero.innerHTML, quantity: rows.holdingQuantityText(input.row), unchanged: before === JSON.stringify(input)}));
"""


def run(row, field="rendered"):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for real JS execution")
    result = subprocess.run(
        [node, "-e", SCRIPT], input=json.dumps({"row": row}), text=True,
        encoding="utf-8", capture_output=True, cwd=ROOT, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["unchanged"]
    assert "IBKR" in output["late"]
    assert "ALL" in output["all"]
    assert "mobileHeroMetaTop" not in output["late"]
    return output[field]


@pytest.mark.parametrize("stamp", [None, "", "2026-10-28", "2026-10-28T20:30:00", "invalid", "2026-13-28T20:30:00Z"])
def test_unknown_is_not_replaced_by_valuation_date_or_browser_time(stamp):
    html = run({"valuation_updated_at": stamp, "as_of": "2026-10-28", "updated_at": "2026-10-29T00:00:00Z"})
    assert "\u65f6\u95f4\u672a\u77e5" in html
    assert "<time" not in html


@pytest.mark.parametrize("stamp", ["2026-10-28T20:30:17.123456+00:00", "2026-10-29T04:30:17+08:00", "2026-10-28T20:30:17Z"])
def test_explicit_zones_render_in_beijing(stamp):
    html = run({"valuation_updated_at": stamp})
    assert "10-29 04:30" in html
    assert "\u5317\u4eac\u65f6\u95f4" in html
    assert ">\u540c\u6b65 " not in html


def test_merged_fund_uses_oldest_account_and_never_hides_missing_provenance():
    rows = [{"valuation_updated_at": "2026-10-28T20:30:00Z"}, {"valuation_updated_at": "2026-10-28T17:10:00Z"}]
    assert "10-29 01:10" in run({"__accountRows": rows})
    assert "<time" not in run({"__accountRows": rows + [{}]})


@pytest.mark.parametrize("quantity,expected", [(10, "10"), ("20.000000", "20"), ("20.010", "20.01"), ("75.010", "75.01"), (20.1, "20.1"), ("32665.908748", "32,665.909"), ("0.001", "0.001"), (None, "--"), (0, "--"), ("invalid", "--")])
def test_quantity_trims_only_padding_zeros_at_existing_precision(quantity, expected):
    assert run({"quantity": quantity}, "quantity") == expected
