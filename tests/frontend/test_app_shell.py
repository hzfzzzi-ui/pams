"""The shell owns navigation; page documents retain their existing business controllers."""
import os
from pathlib import Path
import subprocess
import shutil
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
PUBLIC = ROOT / "frontend/public"


def test_single_navigation_and_removed_page_headers():
    host = (PUBLIC / "app.html").read_text(encoding="utf-8")
    assert host.count('aria-label="主导航"') == 1
    assert host.count('data-page=') == 3
    for name in ("investments", "ledger", "more", "accounts", "data_sources", "account_import"):
        html = (PUBLIC / f"{name}.html").read_text(encoding="utf-8")
        assert "data-nav" not in html
        assert "page-shell.css?v=" in html
    ledger = (PUBLIC / "ledger.html").read_text(encoding="utf-8")
    for removed in ('<h1>流水</h1>', '现金流、账户流水与内部转账。', '<summary>口径说明</summary>', '>资金流转匹配</a>'):
        assert removed not in ledger
    assert '<h1>更多</h1>' not in (PUBLIC / "more.html").read_text(encoding="utf-8")
    investment = (PUBLIC / "investments.html").read_text(encoding="utf-8")
    assert investment.index('class="investAnchorNav"') < investment.index('id="investment-conclusion"')


def test_optional_shell_browser(tmp_path):
    if not os.environ.get("PAMS_FRONTEND_PLAYWRIGHT_MODULE"):
        pytest.skip("Optional isolated browser runtime not configured")
    public = tmp_path / "public"
    shutil.copytree(PUBLIC, public)
    subprocess.run([sys.executable, "frontend/tools/mobile_styles.py", str(public / "assets/css")], cwd=ROOT, check=True)
    subprocess.run(["node", "tests/frontend/frontend_shell.mjs", str(public)], cwd=ROOT, check=True, timeout=120)
