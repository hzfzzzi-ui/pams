"""The More hub owns account navigation and audit tools."""
from pathlib import Path
import os
import shutil
import subprocess
import sys

import pytest
import tinycss2

ROOT = Path(__file__).resolve().parents[2]
PUBLIC = ROOT / "frontend/public"


def test_only_hub_scoped_style_and_confirmed_navigation():
    html = (PUBLIC / "more.html").read_text(encoding="utf-8")
    assert 'id="moreImport"' not in html
    assert 'class="statusbar"' not in html and "LIVE" not in html
    assert 'id="moreAdvanced" class="moreAdvanced">' in html
    assert 'id="moreIssues"' not in html
    css = (PUBLIC / "assets/css/more.css").read_text(encoding="utf-8")
    rules = tinycss2.parse_stylesheet(css, skip_comments=True, skip_whitespace=True)
    assert all(rule.type == "qualified-rule" and
               tinycss2.serialize(rule.prelude).strip().startswith("body.page-more") for rule in rules)


def test_more_uses_investment_tokens_without_new_font_or_financial_code():
    css = (PUBLIC / "assets/css/more.css").read_text(encoding="utf-8")
    rules = tinycss2.parse_stylesheet(css, skip_whitespace=True, skip_comments=True)
    assert all(rule.type == "qualified-rule" for rule in rules)
    assert "font-family: var(--pams-font-sans)" in css
    assert "background: var(--pams-bg-canvas)" in css
    assert "var(--pams-mobile-gutter)" in css
    assert "body.page-more.app-shell .moreHub { display: block; }" in css
    assert not any(value in css for value in ("@font-face", "@import", "url(", "vw", "#07090c"))
    html = (PUBLIC / "more.html").read_text(encoding="utf-8")
    assert "more.css" in html
    assert "more.js" in html
    assert 'data_sources.html' not in html
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
