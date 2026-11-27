"""Regression tests for Batch B frontend formatting, privacy, picker, and navigation fixes."""
import shutil
import subprocess
from pathlib import Path
from unittest import TestCase, skipUnless

ROOT = Path(__file__).resolve().parents[2]
JS_ROOT = ROOT / "frontend" / "public" / "assets" / "js"
CSS_PATH = ROOT / "frontend" / "public" / "assets" / "css" / "pams-refresh.css"
ACCOUNTS_HTML = ROOT / "frontend" / "public" / "accounts.html"


class BatchBFrontendFixesTest(TestCase):
    @skipUnless(shutil.which("node"), "node is required for JS module behavioral assertions")
    def test_format_currency_preserves_negative_sign_and_empty_bank_wealth_profile(self):
        script = f"""
import assert from 'node:assert/strict';
import {{ formatCurrency as utilsFmt }} from {(JS_ROOT / 'utils.js').as_uri()!r};
import {{ formatCurrency as summaryFmt, makeMobileHeroProfiles }} from {(JS_ROOT / 'features/investment/holdings/summary_legacy_model.js').as_uri()!r};
import {{ buildAccountCashRows }} from {(JS_ROOT / 'features/investment/holdings/list_legacy_model.js').as_uri()!r};

assert.equal(utilsFmt(-11303.84, 'CNY'), '-¥11,303.84');
assert.equal(utilsFmt(-11303.84, 'CNY', {{ signed: true }}), '-¥11,303.84');
assert.equal(utilsFmt(100, 'CNY'), '¥100.00');
assert.equal(summaryFmt(-11303.84, 'CNY'), '-¥11,303.84');
assert.equal(summaryFmt(-25.5, 'USD'), '-$25.50');

const profiles = makeMobileHeroProfiles({{
  summary: {{}},
  categories: [],
  accountCards: {{ investment_overview: {{ net_value: {{ value: '837050.86' }} }} }},
  accountOverview: {{ accounts: [] }},
  fxRates: {{}},
  aStockRowsSummary: {{}},
  aStockSummary: {{}},
  ibkrSummary: {{}},
  preciousSummary: {{}},
  esopSummary: {{}},
}});
assert.ok(profiles['银行理财']);
assert.equal(profiles['银行理财'].value, 0);

// IBKR fallback cash_base is already converted to CNY and must not be labeled USD.
const {{ rows }} = buildAccountCashRows({{
  by_account: [{{ account_code: 'ibkr', portfolio_value: {{ cash_base: '-186126.30', base_currency: 'USD', status: 'official_close' }} }}],
}}, []);
assert.equal(rows.length, 1);
assert.equal(rows[0].currency, 'CNY');
"""
        proc = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)

    def test_static_contracts_for_accounts_css_privacy_selectors_and_tabs(self):
        self.assertIn("assets/css/pams-refresh.css", ACCOUNTS_HTML.read_text(encoding="utf-8"))
        css = CSS_PATH.read_text(encoding="utf-8")
        for token in (
            "body.is-privacy-masked",
            "#accountNetworthDebtsPanel strong",
            ".monthlyOverview .metricTabs b",
            ".monthlyOverview .plotRow .rowValue",
            ".pamsIconPicker:has(> .pamsIconMenu:not([hidden]))",
        ):
            self.assertIn(token, css)

        analysis_ctrl = (JS_ROOT / "features/analysis/controller.js").read_text(encoding="utf-8")
        self.assertIn('[data-invest-tab="transactions"]', analysis_ctrl)

        ledger_tpl = (JS_ROOT / "features/ledger/templates.js").read_text(encoding="utf-8")
        self.assertIn("RAW_FLOW_TYPE_LABELS", ledger_tpl)
        self.assertIn("expense: '支出'", ledger_tpl)

        shell_js = (JS_ROOT / "app_shell.js").read_text(encoding="utf-8")
        self.assertIn("toggleAttribute('hidden'", shell_js)
        self.assertIn("pams-privacy", shell_js)
        self.assertIn("pams.api.cache.", shell_js)
