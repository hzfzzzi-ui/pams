"""Read-only portfolio panel boundaries, using explicit isolated fixtures."""
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_portfolio_read_lifecycle_and_request_scope():
    result = subprocess.run(
        ['node', str(ROOT / 'tests/frontend/portfolio_read_session.mjs')],
        cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_optional_portfolio_panel_browser_behaviors():
    if not os.environ.get('PAMS_FRONTEND_PLAYWRIGHT_MODULE'):
        pytest.skip('Optional isolated browser runtime not configured')
    result = subprocess.run(
        ['node', str(ROOT / 'tests/frontend/portfolio_panels_browser.mjs'), str(ROOT / 'frontend/public')],
        cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
