"""Run the real browser modules with deterministic deferred network responses."""
from pathlib import Path
import subprocess


def test_investment_loading_lifecycle():
    result = subprocess.run(
        ["node", "--experimental-vm-modules", "tests/investment_loading.mjs"],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_nav_starts_before_holdings_barrier_and_only_for_analysis():
    source = (Path(__file__).resolve().parents[1] / "frontend/public/assets/js/holdings_categories.js").read_text(encoding="utf-8")
    main = source[source.index("async function main(){"):]
    assert main.index("bindInvestmentPageTabs();") < main.index("loadInvestmentNavChartOnce()")
    assert main.index("loadInvestmentNavChartOnce()") < main.index("await trackInvestmentHoldings")
    assert main.index("runWhenInvestmentAnalysisVisible") < main.index("await trackInvestmentHoldings")
    assert "if (document.body.dataset.investTab === 'analysis')" in main


def test_summary_consumers_use_the_same_versioned_module():
    root = Path(__file__).resolve().parents[1] / "frontend/public/assets/js"
    specifier = "import { loadInvestmentSummaryFresh } from './investment_requests.js?v=20261026-nav-independent-v1';"
    for name in ("holdings_categories.js", "lookthrough.js"):
        source = (root / name).read_text(encoding="utf-8")
        assert specifier in source
        assert "/summary2/latest" not in source
