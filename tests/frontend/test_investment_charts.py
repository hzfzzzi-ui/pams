"""Run the actual investment chart controller with delayed reads and DOM controls."""
from pathlib import Path
import subprocess


def test_investment_chart_controller_lifecycle():
    result = subprocess.run(
        ["node", "--experimental-vm-modules", "tests/frontend/investment_chart_lifecycle.mjs"],
        cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
