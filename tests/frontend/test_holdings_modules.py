"""Synthetic lifecycle checks for the investment list and summary controllers."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import pytest

ROOT = Path(__file__).resolve().parents[2]

def test_optional_holdings_lifecycle_browser(tmp_path):
    if not os.environ.get('PAMS_FRONTEND_PLAYWRIGHT_MODULE'):
        pytest.skip('Optional offline browser runtime not configured')
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required')
    public = tmp_path / 'public'
    shutil.copytree(ROOT / 'frontend/public', public)
    subprocess.run([sys.executable, str(ROOT / 'frontend/tools/mobile_styles.py'), str(public / 'assets/css')],
                   cwd=ROOT, check=True, timeout=30)
    result = subprocess.run([node, str(ROOT / 'tests/frontend/holdings_lifecycle_browser.mjs'), str(public)],
                            cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
