from pathlib import Path
import subprocess
import os
import shutil
import sys

import pytest


def test_shared_page_read_freshness():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ['node', 'tests/frontend/page_read_freshness.mjs'], cwd=root,
        capture_output=True, text=True, encoding='utf-8', timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_optional_page_read_freshness_browser(tmp_path):
    if not os.environ.get('PAMS_FRONTEND_PLAYWRIGHT_MODULE'):
        pytest.skip('Explicit offline browser runtime required')
    root = Path(__file__).resolve().parents[2]
    public = tmp_path / 'public'
    shutil.copytree(root / 'frontend/public', public)
    subprocess.run([sys.executable, str(root / 'frontend/tools/mobile_styles.py'), str(public / 'assets/css')],
                   check=True, timeout=30)
    result = subprocess.run(['node', 'tests/frontend/page_read_freshness_browser.mjs', str(public)],
                            cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
