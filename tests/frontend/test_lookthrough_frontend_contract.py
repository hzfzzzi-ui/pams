from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PUBLIC_ROOT = REPO_ROOT / "frontend" / "public"


def _read_public(path: str) -> str:
    return (PUBLIC_ROOT / path).read_text(encoding="utf-8")


def test_lookthrough_frontend_uses_separate_exposure_tabs() -> None:
    js = ("\n".join(_read_public(path) for path in ("assets/js/features/analysis/constants.js", "assets/js/features/analysis/state.js", "assets/js/features/analysis/legacy_model.js", "assets/js/features/analysis/charts.js", "assets/js/features/analysis/templates.js", "assets/js/features/analysis/view.js", "assets/js/features/analysis/requests.js", "assets/js/features/analysis/controller.js", "assets/js/features/analysis/page.js")))

    for label in ["资产类型", "行业", "主题暴露", "市种", "地区", "币种", "底层暴露"]:
        assert label in js

    assert "风险桶" not in js
    assert "risk_bucket" not in js
    assert "const CHART_DIMENSIONS = new Set(['asset_type', 'industry', 'market', 'region', 'currency']);" in js
    assert "主题可重叠，一只持仓可以同时属于多个主题，因此主题暴露合计可能超过 100%。" in js
    assert "themeFamilyFilters" in js
    assert "underlyingExposureTable" in js
    assert "if (state.activeDimension === 'market') return rows.length;" in js
    assert "INDUSTRY_EXCLUDED_ASSET_TYPES" in js
    assert "renderIndustryExcludedSummary" in js
    assert "exposureRows('asset_type')" in js
    assert "行业图外合计" in js
    assert "敞口占比" in js
    assert "AI 算力链详情" in js
    assert "Top底层" in js
    assert "['underlying_asset'" not in js


def test_return_attribution_frontend_contract() -> None:
    js = ("\n".join(_read_public(path) for path in ("assets/js/features/analysis/constants.js", "assets/js/features/analysis/state.js", "assets/js/features/analysis/legacy_model.js", "assets/js/features/analysis/charts.js", "assets/js/features/analysis/templates.js", "assets/js/features/analysis/view.js", "assets/js/features/analysis/requests.js", "assets/js/features/analysis/controller.js", "assets/js/features/analysis/page.js")))
    html = _read_public("investments.html")

    assert "收益归因" in html
    assert "资产底表" in html
    assert "analysisSwitch" in html
    assert "data-analysis-panel-switch" in html
    assert "data-analysis-panel-page" in html
    assert "return-attribution" in html
    assert "asset-master-panel" in html
    assert "/api/analysis/return-attribution?baseCurrency=CNY" in js
    assert "pams.analysis.panel" in js
    assert "pams:holdings-kpi-ready" not in js
    assert "calibratedReturnAttributionData" in js
    assert "renderAssetMaster" in js
    assert "asset_master" in js
    assert "fund_underlying_top10" in js
    assert "assetMasterFundHolding" in js
    assert "字段契约" in js
    assert "持仓页最终 KPI" not in js
    assert "不按总额比例缩放" not in js
    assert "maxAbsPnl" in js
    assert "scaleReturnRow" not in js
    assert "isClosedAttributionRow" in js
    assert "returnAttributionShowAll" in js
    assert "RETURN_ATTRIBUTION_DEFAULT_LIMIT = 5" in js
    assert "data-return-attr-more" in js
    assert "更多 ${hiddenCount} 项" in js
    assert "data-return-attr-closed-row" not in js
    assert "已清仓收益归因" not in js
    for label in ["持仓", "行业", "主题", "地区", "币种", "底层", "资产类型", "市种"]:
        assert label in js
    assert "主题按 theme_score 加权且可重叠" in js
    assert "基金收益按底层权重分摊" in js


def test_lookthrough_frontend_has_required_supporting_styles() -> None:
    css = _read_public("assets/css/style.css")

    for cls in [
        ".lookthroughNarrative",
        ".lookthroughNarrativeMetrics",
        ".lookthroughNarrativeMetric",
        ".themeFamilyFilters",
        ".underlyingExposureTable",
        ".underlyingExposureRow",
        ".aiSubThemeGrid",
        ".aiSubThemeCard",
        ".returnAttributionCards",
        ".returnAttributionRow",
        ".returnAttributionPnlBar",
        ".returnAttributionMoreBtn",
        ".assetMasterCards",
        ".assetMasterIssueRow",
        ".assetMasterContractRow",
        ".assetMasterFund",
        ".assetMasterFundHolding",
        ".analysisSwitch",
        ".analysisPanelPage[hidden]",
    ]:
        assert cls in css
    assert ".returnAttributionClosedBlock" not in css
    assert ".riskSummaryBlock" not in css
    assert ".lookthroughTwinGrid" in css
