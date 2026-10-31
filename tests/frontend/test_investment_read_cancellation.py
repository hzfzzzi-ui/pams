"""Cancellation regressions use only fixed synthetic data and loopback HTTP."""
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_investment_optional_read_cancellation():
    result = subprocess.run(['node', 'tests/frontend/investment_read_cancellation.mjs'],
                            cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_optional_native_investment_read_cancellation():
    if not os.environ.get('PAMS_FRONTEND_PLAYWRIGHT_MODULE'):
        pytest.skip('Optional isolated browser runtime not configured')
    result = subprocess.run(['node', 'tests/frontend/investment_read_cancellation_browser.mjs', 'frontend/public'],
                            cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
