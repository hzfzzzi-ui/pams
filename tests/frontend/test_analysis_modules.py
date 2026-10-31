"""Exercise the actual analysis modules against deterministic deferred reads."""
from pathlib import Path
import os
import subprocess
import pytest


def test_analysis_module_lifecycle_and_read_order():
    result = subprocess.run(
        ["node", "--experimental-vm-modules", "tests/frontend/analysis_modules.mjs"],
        cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_optional_analysis_lifecycle_in_browser():
    if not os.environ.get("PAMS_FRONTEND_PLAYWRIGHT_MODULE"):
        pytest.skip("Set PAMS_FRONTEND_PLAYWRIGHT_MODULE for local isolated browser checks")
    repo = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["node", "tests/frontend/analysis_modules_browser.mjs", str(repo / "frontend/public")],
        cwd=repo, capture_output=True, text=True, timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
