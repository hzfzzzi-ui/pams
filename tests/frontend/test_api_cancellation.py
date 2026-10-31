"""Exercise real HTTP helper behavior with deterministic fetch/XHR transports."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_api_cancellation_cache_timeout_and_write_independence():
    result = subprocess.run(
        ['node', '--experimental-vm-modules', 'tests/frontend/api_cancellation.mjs'],
        cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
