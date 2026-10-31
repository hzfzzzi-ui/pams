"""Synthetic transaction UI lifecycle and exact write projection contracts."""
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_transactions_modules_fields_commands_and_lifecycle():
    result = subprocess.run(['node', '--experimental-vm-modules', str(ROOT / 'tests/frontend/transactions_modules.mjs')],
                            cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_optional_transactions_browser_lifecycle():
    if not os.environ.get('PAMS_FRONTEND_PLAYWRIGHT_MODULE'):
        pytest.skip('Optional isolated browser runtime not configured')
    result = subprocess.run(['node', str(ROOT / 'tests/frontend/transactions_lifecycle_browser.mjs'), str(ROOT / 'frontend/public')],
                            cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
