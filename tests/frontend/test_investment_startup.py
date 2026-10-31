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
    chart_controller = (PUBLIC / "assets/js/features/investment/charts/controller.js").read_text(encoding="utf-8")
    requests = (PUBLIC / "assets/js/features/investment/charts/requests.js").read_text(encoding="utf-8")
    assert "navPromise = trackInvestmentNav(readInvestmentNav()" in chart_controller
    assert "jget('/nav/weekly?" in requests
    assert "afterInvestmentNav(() => loadInvestmentReturnChartOnce())" in source
    for name, kind, tab in (
        ("portfolio_risk.js", "risk", "risk"),
        ("portfolio_config.js", "config", "allocation"),
    ):
        text = (PUBLIC / "assets/js" / name).read_text(encoding="utf-8")
        assert "afterStartup: afterInvestmentStartup" in text
        assert f"./features/portfolio/{kind}_controller.js?v=20261031-panels-v1" in text
        controller = (PUBLIC / "assets/js/features/portfolio" / f"{kind}_controller.js").read_text(encoding="utf-8")
        assert f"tab: '{tab}'" in controller
        assert "afterStartup: context.afterStartup" in controller
    ui = (PUBLIC / "assets/js/ui.js").read_text(encoding="utf-8")
    assert "createPageBootstrap({afterStartup: afterInvestmentStartup, warmCache: warmApiCache})" in ui
    bootstrap = (PUBLIC / "assets/js/app/page_bootstrap.js").read_text(encoding="utf-8")
    assert "afterStartup(() => {" in bootstrap
    assert "if (!disposed) return callback();" in bootstrap
    assert "afterNav(() => {" in (PUBLIC / "assets/js/features/analysis/controller.js").read_text(encoding="utf-8")


def test_investment_entries_share_one_startup_and_ui_module_version():
    html = (PUBLIC / "investments.html").read_text(encoding="utf-8")
    for name in ("holdings", "holdings_categories", "lookthrough", "transactions", "portfolio_risk", "portfolio_config"):
        version = ("20261031-panels-v1" if name in ("portfolio_risk", "portfolio_config")
                   else "20261031-investment-charts-v1" if name == "holdings_categories" else "20261031-analysis-modules-v1" if name == "lookthrough" else "20261031-transactions-v1" if name == "transactions" else "20261031-ui-v1")
        assert f"assets/js/{name}.js?v={version}" in html
        text = (PUBLIC / "assets/js" / f"{name}.js").read_text(encoding="utf-8")
        if name == "lookthrough":
            controller = (PUBLIC / "assets/js/features/analysis/controller.js").read_text(encoding="utf-8")
            assert "../../components/bottom_sheet.js?v=20261031-ui-v1" in controller
        elif name != "portfolio_risk":
            assert "./ui.js?v=20261031-ui-v1" in text
        else:
            view = (PUBLIC / "assets/js/features/portfolio/risk_view.js").read_text(encoding="utf-8")
            assert "../../components/data_state.js?v=20261031-ui-v1" in view
    for name in ("ui", "holdings_categories", "lookthrough", "portfolio_risk", "portfolio_config"):
        text = (PUBLIC / "assets/js" / f"{name}.js").read_text(encoding="utf-8")
        if name == "lookthrough":
            text = (PUBLIC / "assets/js/features/analysis/controller.js").read_text(encoding="utf-8")
        assert re.findall(r"investment_startup\.js\?v=([^'\"]+)", text) == ["20261031-gray-v1"]
