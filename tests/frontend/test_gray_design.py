"""Ownership and semantic boundaries for the user-selected grayscale design."""
from pathlib import Path
import subprocess
import tinycss2

ROOT = Path(__file__).resolve().parents[2]
PUBLIC = ROOT / "frontend/public"


def test_shared_tokens_and_page_styles_have_single_owners():
    for name in ("app", "investments", "ledger", "more", "accounts", "data_sources", "account_import"):
        html = (PUBLIC / f"{name}.html").read_text(encoding="utf-8")
        assert html.count('href="assets/css/tokens.css?') == 1
        assert html.index('href="assets/css/tokens.css?') < html.index('href="assets/css/' + ("app-shell" if name == "app" else "style"))
        assert "fonts.googleapis.com" not in html
        assert "<style>" not in html
    for name in ("style", "design-system"):
        css = (PUBLIC / f"assets/css/{name}.css").read_text(encoding="utf-8")
        assert ":root" not in css
        assert ".investAnchorNav" not in css and ".ledgerAnchorNav" not in css
    for path in (PUBLIC / "assets/css/pages").glob("*.css"):
        assert not [rule for rule in tinycss2.parse_stylesheet(path.read_text(encoding="utf-8")) if rule.type == "error"]


def test_chart_theme_consumes_css_and_preserves_non_color_series_identity():
    script = r"""
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
globalThis.document = {documentElement: {}};
globalThis.getComputedStyle = () => ({getPropertyValue: name => name === '--pams-chart-series-1' ? '#eeeeee' : ''});
const source = readFileSync('frontend/public/assets/js/shared/chart_theme.js','utf8');
const theme = await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
assert.equal(theme.SERIES_COLORS[0], '#eeeeee');
assert.equal(theme.CHART_THEME.line, '#eeeeee');
for (const color of theme.SERIES_COLORS) {
  const values = color.slice(1).match(/../g).map(v => parseInt(v,16));
  assert.ok(Math.max(...values)-Math.min(...values)<=8, color);
}
assert.equal(new Set([0,1,2,3].map(i=>JSON.stringify(theme.seriesAppearance(i)))).size,4);
assert.ok(Object.isFrozen(theme.SERIES_COLORS));
"""
    result = subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
