"""Lifecycle checks with synthetic fixtures and no production API access."""
import os
from pathlib import Path
import subprocess
import shutil
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_low_frequency_feature_lifecycle():
    result = subprocess.run(
        ['node', '--experimental-vm-modules', str(ROOT / 'tests/frontend/feature_modules.mjs')],
        cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_optional_low_frequency_lifecycle_in_browser(tmp_path):
    if not os.environ.get('PAMS_FRONTEND_PLAYWRIGHT_MODULE'):
        pytest.skip('Optional isolated browser runtime not configured')
    public = tmp_path / 'public'
    shutil.copytree(ROOT / 'frontend/public', public)
    subprocess.run([sys.executable, 'frontend/tools/mobile_styles.py', str(public / 'assets/css')], cwd=ROOT, check=True)
    result = subprocess.run(
        ['node', str(ROOT / 'tests/frontend/feature_lifecycle_browser.mjs'), str(public)],
        cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
