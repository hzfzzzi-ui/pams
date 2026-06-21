from html.parser import HTMLParser
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_ROOT = REPO_ROOT / "frontend" / "public"


def _read_public(path: str) -> str:
    return (PUBLIC_ROOT / path).read_text(encoding="utf-8")


class _StaticHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.stylesheets: list[str] = []
        self.scripts: list[str] = []
        self.body_classes: list[str] = []
        self.data_navs: list[dict[str, str]] = []
        self._active_data_nav_index: int | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {key: value or "" for key, value in attrs}
        if tag == "link" and attr.get("rel") == "stylesheet":
            self.stylesheets.append(attr.get("href", ""))
        if tag == "script" and attr.get("src"):
            self.scripts.append(attr["src"])
        if tag == "body":
            self.body_classes = attr.get("class", "").split()
        if tag == "a" and "data-nav" in attr:
            self.data_navs.append({"href": attr.get("href", ""), "text": ""})
            self._active_data_nav_index = len(self.data_navs) - 1

    def handle_data(self, data: str) -> None:
        if self._active_data_nav_index is not None:
            self.data_navs[self._active_data_nav_index]["text"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._active_data_nav_index = None


def _parse_html(html: str) -> _StaticHtmlParser:
    parser = _StaticHtmlParser()
    parser.feed(html)
    return parser


def _formal_html_pages() -> list[str]:
    return sorted(path.name for path in PUBLIC_ROOT.glob("*.html"))


def test_data_sources_page_exposes_portfolio_data_quality_panel() -> None:
    """Guard the data configuration page's unified data-quality summary."""

    html = _read_public("data_sources.html")
    js = _read_public("assets/js/data_sources.js")

    assert 'id="dataQuality"' in html
    assert "/api/portfolio/data-quality?baseCurrency=CNY" in js
    assert "/api/portfolio/state?baseCurrency=CNY" not in js
    assert "data_quality" in js
    assert "????" not in js

    for label in [
        "覆盖范围",
        "最新日期",
        "缺数据",
        "行动提示",
        "账户余额",
        "投资持仓",
        "汇率",
        "补录账户余额",
        "当前无阻塞动作",
    ]:
        assert label in js

    for href in [
        "ledger.html#accountIssueStrip",
        "ledger.html#transfers",
        "investments.html",
        "investments.html#investment-ledger",
        "ledger.html",
        "data_sources.html#dataQuality",
    ]:
        assert href in js


def test_frontend_shell_removed_components_stay_removed() -> None:
    css = _read_public("assets/css/style.css")
    ui_js = _read_public("assets/js/ui.js")

    assert "appActionRow" not in css
    assert "riskAnchorNav" not in css
    mobile_tabs = ui_js.split("const MOBILE_TABS = {", 1)[1].split("};", 1)[0]
    assert "'investments.html': { key: 'invest', label: '投资' }" in mobile_tabs
    assert "'ledger.html': { key: 'ledger', label: '收支' }" in mobile_tabs
    assert "'more.html': { key: 'more', label: '更多' }" in mobile_tabs
    assert "index.html" not in mobile_tabs
    assert "accounts.html" not in mobile_tabs
    assert "accounts: 'more.html'" in ui_js
    assert "'accounts.html': 'more.html'" in ui_js
    assert "index: 'investments.html'" in ui_js
    assert "'index.html': 'investments.html'" in ui_js
    assert "'risk.html': 'investments.html'" in ui_js
    assert "const moreActive = new Set(['more.html', 'accounts.html', 'data_sources.html', 'research.html'])" in ui_js
    assert "const primary = new Set(['investments.html', 'ledger.html', 'more.html'])" in ui_js

    for page in [
        "accounts.html",
        "investments.html",
        "ledger.html",
    ]:
        html = _read_public(page)
        assert "appActionRow" not in html


def test_frontend_design_system_layer_is_loaded_after_legacy_styles() -> None:
    """Guard the shared design-system layer so new pages do not bypass it."""

    legacy_css = _read_public("assets/css/style.css")
    design_css = _read_public("assets/css/design-system.css")
    design_doc = (REPO_ROOT / "docs" / "PAMS_FRONTEND_DESIGN_SYSTEM.md").read_text(encoding="utf-8")

    for token in [
        "PAMS Design System Layer v4",
        "PAMS Page Shell Template v1",
        "--pams-surface",
        "--pams-radius-panel",
        "--pams-row-min",
        "--pams-shell-max-width",
        "--pams-shell-gutter",
        "--pams-card-pad",
        "--pams-list-gap",
        "--pams-list-row-pad-y",
        "--pams-list-row-pad-x",
        "body.app-shell .wrap",
        "body.app-shell .pageHeader",
        "body.app-shell .pageSubtitle",
        "body.app-shell .card",
        "body.app-shell .card + .card",
        "PAMS Core List Template v1",
        "body.app-shell .pamsList",
        "body.app-shell .ledgerListRow",
        "body.app-shell .syncCenterRow",
        "body.app-shell .accountIssueRow",
        "body.app-shell .sourceHealthRow",
        "body.app-shell .mobileHoldingRow",
        "body.app-shell .moreRow",
        "body.app-shell .tradeReviewDirectionTrade",
        "PAMS Low Frequency Page Template v1",
        "body.app-shell.app-low-frequency .lowFrequencyStack",
        "body.app-shell.app-low-frequency .appWorkHeader",
        "body.app-shell.app-low-frequency details.card > summary",
        "PAMS Mobile Polish v1",
        "@media (max-width: 430px)",
        "--pams-mobile-gutter",
        "body.app-shell .nav a[data-mobile-nav=\"primary\"]",
        "body.app-shell .appBottomSheetPanel",
        "body.app-shell .appWorkHeader",
        "body.app-shell .appShortcutGrid",
        "body.app-shell .appBottomSheet",
        "body.app-shell .emptyState",
        "body.app-shell .dataState",
        "body.app-shell .dataState-error",
        "body.app-shell .miniBadge",
        "body.app-shell .skeletonCard",
        "body.app-shell .statusChip.in_transit",
        "body.app-shell .ledgerActionBtn.confirm",
        "PAMS Visual Cohesion v1",
        "body.app-shell .statusbar",
        "body.app-shell .appWorkHeader p",
        "body.app-shell .moreRow.accent-data",
        "body.app-shell .moreRow.accent-risk",
        "prefers-reduced-motion",
    ]:
        assert token in design_css

    for removed_shell in ["appActionRow", "riskAnchorNav", "appPageHero"]:
        assert removed_shell not in design_css

    assert "appPageHero" not in legacy_css
    assert "appHeroTop" not in legacy_css
    assert "appHeroLinks" not in legacy_css
    for canonical_token in [
        "--pams-bg-canvas: #05070a",
        "--pams-bg-panel: #0f141b",
        "--pams-text-primary: #f3f6fb",
        "--pams-profit: #23c26b",
        "--pams-loss: #ff5b5b",
        "--pams-action: #58a6ff",
        "--bg: var(--pams-bg-canvas)",
        "--card: var(--pams-bg-panel)",
        "--good: var(--pams-profit)",
        "--bad: var(--pams-loss)",
    ]:
        assert canonical_token in legacy_css
    assert "appShortcutGrid" not in legacy_css
    for migrated_selector in [
        ".emptyState {\n",
        ".miniBadge {\n",
        ".skeletonCard,\n.skeletonLine,\n.skeletonList",
        ".appBottomSheet {\n",
        "@keyframes skeletonPulse",
    ]:
        assert migrated_selector not in legacy_css

    accounts_html = _read_public("accounts.html")
    ledger_html = _read_public("ledger.html")
    for duplicated_shared_style in [
        ".statusChip { display",
        ".reviewBtn { border",
    ]:
        assert duplicated_shared_style not in accounts_html
    for duplicated_shared_style in [
        ".flowTag { display",
        ".ledgerActionBtn { min-width",
    ]:
        assert duplicated_shared_style not in ledger_html

    for removed_selector in [
        "page-risk",
        "page-dashboard",
        "riskTerminal",
        "riskCommandHero",
        "dashboardHero",
        "dashboardHoldingRow",
        "metricStrip",
        "metricTile",
        "appHeroStrip",
        "appHeroCopy",
    ]:
        assert removed_selector not in legacy_css

    for page in _formal_html_pages():
        html = _read_public(page)
        parsed = _parse_html(html)
        style_index = next(i for i, href in enumerate(parsed.stylesheets) if href.startswith("assets/css/style.css"))
        design_index = next(i for i, href in enumerate(parsed.stylesheets) if href.startswith("assets/css/design-system.css"))
        design_href = parsed.stylesheets[design_index]

        assert style_index < design_index
        assert "?v=" in parsed.stylesheets[style_index]
        assert "?v=" in design_href
        assert parsed.stylesheets[style_index] == "assets/css/style.css?v=20260620-mobile-density-v1"
        assert design_href == "assets/css/design-system.css?v=20260620-visual-cohesion-v1"
        assert "app-shell" in parsed.body_classes
        assert [(item["href"], item["text"].strip()) for item in parsed.data_navs] == [
            ("investments.html", "投资"),
            ("ledger.html", "收支"),
            ("more.html", "更多"),
        ]
        assert "appPageHero" not in html
        assert "page-risk" not in parsed.body_classes
        assert "page-dashboard" not in parsed.body_classes

    for phrase in [
        "PAMS 前端设计系统收敛 v4",
        "`design-system.css` 必须在带版本号的 `style.css?v=...` 后加载",
        "不要恢复大 hero",
    ]:
        assert phrase in design_doc

    assert "PAMS Mobile Density v1" in legacy_css
    assert "--mobile-density-gutter" in legacy_css
    assert "body.app-shell .nav a[data-mobile-nav=\"primary\"]" in legacy_css


def test_legacy_frontend_entrypoints_stay_removed() -> None:
    legacy_pages = [
        "bank_reconcile.html",
        "cashflow.html",
        "holdings.html",
        "holdings_categories.html",
        "holdings_detail.html",
        "strategy.html",
        "transactions.html",
        "watchlist.html",
    ]
    dockerfile = (REPO_ROOT / "frontend" / "Dockerfile").read_text(encoding="utf-8")
    ui_js = _read_public("assets/js/ui.js")

    for page in legacy_pages:
        assert not (PUBLIC_ROOT / page).exists()
        assert f"location = /{page} {{ return 404; }}" in dockerfile
        assert f"'{page}'" not in ui_js
        assert f'"{page}"' not in ui_js
        assert f"return 302 /{page}" not in dockerfile

    assert not (PUBLIC_ROOT / "assets/js/risk.js").exists()
    assert not (PUBLIC_ROOT / "index.html").exists()
    assert not (PUBLIC_ROOT / "risk.html").exists()
    assert not (PUBLIC_ROOT / "assets/js/dashboard.js").exists()
    assert not (PUBLIC_ROOT / "assets/js/risk_metrics.js").exists()
    assert "location = /index.html { return 302 /investments.html; }" in dockerfile
    assert "location = /risk.html { return 302 /investments.html#portfolio-risk-disclosure; }" in dockerfile

    for legacy_alias in ["bank_reconcile", "cashflow", "holdings", "strategy", "transactions", "watchlist"]:
        assert f"{legacy_alias}:" not in ui_js


def test_ledger_page_uses_card_list_navigation_contract() -> None:
    html = _read_public("ledger.html")
    js = _read_public("assets/js/ledger_cashflow.js")
    css = _read_public("assets/css/style.css")

    assert 'class="ledgerAnchorNav"' in html
    assert ".ledgerAnchorNav { display:grid; grid-template-columns:repeat(3,minmax(0,1fr))" in html
    assert ".page-ledger .cashflowBodyGrid {" in html
    assert "display:grid !important;" in html
    assert "grid-template-columns:minmax(0, 7fr) minmax(280px, 3fr) !important;" in html
    assert ".page-ledger .cashflowBodyGrid > div:first-child { order:2 }" in html
    assert "body.page-ledger.app-shell .ledgerTop > .sectionTitle" in html
    assert "body.page-ledger.app-shell .ledgerTop .ledgerToolbar" not in html
    for href in [
        "#cashflow-workbench",
        "#recent-flow-section",
        "#account-cashflow-section",
    ]:
        assert f'href="{href}"' in html
    assert 'href="#review-section"' not in html
    assert 'data-ledger-tab="review"' not in html
    for removed_href in ["#income-section", "#accountIssueStrip", "#transfers"]:
        assert f'data-ledger-tab' in html
        assert f'href="{removed_href}" data-ledger-tab=' not in html

    assert 'id="accountFlowRows" class="accountFlowList"' in html
    assert 'data-account-period="month"' in html
    assert 'data-account-period="ytd"' in html
    assert 'id="recentAccountScope" class="recentAccountScope" hidden' in html
    assert 'id="recentFlowChips" class="ledgerFlowChips"' in html
    for flow_filter in ["income", "expense", "investment_transfer", "internal_transfer", "review"]:
        assert f'data-ledger-flow-filter="{flow_filter}"' in html

    assert 'id="reviewRows" class="ledgerList"' not in html
    assert 'id="recentRows" class="ledgerList"' in html
    assert 'id="recentFlowFilter"' not in html
    assert "消费排行" in html
    assert "消费 Top 5" not in html
    assert "reviewEntryHeader" not in html
    assert "reviewEntryStatus" not in html
    assert "accountPendingBadge" in html
    assert "ledgerRowsTable" not in html
    assert "mobileReviewRows" not in html
    assert 'id="accountIssueStrip"' not in html
    assert "<table" not in html
    assert "overflow-x:auto" not in html

    assert "ledgerListRow" in js
    assert "categoryBarList" in js
    assert "categoryBarMeta" in js
    assert "categoryBarHead" not in js
    assert "reviewEntryRow" not in js
    assert "reviewEntryStatus" not in js
    assert "accountPendingBadgeHtml" in js
    assert "renderAccountFlowSummary" in js
    assert "openRecentForAccount" in js
    assert "recentAccountFilter" in js
    assert "account_summary" in js
    assert "account_summary_ytd" in js
    assert "currentAccountSummary" in js
    assert "高优先级" not in js
    assert " / 详情" not in js
    assert "recentFlowFilter" in js
    assert "isInvestmentTransferRow" in js
    assert "isInternalTransferRow" in js
    assert "renderMobileLedgerRows" not in js
    assert "<tr" not in js
    assert "'#review-section': 'account'" in js
    assert "assets/js/ledger_cashflow.js?v=20260621-ledger-category-normalize-v1" in html
    assert 'id="overviewTitle"' in html
    assert '<h3 id="overviewTitle">总览</h3>' in html
    assert "overviewTitle', `${overviewPeriodLabel()}现金流`" not in js
    assert 'data-overview-period="month"' in html
    assert 'data-overview-period="ytd"' in html
    assert 'class="accountPeriodButton active" type="button" data-overview-period="ytd"' in html
    assert 'id="monthPicker"' not in html
    assert "overviewPeriod: 'ytd'" in js
    assert "currentOverviewPeriod" in js
    assert "currentOverviewCategories" in js
    assert "renderOverviewPeriodButtons" in js
    assert "expense_categories_ytd" in js
    assert "income_categories_ytd" in js
    assert "transfer_summary_month" in js
    assert "transfer_summary_ytd" in js
    assert 'data-recent-period="month"' in html
    assert 'data-recent-period="ytd"' in html
    assert "recentPeriod: 'ytd'" in js
    assert "accountPeriod: 'ytd'" in js
    assert 'class="accountPeriodButton active" type="button" data-account-period="ytd"' in html
    assert 'class="accountPeriodButton active" type="button" data-recent-period="ytd"' in html
    assert "recent_rows_month" in js
    assert "recent_rows_ytd" in js
    assert "originalCurrencyText" in js
    assert "transferRouteHtml" in js
    assert "转出账户" in js
    assert "转入账户" in js
    assert "投资入金" in js
    assert "payment_proxy:" in js
    assert "function isAccountValidationRow(row)" in js
    assert "function rowTagLabel(row)" in js
    assert "function rowAmountClass(row, amount)" in js
    assert "function rowDateText(row)" in js
    assert "function accountFriendlyName(value)" in js
    assert "to_tonghuashun" in js
    assert "direct_investment" in js
    assert "function rowSearchText(row)" in js
    assert "function rowMetaText(row)" in js
    assert "function ledgerEditSheetHtml(row)" in js
    assert "function bindLedgerEditSheet(row)" in js
    assert "function openLedgerEdit(row)" in js
    assert "function ledgerEditActionLabel(row)" in js
    assert "ledgerInlineEditBtn" in html
    assert "ledgerInlineEditBtn" in js
    assert "保存并确认" in js
    assert "layout: { padding: { left: 8, right: 18, top: 2, bottom: 4 } }" in js
    assert "/api/ledger/rows/${encodeURIComponent(row.id)}/update" in js
    assert "data-ledger-confirm" not in js
    assert "确认这笔消费" not in js
    assert "data-ledger-edit" in js
    assert "ledgerEditGrid" in html
    assert 'class="ledgerListAccount"' in js
    assert 'class="ledgerListDate"' in js
    assert "[row.summary, row.description, row.counterparty].filter(Boolean).join(' / ')" not in js
    assert ".transferRoute" in html
    assert ".ledgerListSide b.transfer" in html
    assert "ledgerOriginalAmount" in html
    assert "jget(`/api/ledger/workbench?${params.toString()}`, { cache: false })" in js
    assert "ACCOUNT_GROUPS" in js
    assert "buildAccountFlowGroups" in js
    assert "accountGroupRow" in html
    assert "accountSubRow" in html
    assert "accountSyncBadge" in html
    assert "syncBadgeHtml" in js
    assert "latest_sync_at" in js
    assert ".page-ledger .cashflowBridge,\n  .page-ledger .ledgerRules" not in css


def test_investments_page_exposes_performance_dashboard_sections() -> None:
    html = _read_public("investments.html")
    holdings_js = _read_public("assets/js/holdings_categories.js")
    holding_rows_js = _read_public("assets/js/holdings.js")
    account_cash_js = _read_public("assets/js/account_cash.js")
    tx_js = _read_public("assets/js/transactions.js")

    for element_id in [
        "investmentSummaryHeader",
        "profitContributionPanel",
        "returnMethodBar",
        "investmentReturnWindowToggle",
        "returnEventHotspots",
        "returnEventPopover",
        "holdings-smart-table",
        "lookthrough-exposure",
        "lookthroughTabs",
        "lookthroughList",
        "lookthroughQuality",
        "trade-attribution-analysis",
        "category-detail-shell",
        "categoryTabs",
        "transactionSummaryPanel",
        "manualTradeEntry",
        "btnCreateTrade",
        "tradeAmount",
        "tradeQuantity",
        "tradeFees",
        "tradeAccount",
        "tradeCurrency",
        "tradeNameList",
        "tradeFilterPanel",
        "portfolio-risk-disclosure",
        "portfolioRiskDashboard",
        "portfolioRiskCategories",
    ]:
        assert f'id="{element_id}"' in html
    assert "portfolioRiskInputs" not in html
    assert '<details id="category-detail-shell" class="investmentDetailFold" hidden>' in html
    assert '<details id="category-detail-shell" class="investmentDetailFold" data-invest-panel="analysis">' not in html
    assert '<details id="category-detail-shell" class="investmentDetailFold" data-invest-panel="analysis" hidden>' not in html
    assert '<details id="category-detail-shell" class="investmentDetailFold" data-invest-panel="analysis" open>' not in html

    assert 'id="tradeFilterPanel" class="investmentPanel tradeFilterPanel" hidden' in html

    lookthrough_js = _read_public("assets/js/lookthrough.js")
    assert "/api/analysis/lookthrough?baseCurrency=CNY" in lookthrough_js
    assert "observeAnalysisVisibility" in lookthrough_js
    assert "loadActiveAnalysisPanel" in lookthrough_js
    assert "trade_attribution" in lookthrough_js
    assert "loadLookthrough();" not in lookthrough_js
    assert 'data-analysis-panel-switch="trade_attribution"' in html
    risk_panel_html = html.split('id="portfolio-risk-disclosure"', 1)[1].split('id="portfolio-constraints"', 1)[0]
    analysis_panel_html = html.split('id="analysisSwitch"', 1)[1].split('id="category-details"', 1)[0]
    assert 'id="lookthrough-exposure"' in risk_panel_html
    assert "风险暴露" in risk_panel_html
    assert 'data-analysis-panel-switch="lookthrough"' not in analysis_panel_html
    assert 'data-analysis-panel-page="lookthrough"' not in analysis_panel_html
    assert "investmentDetailFold" in _read_public("assets/css/style.css")
    assert ".investmentDetailFold:not([open]) > .investmentDetailBody" in _read_public("assets/css/style.css")
    assert "window.setTimeout(loadVisibleCategoryReturnChart, 900)" in holdings_js
    assert "window.setTimeout(loadAllCategoryReturnCharts, 900)" not in holdings_js
    assert "key === 'category-details' || CATEGORY_SECTION_IDS.includes(key)" in holdings_js
    assert "CATEGORY_SECTION_IDS.includes(key)) show(key, { openShell: true })" in holdings_js

    for fn in [
        "InvestmentSummaryHeader",
        "ProfitContributionPanel",
        "ReturnMethodBadge",
        "DataQualityBanner",
        "CategoryDetailTabs",
        "openCategoryDetailShell",
        "isCategoryDetailOpen",
        "shell?.hidden",
        "normalizeHolding",
        "TransactionSummaryPanel",
        "tradeLedgerItem",
        "btnOpenTradeForm",
        "btnToggleTradeFilters",
        "btnToggleAllTrades",
        "setTradeFilterPanelOpen",
    ]:
        assert fn in holdings_js + tx_js

    main_js = holdings_js.split("async function main()", 1)[1]
    first_main_load = main_js.split("await Promise.all([", 1)[1].split("]);", 1)[0]
    assert "runWhenInvestmentAnalysisVisible" in holdings_js
    assert "loadAllCategoryReturnCharts" in holdings_js
    assert "const aStockDailyPnl = Number.isFinite(Number(aStockSummary.dailyPnl))" in holdings_js
    assert "dailyPnl: Number.isFinite(Number(aStockAccountSummary.dailyPnl))" in holdings_js
    assert "const dailyPnlEffective = perf.daily_pnl_effective !== false" in holdings_js
    assert "dailyPnlEffective && Number.isFinite(Number(perf.daily_pnl_cny))" in holdings_js
    assert 'id="investmentNavChart"' in html
    assert "function renderInvestmentNavChart(rows, windowKey, isMobile)" in holdings_js
    assert "function createInvestmentReturnCanvasFallback(canvas, data)" in holdings_js
    assert "if (typeof Chart === 'undefined')" in holdings_js
    assert "function returnEventAdjustedAmount(row, prev)" in holdings_js
    assert "const rawPnlDelta = safeNum(row.total_pnl) - safeNum(prev.total_pnl)" in holdings_js
    assert "pnlDelta = returnEventAdjustedAmount(row, prev)" in holdings_js
    assert "/nav/weekly?start=2026-01-01&baseCurrency=CNY&latest=true&interval=daily" in holdings_js
    assert "function normalizeInvestmentNavRows(rows)" in holdings_js
    assert "row.netWorth ?? row.net_worth ?? row.market_value" in holdings_js
    assert "updateInvestmentNavChart(state);" in holdings_js
    assert "updateInvestmentNavChart(state, windowData.rows)" not in holdings_js
    return_date_parser = holdings_js.split("function parseReturnSeriesDate(value)", 1)[1].split("function returnWindowStartDate", 1)[0]
    assert "Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]))" in return_date_parser
    assert "T00:00:00+08:00" not in return_date_parser
    assert "setUTCDate" in holdings_js
    assert "setUTCMonth" in holdings_js
    assert "/nav/series" not in holdings_js
    assert "dailyPnl: null" in holdings_js
    assert "flexCoversPositionDate" in holdings_js
    assert "accountNetLiquidationFromHoldings" in holdings_js
    assert "jgetFresh('/holdings/latest?limit=500').catch(() => null)" in holdings_js
    assert "accountPrimaryBalanceCny(accountOverview, 'ibkr', fxRates)" in holdings_js
    assert "ibkrOverviewCoversPositionDate" in holdings_js
    assert "safeNum(ibkrOverviewBalance.amountCny) > 0" in holdings_js
    assert "const ibkrHoldingsNet = accountNetLiquidationFromHoldings(latestHoldings, 'ibkr')" in holdings_js
    assert "const ibkrUsesNetLiquidation = Boolean(" in holdings_js
    assert "holdings_snapshot_net_liquidation" in holdings_js
    assert "ibkrSummary?.flexCoversPositionDate ||" in holdings_js
    assert "ibkrSummary?.holdingsNetCoversPositionDate ||" in holdings_js
    assert "{ ...usCategorySummary, dailyPnl: usCategorySummary.dailyTotalPnl }" in holdings_js
    assert "const useIbkrAccountTotals = Boolean(ibkrSummary && ibkrFlexCoversPositionDate)" in holdings_js
    assert "totalValue: ibkrUsesNetLiquidation ? ibkrAccountValue : usSummary.totalValue" in holdings_js
    assert "positionValue: ibkrPositionValue" in holdings_js
    assert "dailyPnl: usSummary.dailyPnl" in holdings_js
    assert "ibkrSummary: usCategorySummary" in holdings_js
    assert "function freshValuationStateForTarget" in holdings_js
    assert "const summaryFresh = isFreshPayloadAsOf(portfolioSummary, targetAsOf)" in holdings_js
    assert "portfolioSummaryFreshForAsOf && portfolioSummary?.totals?.daily_pnl" in holdings_js
    assert "const reportedReturnsFresh = isFreshPayloadAsOf(reportedReturns, asof)" in holdings_js
    assert "staleReportedReturns" in holdings_js
    assert "valueLabel: '账户净值'" in holdings_js
    assert "btnLoadInvestmentReturnChart" not in holdings_js
    assert "加载收益曲线" not in holdings_js
    assert "有效历史不足" not in holdings_js
    assert "is-unavailable" not in holdings_js
    assert "trimReturnRowsForChart" not in holdings_js
    assert "/investment/return-series?start=2026-01-01&interval=daily&baseCurrency=CNY" not in first_main_load
    assert "/investment/section-return-series" not in first_main_load

    for phrase in [
        "贡献",
        "组合收益率",
        "手动交易记录录入",
        "手动录入",
        "平台",
        "交易日期",
        "标的名称",
        "交易标的",
        "金额",
        "持仓",
        "份数",
        "税费",
        "展开更多",
        "含多口径数据",
    ]:
        assert phrase in html + holdings_js + tx_js

    assert "最近10单" not in tx_js
    assert "deposit: '入金'" in tx_js
    assert "withdraw: '出金'" in tx_js
    assert "transfer_in: '转入'" in tx_js
    assert "fx_conversion: '换汇'" in tx_js
    assert '<option value="fx_conversion">换汇</option>' in html

    assert "ledgerDetails" not in html + tx_js
    assert "ledgerRows" not in html + tx_js
    assert "btnOpenLedgerDetails" not in html + tx_js
    assert "tradeMajor" not in html + tx_js
    assert "tradeMinor" not in html + tx_js
    assert "tradeSymbol" not in html + tx_js
    assert "tradePrice" not in html + tx_js
    assert "tradeNotes" not in html + tx_js

    assert "assets/js/holdings.js?v=20260620-data-state-v1" in html
    assert "assets/js/holdings_categories.js?v=20260620-category-detail-hidden-v1" in html
    assert "assets/js/lookthrough.js?v=20260620-data-state-v1" in html
    assert "assets/js/portfolio_risk.js?v=20260620-data-state-v1" in html
    assert "assets/js/portfolio_config.js?v=20260620-data-state-v1" in html
    assert "style.css?v=20260620-mobile-density-v1" in html
    assert 'href="#portfolio-risk-disclosure" data-invest-tab="risk">风险</a>' in html
    assert 'href="#portfolio-constraints" data-invest-tab="allocation">配置</a>' in html
    assert 'id="portfolio-constraints"' in html
    assert "风险检查清单" in html
    assert ">风险披露</h2>" not in html
    assert "七类风险来源，不作为目标配置加总。" not in html
    assert "风险仪表盘" not in html
    assert "目标配置规则" in html
    assert "portfolioRuleList" in html
    assert "portfolioRebalanceQueue" in html
    assert "说明" in html
    assert "累计盈亏" in holding_rows_js
    assert "当前浮盈" in holding_rows_js
    assert "总负债" in holdings_js
    assert "resolvePortfolioLiabilitiesCny" in holdings_js
    assert "totalLiabilities" in holdings_js
    assert "摊薄成本" in holding_rows_js
    assert "realizedPnlNote" in holding_rows_js
    assert "holdingAveragePriceText" in holding_rows_js
    assert "dilutedAveragePriceText" in holding_rows_js
    assert "currentFloatPnl" in holding_rows_js
    assert "mobileHoldingQtyInline" not in holding_rows_js
    assert "mobileHoldingDailyLine" in holding_rows_js
    assert "mobileHoldingChangePct" in holding_rows_js
    assert "mobileHoldingQuoteLine" in holding_rows_js
    assert "join('<i aria-hidden=\"true\">·</i>')" in holding_rows_js
    assert "<em>份数</em>" in holding_rows_js
    assert "<em>${isCash ? '本币' : '现价'}</em>" in holding_rows_js
    assert "mobileHeroMetaTop" in holdings_js
    assert "returnChartHeader" in html
    assert "returnWindowSegment" in html
    assert "returnSeriesRail" in html
    assert "returnSeriesToggle" in html
    assert "returnInsightGrid" in html
    assert "returnInsightCard" in html
    assert "returnEventsPanel" not in html
    assert "buildReturnEvents" in holdings_js
    assert "renderReturnEventHotspots" in holdings_js
    assert "bindReturnEventPopover" in holdings_js
    assert "showReturnEventPopover" in holdings_js
    assert "buildTopContributionForWindow" in holdings_js
    assert "TopContributionPanel" in holdings_js
    assert "realized_other" not in holdings_js
    assert "总账补差" not in holdings_js
    assert "confirmedTotalPnl" not in holdings_js
    assert "renderInvestmentReturnChart(grandPnl)" not in holdings_js
    assert "renderInvestmentReturnChart()" in holdings_js
    assert "unattributed_residual" in holdings_js
    assert "未归因差额" in holdings_js
    assert "returnSeriesKey: 'events'" in holdings_js
    assert "RETURN_WINDOW_OPTIONS" in holdings_js
    assert "data-return-window" in holdings_js
    assert "年初至今" in holdings_js
    assert "近1月" in holdings_js
    assert "近1周" in holdings_js
    assert "rebaseReturnRate" in holdings_js
    assert "accountCashBreakdown" in holdings_js
    assert "makeAccountCashHoldingRows" in holding_rows_js
    assert "subtractMoneyCents" in holding_rows_js
    assert "returnRateFromCapitalBase" in holdings_js
    assert "fundActiveSummary" in holdings_js
    assert "setKpiLabel('dailyPnlUSStock', '已实现 / 未实现')" not in holdings_js
    assert "setKpiLabel('dailyPnlAStockEquity', '已实现 / 未实现')" not in holdings_js
    assert "dailyTotalPnl += rowDailyTotalPnl" in holdings_js
    assert "cashActiveValue" in holdings_js
    for account_type in ["funding", "cash_management", "wallet", "liability", "credit_card"]:
        assert account_type in account_cash_js
    assert "现金/活期" in account_cash_js + holdings_js + holding_rows_js


def test_cashflow_and_transaction_scripts_share_display_helpers() -> None:
    utils_js = _read_public("assets/js/utils.js")
    ledger_js = _read_public("assets/js/ledger_cashflow.js")
    tx_js = _read_public("assets/js/transactions.js")
    ledger_html = _read_public("ledger.html")
    investments_html = _read_public("investments.html")

    for helper in [
        "export const byId",
        "export function escapeHtml",
        "export function toFiniteNumber",
        "export function formatPlainMoney",
        "export function formatCurrency",
        "export function formatPercent",
        "export function formatQuantity",
        "export function readOptionalNumber",
        "export function showError",
        "export function clearError",
        "export function setText",
    ]:
        assert helper in utils_js

    for js in [ledger_js, tx_js]:
        assert "from './utils.js?v=20260607-shared-format-v1'" in js
        assert "function escapeHtml" not in js
        assert "function num(" not in js
        assert "function showError(" not in js
        assert "function clearError(" not in js

    for duplicated_helper in [
        "function money(",
        "function formatCurrency(",
        "function formatPercent(",
        "function formatQuantity(",
        "function readOptionalNumber(",
    ]:
        assert duplicated_helper not in tx_js

    for duplicated_helper in [
        "function money(",
        "function signedMoney(",
        "function axisMoney(",
        "function pct(",
        "function setText(",
    ]:
        assert duplicated_helper not in ledger_js

    assert "assets/js/ledger_cashflow.js?v=20260621-ledger-category-normalize-v1" in ledger_html
    assert "assets/js/transactions.js?v=20260620-data-state-v1" in investments_html


def test_investment_portfolio_config_page_uses_constraint_rules() -> None:
    html = _read_public("investments.html")
    js = _read_public("assets/js/portfolio_config.js")
    risk_js = _read_public("assets/js/portfolio_risk.js")
    css = _read_public("assets/css/style.css")
    tab_js = _read_public("assets/js/holdings_categories.js")
    endpoint_py = Path("backend/api/endpoints/portfolio_state.py").read_text(encoding="utf-8")
    service_py = Path("backend/api/services/portfolio_config.py").read_text(encoding="utf-8")
    risk_service_py = Path("backend/api/services/portfolio_risk_disclosure.py").read_text(encoding="utf-8")

    assert 'data-invest-tab="risk"' in html
    assert 'data-invest-panel="risk"' in html
    assert 'data-invest-tab="allocation"' in html
    assert 'data-invest-panel="allocation"' in html
    assert 'id="portfolio-risk-disclosure"' in html
    assert 'id="portfolio-constraints"' in html
    assert "portfolioRiskDashboard" in html
    assert "portfolioRiskCategories" in html
    assert "portfolioConfigRules" in html
    assert "portfolioRebalanceQueue" in html
    assert "key === 'portfolio-risk-disclosure' || key === 'lookthrough-exposure'" in tab_js
    assert "portfolio-constraints') return 'allocation'" in tab_js

    assert "/api/portfolio/config?baseCurrency=CNY" in js
    assert "/api/portfolio/risk-disclosure?baseCurrency=CNY" in risk_js
    assert "/api/risk/summary" not in js
    assert "/api/risk/summary" not in risk_js
    assert "/api/analysis/lookthrough" not in js
    assert "/api/analysis/lookthrough" not in risk_js
    assert '@router.get("/portfolio/config")' in endpoint_py
    assert '@router.get("/portfolio/risk-disclosure")' in endpoint_py
    for label in [
        "第一层配置",
        "非上市权益",
        "股票",
        "债券",
        "现金短债",
        "融资负债",
        "美国权益",
        "中国/港股权益",
        "韩国/欧洲/其他权益",
        "其他/待映射权益",
        "美股宽基",
        "美股/海外个股",
    ]:
        assert label in service_py
    for label in [
        "持仓事实",
        "风险披露",
        "配置输入",
        "市场方向风险",
        "地域与币种风险",
        "行业与主题风险",
        "集中度风险",
        "流动性与估值风险",
        "杠杆与融资风险",
        "因子风格风险",
    ]:
        assert label in risk_service_py
    for rule_token in [
        "CONFIG_GROUPS",
        "primary_allocation",
        "group_id",
        "display",
        "children",
        "drilldown_rules",
        "target_low",
        "target_high",
        "hard_high",
        "hard_low",
        "current_amount_label",
        "target_amount_label",
        "limit_amount_label",
        "inputs",
        "build_portfolio_config_input_summary",
        "_build_rebalance",
        "_financing_cost_children",
        "reserve_interest",
    ]:
        assert rule_token in service_py
    for rule_token in [
        "targetLow",
        "targetHigh",
        "hardHigh",
        "hardLow",
        "currentAmountLabel",
        "targetAmountLabel",
        "limitAmountLabel",
        "currentGroups",
        "currentDrilldownRules",
        "linkedChildren",
        "renderRuleGroups",
        "portfolioRuleGroup",
        "portfolioRuleNode",
        "portfolioRuleChildren",
        "portfolioLeafRow",
        "data-portfolio-subrule",
        "expandedRuleIds",
        "expandedSubRuleIds",
        "portfolioRuleAmounts",
        "portfolioRuleTrack",
        "portfolioRuleCheck",
    ]:
        assert rule_token in js
    for rule_token in [
        "currentCategories",
        "data-risk-category",
        "portfolioRiskDashboard",
        "portfolioRiskGauge",
        "portfolioRiskNeedle",
        "portfolioRiskGaugeHub",
        "dashboardSeverity",
        "categoryStatusCounts",
        "category_scores",
        "max_score",
        "portfolioRiskCategory",
        "portfolioRiskChecklistCard",
        "portfolioRiskChecklistSummary",
        "portfolioRiskInlinePanel",
        "portfolioRiskInlineItem",
        "portfolioRiskInlineName",
        "portfolioRiskCompactRule",
        "portfolioRiskRuleStrip",
        "portfolioRiskRuleMeta",
        "cleanAssetName",
        "ruleFormula",
        "thresholdChipData",
        "compactAdjustment",
        "itemStatusLabel",
        "itemState",
        "ruleBlock",
        "expandedRiskCategoryId",
    ]:
        assert rule_token in risk_js
    assert "ruleLine" not in risk_js
    assert "ruleConditions" not in risk_js
    assert "满足：" not in risk_js
    assert "不满足：" not in risk_js
    assert "分子" not in risk_js
    assert "分母" not in risk_js
    assert "portfolioRiskInputChip" not in risk_js
    assert "portfolioRiskHighlights" not in risk_js
    assert "openBottomSheet" not in risk_js
    assert "portfolioRiskChecklistStats" not in risk_js
    assert "portfolioRiskTopTags" not in risk_js
    assert "portfolioRiskSector" not in risk_js
    assert "风险仪表盘" not in risk_js
    assert "dashboard.counts" not in risk_js
    assert "checklistRuleText" not in risk_js
    assert "checklistActionText" not in risk_js

    assert "portfolioRiskCategory" in css
    assert "portfolioRiskDashboard" in css
    assert "portfolioRiskGauge" in css
    assert "portfolioRiskNeedle" in css
    assert "portfolioRiskGaugeHub" in css
    assert "border: 0;" in css
    assert ".portfolioRiskDashboard.risk { --risk-accent: #ff6b6b; }" in css
    assert "portfolioRiskChecklistCard" in css
    assert "portfolioRiskChecklistSummary" in css
    assert "portfolioRiskInlinePanel" in css
    assert "portfolioRiskInlineItem.pass" in css
    assert "portfolioRiskInlineItem.watch" in css
    assert "portfolioRiskInlineItem.fail" in css
    assert "portfolioRiskInlineName" in css
    assert "portfolioRiskCompactRule" in css
    assert "portfolioRiskRuleChip" in css
    assert "portfolioRiskRuleMeta" in css
    assert "portfolioRiskInputFlow" not in css
    assert "portfolioRiskDetailItem" in css
    assert 'body.page-investments[data-invest-tab="risk"] #investment-conclusion' in css
    assert 'data-rule-node="esop"' in css
    assert 'data-rule-node="stock"' in css
    assert 'data-rule-node="bond"' in css
    assert 'data-rule-node="cash"' in css
    assert "--portfolio-state-surface" in css
    assert "--portfolio-state-border" in css
    assert "--portfolio-status-rgb" in css
    assert "--portfolio-status-ink" in css
    assert "portfolioRuleFooter" in css
    assert "portfolioRuleCheck" in css
    assert "portfolioRuleRow" in css
    assert "portfolioRuleGroup" in css
    assert "portfolioRuleChildren" in css
    assert "portfolioLeafRow" in css
    assert "portfolioHierarchyLabel" in css
    assert "portfolioRuleAmounts" in css
    assert "portfolioRuleTarget" in css
    assert "portfolioRebalanceRow" in css
    assert "portfolioConfigSheet" in css
    assert "ESOP增配 40万" not in html
    assert "32万融资" not in html


def test_investment_trade_review_uses_fact_annotations_and_sheet() -> None:
    html = _read_public("investments.html")
    tx_js = _read_public("assets/js/transactions.js")
    css = _read_public("assets/css/style.css")
    more_html = _read_public("more.html")
    api_py = Path("backend/api/endpoints/investment_transactions.py").read_text(encoding="utf-8")

    assert 'id="filterReviewStatus"' not in html + tx_js
    assert 'id="reviewStatus"' not in html + tx_js
    assert 'id="filterAsset"' not in html
    assert "tradeFilterForm" in tx_js
    assert "filterSheetAsset" in tx_js
    assert "tradeReviewBtn" in tx_js
    assert "tradeReviewForm" in tx_js
    assert "openBottomSheet" in tx_js
    assert "/api/investment-transactions/options" in tx_js
    assert "/api/investment-transactions/${encodeURIComponent(tradeId)}/review" in tx_js
    assert "review_options" in tx_js
    assert "data.assets" in tx_js
    assert "asset_key" in tx_js
    assert "toggleTradeFilterPanel" in tx_js
    assert 'aria-controls="tradeFilterPanel"' in tx_js
    assert "filterSheetSearch" in tx_js
    assert "review_status:" not in tx_js
    assert "reviewStatusLabel" not in tx_js
    assert "renderReviewChoiceOptions" in tx_js
    assert "radioInputValue" in tx_js
    assert "buy_reasons" in tx_js
    assert "sell_reasons" in tx_js
    assert "review_tags" in tx_js
    assert "position_identities" in tx_js
    assert "current_position_identities" in tx_js
    assert "identity_confidences" in tx_js
    assert "sell_rule_types" in tx_js
    assert "transition_types" in tx_js
    assert "transition_triggers" in tx_js
    assert "卖出原因" in tx_js
    assert "快速记录" in tx_js
    assert "查看交易后表现" in tx_js
    assert "_fact_major_category_expr" in api_py
    assert "_fact_minor_category_expr" in api_py
    assert "A?" in api_py
    assert "A股" in api_py
    assert "展开计划细节" in tx_js
    assert "填写事后复盘" in tx_js
    assert "tradeReviewQualityBadge" in tx_js
    assert "identityRuleHint" in tx_js
    assert "reviewTagCompletion" in tx_js
    assert "复盘标签最多选择 3 个" in tx_js
    assert "reviewDueDate" not in tx_js
    assert "reviewTradeIntent" not in tx_js
    assert "reviewTradePlan" not in tx_js
    assert "交易备注" not in tx_js
    assert "计划备注" not in tx_js
    assert "仓位身份" in tx_js
    assert "初始仓位身份" in tx_js
    assert "身份变化留痕" in tx_js
    assert "更优标的切换" in api_py
    assert "isClosedPosition" in tx_js
    assert "不适用" in tx_js
    assert "也可以先保存后再完善" in tx_js
    assert "身份变化必须补充新卖出规则" not in tx_js
    assert "趋势仓转价值仓是高风险身份变化，必须补充" not in tx_js
    assert "未确定 / 待判断仓必须有最大仓位上限" in tx_js
    assert "不确定可以，但必须小仓位、限期复盘、不能无限加仓" in tx_js
    assert "高风险身份变化" in tx_js
    assert "tradeReviewTagGrid" in css
    assert "tradeReviewChoiceGrid" in css
    assert "tradeReviewSubsection" in css
    assert "tradeReviewTagGroup" in css
    assert "tradeReviewFold" in css
    assert "tradeReviewQualityBadge" in css
    assert "tradeFilterSheetForm" in css
    assert "tradeReviewSummary" in css
    assert "trade_review.html" not in html + more_html + tx_js


def test_holdings_category_uses_market_view_date() -> None:
    api_py = Path("backend/api/main.py").read_text(encoding="utf-8")

    assert "def _holdings_market_view_as_of" in api_py
    assert "_market_data_candidate_date" in api_py
    assert '["A股", "美股", "贵金属"]' in api_py
    assert "view_as_of=_holdings_market_view_as_of(cur)" in api_py
    assert "daily_pnl_label_date=_daily_pnl_view_as_of()" in api_py


def test_return_charts_use_daily_pnl_view_date() -> None:
    api_py = Path("backend/api/main.py").read_text(encoding="utf-8")
    function_body = api_py.split("def _investment_return_view_as_of", 1)[1].split("def _investment_today_label_date", 1)[0]

    assert "Return charts use the same daily-PnL label as the holdings page" in function_body
    assert "candidate = _daily_pnl_window_date(tz_name=tz_name)" in function_body
    assert "candidate = _market_data_candidate_date" not in function_body
    assert "interval: str = Query(\"weekly\", pattern=\"^(daily|weekly)$\")" in api_py


def test_investment_trade_attribution_is_rendered_from_api() -> None:
    html = _read_public("investments.html")
    tx_js = _read_public("assets/js/transactions.js")
    css = _read_public("assets/css/style.css")

    assert "TradeAttributionPanel" in tx_js
    assert "TradeAutoAttributionPanel" in tx_js
    assert "TradeAutoAttributionPanelV2" in tx_js
    assert "TradeAutoAttributionPanelV3" in tx_js
    assert "TradeAttributionPanelV2" in tx_js
    assert "auto_review" in tx_js
    assert "data-auto-tag" in tx_js
    assert "tradeAutoSuggestionBar" in css
    assert "tradeAutoEvidenceList" in css
    assert "tradeAttributionDetails" in css
    assert "/api/investment-transactions/attribution/summary" in tx_js
    assert "/api/investment-transactions/campaigns" in tx_js
    assert "/api/investment-transactions/${encodeURIComponent(tradeId)}/attribution" in tx_js
    assert "TradeAttributionAnalysisEntry" not in tx_js
    assert 'href="#trade-attribution-analysis" aria-label="查看整体交易归因"' not in tx_js
    assert "tradeAttributionPanel" in tx_js
    assert "TradeReviewWorkbench" in tx_js
    assert "priority_items" in tx_js
    assert "模式诊断" in tx_js
    assert "tradeReviewAnalysisFold" in tx_js
    assert "tradeReviewPatternExamples" in tx_js
    assert "tradeReviewPatternTrade" in tx_js
    assert "TradeReviewPatternHeatmap" in tx_js
    assert "TradeReviewAssetHeatmap" in tx_js
    assert "asset_heatmap" in tx_js
    assert "data-asset-heat-select" in tx_js
    assert "data-asset-heat-float" in tx_js
    assert "data-asset-heat-close" in tx_js
    assert "tradeReviewAssetFloatLayer" in css
    assert "tradeReviewAssetDetailStack" not in tx_js
    assert "TradeReviewAssetAttributionDetail" in tx_js
    assert "TradeReviewAssetTradeCard" in tx_js
    assert "TradeReviewDirectionalTradePanel" in tx_js
    assert "directional_trades" in tx_js
    assert "positive_action_count" in tx_js
    assert "negative_action_count" in tx_js
    assert "tradeReviewAssetTileText" in tx_js
    assert "tradeReviewAssetFullLabel" in tx_js
    assert "tradeReviewCompactSymbol" in tx_js
    assert "title=\"${escapeHtml(fullLabel)}\"" in tx_js
    assert "effect * 6" not in tx_js
    assert "Math.pow(tradeReviewAssetWeight(asset), 0.9)" in tx_js
    assert "tradeReviewDirectionGrid" in css
    assert "tradeReviewDirectionTrade" in css
    assert 'data-direction-limit="${escapeHtml(limit)}"' in tx_js
    assert "const limit = 10;" in tx_js
    assert "data-direction-count" in tx_js
    assert "data-direction-filter-toggle" in tx_js
    assert "data-direction-filter-panel hidden" in tx_js
    assert "data-direction-filter-value" in tx_js
    assert "applyTradeReviewDirectionFilter" in tx_js
    assert "tradeReviewDirectionFilterToggle" in css
    assert "tradeReviewDirectionFilters[hidden]" in css
    assert "contribution_reason" in tx_js
    assert "actual_total_pnl_cny" in tx_js
    assert "tradeReviewAssetPrimaryPnl" in tx_js
    assert "TradeReviewAssetAttributionBridge" in tx_js
    assert "TradeReviewAssetRuleInsights" in tx_js
    assert "tradeReviewAttributionBridge" in css
    assert "tradeReviewRuleInsight" in css
    assert "正贡献" in tx_js
    assert "负贡献" in tx_js
    assert "tradeReviewAssetTradeList.attribution" in css
    assert "tradeReviewTreemapLayout" in tx_js
    assert ".slice(0, 72)" in tx_js
    assert "tradeReviewAssetTileSize" not in tx_js
    assert "tradeReviewAssetTile" in css
    assert "tradeReviewAssetDetail" in css
    assert "--asset-name" in css
    assert "grid-column: span var(--asset-col" not in css
    assert "标的归因热力图" in tx_js
    assert "交易归因闭环" in tx_js
    assert "真实盈亏优先" in tx_js
    assert "热力图面积按真实盈亏优先" in tx_js
    assert "未解释差额等待基准归因" in tx_js
    assert "交易动作明细" in tx_js
    assert "高影响动作" in tx_js
    assert "逐笔正负贡献证据" in tx_js
    assert ".slice(0, 6)" not in tx_js
    assert "负向动作" in tx_js
    assert "正向动作" in tx_js
    asset_heatmap_block = tx_js.split("function TradeReviewAssetHeatmap", 1)[1].split("function tradeReviewPatternTiles", 1)[0]
    assert "TradeReviewDirectionalTradePanel(payload)" in asset_heatmap_block
    assert "TradeReviewPatternPanel" not in asset_heatmap_block
    assert "TradeReviewDataGaps" not in asset_heatmap_block
    assert "<strong>模式</strong>" not in asset_heatmap_block
    workbench_block = tx_js.split("function TradeReviewWorkbench(payload)", 1)[1].split("function TradeAttributionPanelV2", 1)[0]
    assert "TradeReviewAssetHeatmap(payload" in workbench_block
    assert "标的实际盈亏热力图已移到收益归因" not in workbench_block
    assert "tradeReviewHeatTile" in tx_js
    assert "面积≈交易规模" in tx_js
    assert "pattern_universe" in tx_js
    assert "tradeReviewPatternSwitch" in tx_js
    assert "data-pattern-view-target" in tx_js
    assert "复盘主线" in tx_js
    assert "问题优先级" in tx_js
    assert "tradeReviewPatternView" in css
    assert "tradeReviewPatternSwitch" in css
    assert "tradeReviewHeatmapCanvas" in css
    assert "tradeReviewHeatmapAside" in css
    assert "tradeReviewHeatSummary" in css
    assert "tradeReviewFocusCue" in css
    assert "tradeReviewModeAttribution" in css
    assert "交易动作归因" in tx_js
    assert "交易模式归因" not in tx_js
    assert "TRADE_REVIEW_ACTION_PATTERN_KEYS" in tx_js
    assert "复盘主线" in tx_js
    assert "只看三条模式主线" in tx_js
    assert "行为标签可重叠，金额不可相加" in tx_js
    assert "正向率按估算决策效果是否为正计算" in tx_js
    assert "行为标签可重叠，净影响不加总" in tx_js
    assert "展示项净影响" not in tx_js
    assert "仓位身份只做背景信息" not in tx_js
    assert "个仓位身份指标已移到明细" not in tx_js
    assert "Top 12 模式" not in tx_js
    assert "openTradeReviewById" in tx_js
    assert "tradeRowFromAttributionPayload" in tx_js
    trade_panel_block = tx_js.split("function TradeAttributionPanelV2", 1)[1].split("function renderTradeAttribution", 1)[0]
    render_attribution_block = tx_js.split("function renderTradeAttribution", 1)[1].split("function renderReviewChoiceOptions", 1)[0]
    assert "查看原始分组统计" not in trade_panel_block
    assert "CampaignLifecyclePanel" not in render_attribution_block
    assert "展开卖出复盘" not in render_attribution_block
    assert "tradeReviewWorkbench" in css
    assert "tradeReviewPatternGrid" in css
    assert "tradeReviewHeatmapTiles" in css
    assert "tradeReviewHeatTrade" in css
    assert "tradeReviewPatternExamples" in css
    assert "tradeReviewPatternTrade" in css
    assert "tradeReviewAnalysisFold" in css
    assert "trade-attribution-analysis" in html + tx_js
    assert "buy_amount_personal_cny" in tx_js
    assert "gross_amount_personal_cny" in tx_js
    assert "个人买入" in tx_js
    assert "账户原始" in tx_js
    assert "个人口径" in tx_js
    assert "transactionOverview" in tx_js
    transaction_overview_css = css.split(".transactionOverview {", 1)[1].split("}", 1)[0]
    assert "border-bottom" in transaction_overview_css
    assert "display: grid" in transaction_overview_css
    load_ledger_body = tx_js.split("async function loadLedger()", 1)[1].split("function requestAttributionSummaryLoad()", 1)[0]
    assert "loadAttributionSummary" not in load_ledger_body
    assert "pams:load-trade-attribution" in tx_js
    assert "attributionLoadPromise" in tx_js
    assert "shouldLoadVisibleTradeAttribution" in tx_js
    assert "requestVisibleTradeAttributionLoad" in tx_js
    assert "requestInitialVisibleTradeAttributionLoad" in tx_js
    assert "scheduleInitialTradeAttributionLoadProbe" in tx_js
    assert "hash === 'trade-attribution-analysis' || panel.hidden === false" in tx_js
    assert "window.addEventListener('hashchange', requestVisibleTradeAttributionLoad)" in tx_js
    assert "[0, 300, 1200, 3000]" in tx_js
    assert "TradeAttributionPanel(state.attribution)" not in tx_js
    assert "tradeAutoAttribution" in tx_js
    assert "tradeAttributionGrid" in css
    assert "tradeAttributionEntry" not in css
    assert "tradeAutoMetricGrid" in css
    assert "asset.display_name || asset.name" in tx_js
    assert "start: ''," in tx_js
    assert "assets/js/transactions.js?v=20260620-data-state-v1" in html
    assert "assets/css/style.css?v=20260620-mobile-density-v1" in html


def test_more_page_consolidates_low_frequency_entries() -> None:
    html = _read_public("more.html")
    accounts_html = _read_public("accounts.html")
    data_sources_html = _read_public("data_sources.html")
    research_html = _read_public("research.html")
    data_sources_js = _read_public("assets/js/data_sources.js")
    sync_center_js = _read_public("assets/js/sync_center.js")
    ui_js = _read_public("assets/js/ui.js")
    portfolio_config_js = _read_public("assets/js/portfolio_config.js")
    portfolio_risk_js = _read_public("assets/js/portfolio_risk.js")

    for low_frequency_html in [html, accounts_html, data_sources_html, research_html]:
        assert "app-low-frequency" in low_frequency_html
        assert 'data-page-role="low-frequency"' in low_frequency_html

    assert "lowFrequencyStack" in html
    assert "appTaskList" not in html
    assert 'id="sync"' not in html
    assert 'id="syncSummaryStrip"' not in html
    assert 'id="syncWorkflow"' not in html
    assert 'id="syncCenterRows"' not in html
    assert "data-sync-scope" not in html
    assert "appPageHero" not in html
    assert "morePrimarySummary" not in html
    assert "moreHeroDate" not in html
    assert "moreDataState" not in html
    assert "moreToolState" not in html

    for href in [
        "data_sources.html#dataQuality",
        "accounts.html#sources",
        "accounts.html#overview",
        "investments.html#asset-master-panel",
        "investments.html#portfolio-risk-disclosure",
        "research.html",
    ]:
        assert html.count(f'href="{href}"') == 1
    for removed_text in ["账户核准", "待确认流水", "投资同步", "资产结构", "同步中心", "投资交易记录", "内部转账匹配"]:
        assert removed_text not in html
    assert "investments.html#investment-ledger" not in html
    assert "risk.html" not in html
    assert "ledger.html#transfers" not in html
    assert "more.html#sync" not in html + accounts_html
    assert "assets/js/more.js?v=20260620-data-state-v1" in html
    assert "assets/js/bank_reconcile.js?v=20260620-data-state-v1" in accounts_html
    assert "assets/js/data_sources.js?v=20260620-data-state-v1" in data_sources_html
    assert "assets/js/research.js?v=20260620-data-state-v1" in research_html
    assert "./sync_center.js?v=20260620-data-state-v1" in data_sources_js
    assert "./ui.js?v=20260620-data-state-v1" in sync_center_js
    assert "ui.js?v=20260606-refresh-v3" not in data_sources_js + sync_center_js
    assert "function dataStateHtml" in ui_js
    assert "function renderDataState" in ui_js
    assert "renderDataState(el" in data_sources_js
    assert "dataStateHtml({" in sync_center_js
    assert "dataStateHtml({" in portfolio_config_js
    assert "dataStateHtml({" in portfolio_risk_js


def test_risk_capability_lives_in_investment_subpage() -> None:
    html = _read_public("investments.html")
    js = _read_public("assets/js/portfolio_risk.js")
    css = _read_public("assets/css/style.css")
    dockerfile = (REPO_ROOT / "frontend" / "Dockerfile").read_text(encoding="utf-8")

    for element_id in [
        "portfolio-risk-disclosure",
        "portfolioRiskDashboard",
        "portfolioRiskCategories",
        "lookthrough-exposure",
    ]:
        assert f'id="{element_id}"' in html

    assert "/api/portfolio/risk-disclosure?baseCurrency=CNY" in js
    assert "portfolioRiskInlineItem" in js
    assert "portfolioRiskChecklistCard" in js
    assert "portfolioRiskDashboard" in css
    assert "portfolioRiskCategory" in css
    assert "riskAnchorNav" not in css
    assert "location = /risk.html { return 302 /investments.html#portfolio-risk-disclosure; }" in dockerfile


def test_frontend_uses_short_ttl_api_cache_and_static_asset_cache() -> None:
    api_js = _read_public("assets/js/api.js")
    ui_js = _read_public("assets/js/ui.js")
    holdings_js = _read_public("assets/js/holdings_categories.js")
    lookthrough_js = _read_public("assets/js/lookthrough.js")
    css = _read_public("assets/css/style.css")
    dockerfile = (REPO_ROOT / "frontend" / "Dockerfile").read_text(encoding="utf-8")

    assert "API_CACHE_PREFIX" in api_js
    assert "__page_refresh" in api_js
    assert "pageRefreshToken" in api_js
    assert "sessionStorage" in api_js
    assert "localStorage" in api_js
    assert "warmApiCache" in api_js
    assert "clearApiCache();" in api_js
    assert 'port === "8080"' in api_js

    assert "setupAppPreload" in ui_js
    assert "/investment/return-series?start=2026-01-01&interval=daily&baseCurrency=CNY" in holdings_js
    assert "/api/analysis/lookthrough?baseCurrency=CNY" in lookthrough_js
    assert "/api/analysis/return-attribution?baseCurrency=CNY" in lookthrough_js
    preload_block = ui_js.split("warmApiCache([", 1)[1].split("]).catch", 1)[0]
    assert "/investment/return-series?start=2026-01-01&interval=daily&baseCurrency=CNY" not in preload_block
    assert "/api/analysis/lookthrough?baseCurrency=CNY" not in preload_block
    assert "/api/analysis/return-attribution?baseCurrency=CNY" not in preload_block
    assert "assets/js/ledger_cashflow.js?v=20260621-ledger-overview-period-v2" in ui_js
    assert "assets/css/style.css?v=20260620-mobile-density-v1" in ui_js
    assert "assets/css/design-system.css?v=20260620-visual-cohesion-v1" in ui_js
    assert "assets/js/ui.js?v=20260620-data-state-v1" in ui_js
    assert "assets/js/holdings.js?v=20260620-data-state-v1" in ui_js
    assert "assets/js/holdings_categories.js?v=20260620-category-detail-hidden-v1" in ui_js
    assert "assets/js/lookthrough.js?v=20260620-data-state-v1" in ui_js
    assert "assets/js/transactions.js?v=20260620-data-state-v1" in ui_js
    assert "assets/js/portfolio_risk.js?v=20260620-data-state-v1" in ui_js
    assert "assets/js/portfolio_config.js?v=20260620-data-state-v1" in ui_js
    assert "assets/js/more.js?v=20260620-data-state-v1" in ui_js
    assert "risk_metrics.js" not in ui_js
    assert "/api/risk/summary" in api_js + ui_js

    assert "gzip on;" in dockerfile
    assert "location /holdings/" in dockerfile
    assert "summary2|nav|risk|cashflow|policy|watchlist" in dockerfile
    assert "proxy_pass http://pams-api:8000;" in dockerfile
    assert 'location /assets/' in dockerfile
    assert 'public, max-age=300, stale-while-revalidate=1800' in dockerfile
    assert "returnAttributionOverride" not in lookthrough_js
    assert "holdingsKpiSummary" not in lookthrough_js
    assert "盈亏总额以持仓页最终 KPI 为准" not in lookthrough_js
    assert "/summary2/latest?baseCurrency=CNY" in lookthrough_js
    assert "ReturnAttributionAssetHeatmap" in lookthrough_js
    assert "returnAttributionHeatmapRows" in lookthrough_js
    assert "returnAttributionLookupHoldingDaily" in lookthrough_js
    assert "['holding', '标的']" in lookthrough_js
    assert "['industry', '行业']" in lookthrough_js
    assert "['region', '地区']" in lookthrough_js
    assert "['theme', '主题']" in lookthrough_js
    assert "['underlying', '基金底层']" in lookthrough_js
    return_attribution_dimensions = lookthrough_js.split("const RETURN_ATTRIBUTION_DIMENSIONS = [", 1)[1].split("];", 1)[0]
    assert "['currency', '币种']" not in return_attribution_dimensions
    assert "['asset_type', '资产类型']" not in return_attribution_dimensions
    assert "['market', '市种']" not in return_attribution_dimensions
    assert "dimensions?.[dimension]" in lookthrough_js
    assert "data-return-asset-heat-row" in lookthrough_js
    assert "标的盈亏热力图" in lookthrough_js
    assert "returnAttributionCompactName(label" in lookthrough_js
    assert "returnAttributionCompactSymbol" not in lookthrough_js
    assert "returnAttributionCards" not in lookthrough_js
    assert "safeStorageGet" in lookthrough_js
    assert "safeStorageSet" in lookthrough_js
    assert "localStorage.getItem('pams.lookthrough.dimension')" not in lookthrough_js
    assert "jget('/summary2/latest?baseCurrency=CNY', { cache: false })" in lookthrough_js
    assert "pams:load-trade-attribution" in lookthrough_js
    assert "returnAssetHeatmapCanvas" in css
    assert "--return-asset-name" in css


def test_holding_summary_uses_market_view_date() -> None:
    api_py = Path("backend/api/main.py").read_text(encoding="utf-8")

    assert "def _holdings_market_view_as_of" in api_py
    assert api_py.count("view_as_of=_holdings_market_view_as_of(cur)") >= 4


def test_investment_header_daily_pnl_uses_total_return_window() -> None:
    holdings_js = _read_public("assets/js/holdings_categories.js")
    api_js = _read_public("assets/js/api.js")

    assert "/summary2/latest?baseCurrency=CNY" in holdings_js
    assert "function canonicalPortfolioDashboardSummary" in holdings_js
    assert "jgetFresh('/holdings/by-category/A股?minor_category=A股&limit=500&include_closed=true')" in holdings_js
    assert "jgetFresh(withClosedParam('/holdings/by-category/美股?limit=500'))" in holdings_js
    assert "jgetFresh('/summary2/latest?baseCurrency=CNY')" in holdings_js
    assert "jgetFresh('/api/portfolio/valuation-state?baseCurrency=CNY')" in holdings_js
    assert "totals.daily_pnl" in holdings_js
    assert "totals.market_value" in holdings_js
    assert "totals.holding_pnl" in holdings_js
    assert "totals.current_float_pnl" in holdings_js
    assert "当前浮盈" in holdings_js
    assert "总负债" in holdings_js
    assert "liabilities_cny" in holdings_js
    assert "const authoritativeDailyPnl" in holdings_js
    assert "dashboardSummary.dailyPnl = authoritativeDailyPnl" in holdings_js
    assert 'raw.includes("/api/portfolio/risk-disclosure")' in api_js
    assert "return 120_000" in api_js


def test_investment_holding_rows_escape_imported_fields() -> None:
    holdings_js = _read_public("assets/js/holdings.js")

    assert "function esc(value)" in holdings_js
    assert "${esc(r.symbol || '—')}" in holdings_js
    assert "${esc(r.name || '')}${closedBadge(r)}" in holdings_js
    assert "${esc(holdingGroupLabel(r))}" in holdings_js
    assert "${esc(row.name || row.symbol || '缺数据')}" in holdings_js
    assert "${row.name || row.symbol || '缺数据'}" not in holdings_js
