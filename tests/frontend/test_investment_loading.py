"""Run the real browser modules with deterministic deferred network responses."""
from pathlib import Path
import subprocess


def test_investment_loading_lifecycle():
    result = subprocess.run(
        ["node", "--experimental-vm-modules", "tests/frontend/investment_loading.mjs"],
        cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_nav_starts_before_holdings_barrier_and_only_for_analysis():
    source = (Path(__file__).resolve().parents[2] / "frontend/public/assets/js/features/investment/holdings/summary_controller.js").read_text(encoding="utf-8")
    main = source[source.index("  context.bootstrap?.();"): ]
    assert main.index("bindInvestmentPageTabs();") < main.index("loadInvestmentNavChartOnce()")
    assert main.index("loadInvestmentNavChartOnce()") < main.index("instance.ready = load();")
    assert main.index("runWhenInvestmentAnalysisVisible") < main.index("instance.ready = load();")
    assert "if (document.body.dataset.investTab === 'analysis')" in main


def test_summary_consumers_use_the_same_versioned_module():
    root = Path(__file__).resolve().parents[2] / "frontend/public/assets/js"
    specifier = "import { loadInvestmentSummaryFresh } from './investment_requests.js?v=20261031-gray-v1';"
    for name, prefix in (("holdings_categories.js", "./"), ("features/analysis/requests.js", "../../")):
        source = (root / name).read_text(encoding="utf-8")
        assert specifier.replace("./investment_requests", prefix + "investment_requests") in source
        assert "/summary2/latest" not in source
