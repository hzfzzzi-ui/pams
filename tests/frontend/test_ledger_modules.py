"""Run actual ledger controller, requests and editor against deferred synthetic reads/writes."""
from pathlib import Path
import subprocess
import os

import pytest


def test_ledger_modules_lifecycle_and_edit_actions():
    result = subprocess.run(
        ["node", "--experimental-vm-modules", "tests/frontend/ledger_modules.mjs"],
        cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_optional_ledger_lifecycle_in_browser():
    if not os.environ.get('PAMS_FRONTEND_PLAYWRIGHT_MODULE'):
        pytest.skip('Optional isolated browser runtime not configured')
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ['node', str(root / 'tests/frontend/ledger_modules_browser.mjs'), str(root / 'frontend/public')],
        cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
