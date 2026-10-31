"""Shared presentation lifecycle; no financial calculation or production API use."""
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_ui_modules_startup_routes_and_disposal():
    result = subprocess.run(
        ["node", str(ROOT / "tests/frontend/ui_modules.mjs")],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_optional_bottom_sheet_focus_in_browser():
    if not os.environ.get("PAMS_FRONTEND_PLAYWRIGHT_MODULE"):
        pytest.skip("Optional isolated browser runtime not configured")
    result = subprocess.run(
        ["node", str(ROOT / "tests/frontend/bottom_sheet_browser.mjs"), str(ROOT / "frontend/public")],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
