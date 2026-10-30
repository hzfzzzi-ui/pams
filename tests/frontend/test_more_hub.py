"""The hub consumes server diagnostics, never computes financial classifications."""
from pathlib import Path
import os
import re
import shutil
import subprocess
import sys

import pytest
import tinycss2

ROOT = Path(__file__).resolve().parents[2]
PUBLIC = ROOT / "frontend/public"


def test_server_issue_summary_and_unknown_are_distinct():
    source = (PUBLIC / "assets/js/more.js").read_text(encoding="utf-8")
    source = re.sub(r"^import .*?;\s*", "", source, flags=re.M)
    script = "import assert from 'node:assert/strict';\n" + source.replace("export function", "function") + """
assert.equal(issueSummary({data_quality:{summary:{status:'ok',error_count:0,warning_count:0}}}),null);
assert.equal(issueSummary({data_quality:{summary:{status:'warning',error_count:0,warning_count:2}}}),'错误 0 · 警告 2');
for(const value of [null,{}, {data_quality:{summary:{status:'ok'}}}]) assert.throws(()=>issueSummary(value));
"""
    result = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr


def test_only_hub_scoped_style_and_confirmed_navigation():
    html = (PUBLIC / "more.html").read_text(encoding="utf-8")
    assert html.count('href="account_import.html"') == 1
    assert 'class="statusbar"' not in html and "LIVE" not in html
    assert 'id="moreAdvanced" class="moreAdvanced">' in html
    assert 'id="moreIssues"' in html and 'hidden>' in html
    css = (PUBLIC / "assets/css/more.css").read_text(encoding="utf-8")
    assert all(rule.strip().startswith("body.page-more") for rule in css.split("\n") if rule.strip())


def test_more_uses_investment_tokens_without_new_font_or_financial_code():
    css = (PUBLIC / "assets/css/more.css").read_text(encoding="utf-8")
    rules = tinycss2.parse_stylesheet(css, skip_whitespace=True, skip_comments=True)
    assert all(rule.type == "qualified-rule" for rule in rules)
    assert "font-family: var(--pams-font-sans)" in css
    assert "background: var(--pams-bg-canvas)" in css
    assert "var(--pams-mobile-gutter, 10px)" in css
    assert "body.page-more.app-shell .moreHub { display: block; }" in css
    assert not any(value in css for value in ("@font-face", "@import", "url(", "vw", "#07090c"))
    html = (PUBLIC / "more.html").read_text(encoding="utf-8")
    assert "more.css?v=20261029-investment-language-v1" in html
    assert "more.js?v=20261029-hub-v1" in html
    assert html.count('class="moreRow" href=') == 4
    assert 'href="research.html"' not in html


def test_optional_more_typography_and_interactions_in_real_browser(tmp_path):
    if not os.environ.get("PAMS_FRONTEND_PLAYWRIGHT_MODULE"):
        pytest.skip("Optional offline browser runtime not configured")
    root = tmp_path / "public"
    shutil.copytree(PUBLIC, root)
    subprocess.run([sys.executable, str(ROOT / "frontend/tools/mobile_styles.py"), str(root / "assets/css")],
                   check=True, capture_output=True, text=True)
    result = subprocess.run(["node", str(ROOT / "tests/frontend/frontend_more_language.mjs"), str(root)],
                            cwd=ROOT, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
