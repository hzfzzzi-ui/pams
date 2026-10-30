"""Pending-issues view contracts and optional offline browser coverage."""
from pathlib import Path
import os
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_entry_and_full_diagnostics_remain_separate():
    more = (ROOT / 'frontend/public/more.html').read_text(encoding='utf-8')
    page = (ROOT / 'frontend/public/data_sources.html').read_text(encoding='utf-8')
    script = (ROOT / 'frontend/public/assets/js/data_sources.js').read_text(encoding='utf-8')
    assert 'href="data_sources.html?view=issues#dataQuality"' in more
    assert 'href="data_sources.html"' in more
    assert 'id="issuesView" hidden' in page and 'id="sourceList"' in page and 'id="syncMatrix"' in page
    assert "new URLSearchParams(window.location.search).get('view') === 'issues'" in script
    assert "jget('/api/accounts/overview').catch(() => null)" in script
    assert 'loadSyncCenter({' in script


def test_quality_warning_rendering():
    subprocess.run(['node', '--experimental-vm-modules', str(ROOT / 'tests/frontend/frontend_quality_warnings.mjs')],
                   cwd=ROOT, check=True, capture_output=True, text=True, encoding='utf-8')


def test_optional_pending_issues_browser():
    if not os.environ.get('PAMS_FRONTEND_PLAYWRIGHT_MODULE'):
        pytest.skip('Optional offline browser runtime not configured')
    subprocess.run(['node', str(ROOT / 'tests/frontend/frontend_pending_issues.mjs')], cwd=ROOT,
                   check=True, capture_output=True, text=True, encoding='utf-8', timeout=90)
