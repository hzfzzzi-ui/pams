"""Import lifecycle checks use synthetic requests and never connect to PAMS."""
import os
from pathlib import Path
import shutil
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[2]

def run(name, timeout=60):
    executable = shutil.which('node')
    if not executable:
        pytest.skip('Node.js is required')
    result = subprocess.run([executable, str(ROOT / 'tests/frontend' / name)], cwd=ROOT,
                            capture_output=True, text=True, encoding='utf-8', timeout=timeout)
    assert result.returncode == 0, result.stdout + result.stderr

def test_import_read_scope_and_non_replayed_writes():
    run('account_import_request_scope.mjs')

def test_import_lifecycle_with_intercepted_browser():
    if not os.environ.get('PAMS_FRONTEND_PLAYWRIGHT_MODULE'):
        pytest.skip('Optional offline browser runtime not configured')
    run('account_import_lifecycle_browser.mjs', timeout=120)
