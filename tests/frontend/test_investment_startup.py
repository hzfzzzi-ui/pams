"""Exercise the actual startup coordinator and pin only its integration points."""
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[2]
PUBLIC = ROOT / "frontend/public"


def test_investment_startup_lifecycle():
    result = subprocess.run(
        ["node", "--experimental-vm-modules", "tests/frontend/investment_startup.mjs"],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_startup_hooks_do_not_change_api_or_rendering_contracts():
    source = (PUBLIC / "assets/js/holdings_categories.js").read_text(encoding="utf-8")
    assert "await trackInvestmentHoldings(afterInvestmentNav(() => Promise.all([" in source
    assert "]), 'holdings'));" in source
    assert "investmentNavChartPromise = trackInvestmentNav(jget('/nav/weekly?" in source
    assert "afterInvestmentNav(() => loadInvestmentReturnChartOnce())" in source
    for name, tab, function in (
        ("portfolio_risk.js", "risk", "loadPortfolioRiskDisclosure"),
        ("portfolio_config.js", "allocation", "loadPortfolioConfig"),
    ):
        text = (PUBLIC / "assets/js" / name).read_text(encoding="utf-8")
        assert f"afterInvestmentStartup({function}, '{tab}')" in text
    ui = (PUBLIC / "assets/js/ui.js").read_text(encoding="utf-8")
    assert ui.count("scheduleIdle(() => afterInvestmentStartup(") == 2
    assert "afterInvestmentNav(() => {" in (PUBLIC / "assets/js/lookthrough.js").read_text(encoding="utf-8")


def test_investment_entries_share_one_startup_and_ui_module_version():
    html = (PUBLIC / "investments.html").read_text(encoding="utf-8")
    for name in ("holdings", "holdings_categories", "lookthrough", "transactions", "portfolio_risk", "portfolio_config"):
        version = "20261031-gray-v1"
        assert f"assets/js/{name}.js?v={version}" in html
        text = (PUBLIC / "assets/js" / f"{name}.js").read_text(encoding="utf-8")
        assert "./ui.js?v=20261031-gray-v1" in text
    for name in ("ui", "holdings_categories", "lookthrough", "portfolio_risk", "portfolio_config"):
        text = (PUBLIC / "assets/js" / f"{name}.js").read_text(encoding="utf-8")
        assert re.findall(r"investment_startup\.js\?v=([^'\"]+)", text) == ["20261031-gray-v1"]
