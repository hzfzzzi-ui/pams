from pathlib import Path
import subprocess


def test_quality_panel_displays_backend_warnings():
    root = Path(__file__).resolve().parents[1]
    subprocess.run(
        ["node", "--experimental-vm-modules", "tests/frontend_quality_warnings.mjs"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
