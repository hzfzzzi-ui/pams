"""The hub consumes server diagnostics, never computes financial classifications."""
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
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
