from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_ROOT = REPO_ROOT / "frontend" / "public"


def _read_public(path: str) -> str:
    return (PUBLIC_ROOT / path).read_text(encoding="utf-8")


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
    assert "const primary = new Set(['investments.html', 'ledger.html', 'more.html'])" in ui_js

    for page in [
        "index.html",
        "accounts.html",
        "investments.html",
        "ledger.html",
    ]:
        html = _read_public(page)
        assert "appActionRow" not in html


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
    assert (PUBLIC_ROOT / "risk.html").exists()

    for legacy_alias in ["bank_reconcile", "cashflow", "holdings", "strategy", "transactions", "watchlist"]:
        assert f"{legacy_alias}:" not in ui_js


def test_ledger_page_uses_card_list_navigation_contract() -> None:
    html = _read_public("ledger.html")
    js = _read_public("assets/js/ledger_cashflow.js")
    css = _read_public("assets/css/style.css")

    assert 'class="ledgerAnchorNav"' in html
    for href in [
        "#cashflow-workbench",
        "#recent-flow-section",
        "#income-section",
        "#accountIssueStrip",
        "#transfers",
        "#review-section",
    ]:
        assert f'href="{href}"' in html

    assert 'id="reviewRows" class="ledgerList"' in html
    assert 'id="recentRows" class="ledgerList"' in html
    assert "ledgerRowsTable" not in html
    assert "mobileReviewRows" not in html
    assert "<table" not in html
    assert "overflow-x:auto" not in html

    assert "ledgerListRow" in js
    assert "renderMobileLedgerRows" not in js
    assert "<tr" not in js
    assert 'href="#review-section"' in js
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

    for fn in [
        "InvestmentSummaryHeader",
        "ProfitContributionPanel",
        "ReturnMethodBadge",
        "DataQualityBanner",
        "CategoryDetailTabs",
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
    assert "jget('/holdings/latest?limit=500').catch(() => null)" in holdings_js
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

    assert "assets/js/holdings.js?v=20260617-linkage-v1" in html
    assert "assets/js/holdings_categories.js?v=20260619-valuation-state-v1" in html
    assert "assets/js/lookthrough.js?v=20260618-nav-field-v1" in html
    assert "assets/js/portfolio_risk.js?v=20260617-linkage-v1" in html
    assert "assets/js/portfolio_config.js?v=20260617-linkage-v1" in html
    assert "style.css?v=20260617-return-window-v1" in html
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
    assert "摊薄成本" in holding_rows_js
    assert "realizedPnlNote" in holding_rows_js
    assert "holdingAveragePriceText" in holding_rows_js
    assert "dilutedAveragePriceText" in holding_rows_js
    assert "currentFloatPnl" in holding_rows_js
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

    assert "assets/js/ledger_cashflow.js?v=20260607-shared-format-v1" in ledger_html
    assert "assets/js/transactions.js?v=20260617-linkage-v1" in investments_html


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
        "韩国/其他权益",
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
    assert "TradeAttributionAnalysisEntry" in tx_js
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
    assert "标的热力图" in tx_js
    assert "真实盈亏看交易页；这里看动作时点" in tx_js
    assert "交易动作明细" in tx_js
    assert "动作归因" in tx_js
    assert "正向 / 负向动作" in tx_js
    assert ".slice(0, 6)" not in tx_js
    assert "负向动作" in tx_js
    assert "正向动作" in tx_js
    asset_heatmap_block = tx_js.split("function TradeReviewAssetHeatmap", 1)[1].split("function tradeReviewPatternTiles", 1)[0]
    assert "TradeReviewDirectionalTradePanel(payload)" in asset_heatmap_block
    assert "TradeReviewPatternPanel" not in asset_heatmap_block
    assert "TradeReviewDataGaps" not in asset_heatmap_block
    assert "<strong>模式</strong>" not in asset_heatmap_block
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
    ledger_summary_grid_css = css.split(".page-investments #investment-ledger .transactionSummaryGrid", 1)[1].split("}", 1)[0]
    assert "display: grid !important" in ledger_summary_grid_css
    assert "display: none" not in ledger_summary_grid_css
    assert "TradeAttributionPanel(state.attribution)" not in tx_js
    assert "tradeAutoAttribution" in tx_js
    assert "tradeAttributionGrid" in css
    assert "tradeAttributionEntry" in css
    assert "tradeAutoMetricGrid" in css
    assert "asset.display_name || asset.name" in tx_js
    assert "start: ''," in tx_js
    assert "assets/js/transactions.js?v=20260617-linkage-v1" in html


def test_more_page_consolidates_low_frequency_entries() -> None:
    html = _read_public("more.html")
    accounts_html = _read_public("accounts.html")

    assert "appTaskList" not in html
    assert 'id="sync"' not in html
    assert 'id="syncSummaryStrip"' not in html
    assert 'id="syncWorkflow"' not in html
    assert 'id="syncCenterRows"' not in html
    assert "data-sync-scope" not in html

    for href in [
        "data_sources.html#dataQuality",
        "accounts.html#sources",
        "accounts.html#overview",
        "investments.html#asset-master-panel",
        "risk.html",
        "research.html",
    ]:
        assert html.count(f'href="{href}"') == 1
    for removed_text in ["账户核准", "待确认流水", "投资同步", "资产结构", "同步中心", "投资交易记录", "内部转账匹配"]:
        assert removed_text not in html
    assert "investments.html#investment-ledger" not in html
    assert "ledger.html#transfers" not in html
    assert "more.html#sync" not in html + accounts_html


def test_risk_page_prioritizes_user_configured_metrics() -> None:
    html = _read_public("risk.html")
    js = _read_public("assets/js/risk_metrics.js")
    css = _read_public("assets/css/style.css")
    dockerfile = (REPO_ROOT / "frontend" / "Dockerfile").read_text(encoding="utf-8")

    for element_id in [
        "riskHeroStatus",
        "riskMetricGrid",
        "riskPriorityList",
        "riskSourceList",
        "riskMethodNotes",
    ]:
        assert f'id="{element_id}"' in html

    assert "/api/risk/summary" in js
    assert "user_metrics" in js
    assert "股票 / 权益暴露" not in js
    assert "riskMetricDeck" in css
    assert "riskLimitCard" in css
    assert "riskAnchorNav" not in css
    assert "location = /risk.html { return 404; }" not in dockerfile


def test_frontend_uses_short_ttl_api_cache_and_static_asset_cache() -> None:
    api_js = _read_public("assets/js/api.js")
    ui_js = _read_public("assets/js/ui.js")
    holdings_js = _read_public("assets/js/holdings_categories.js")
    lookthrough_js = _read_public("assets/js/lookthrough.js")
    dockerfile = (REPO_ROOT / "frontend" / "Dockerfile").read_text(encoding="utf-8")

    assert "API_CACHE_PREFIX" in api_js
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
    assert "assets/js/ledger_cashflow.js?v=20260607-shared-format-v1" in ui_js
    assert "assets/css/style.css?v=20260617-return-window-v1" in ui_js
    assert "assets/js/holdings.js?v=20260617-linkage-v1" in ui_js
    assert "assets/js/holdings_categories.js?v=20260618-return-date-v4" in ui_js
    assert "assets/js/lookthrough.js?v=20260618-nav-field-v1" in ui_js
    assert "assets/js/transactions.js?v=20260617-linkage-v1" in ui_js
    assert "assets/js/portfolio_risk.js?v=20260617-linkage-v1" in ui_js
    assert "assets/js/portfolio_config.js?v=20260617-linkage-v1" in ui_js
    assert "assets/js/more.js?v=20260607-more-ia-v1" in ui_js
    assert "assets/js/risk_metrics.js?v=20260607-risk-metrics-v1" in ui_js
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


def test_holding_summary_uses_market_view_date() -> None:
    api_py = Path("backend/api/main.py").read_text(encoding="utf-8")

    assert "def _holdings_market_view_as_of" in api_py
    assert api_py.count("view_as_of=_holdings_market_view_as_of(cur)") >= 4


def test_investment_header_daily_pnl_uses_total_return_window() -> None:
    holdings_js = _read_public("assets/js/holdings_categories.js")
    api_js = _read_public("assets/js/api.js")

    assert "/summary2/latest?baseCurrency=CNY" in holdings_js
    assert "portfolioSummary?.totals?.daily_pnl" in holdings_js
    assert "portfolioSummary?.totals?.market_value" in holdings_js
    assert "portfolioSummary?.totals?.holding_pnl" in holdings_js
    assert "const authoritativeDailyPnl" in holdings_js
    assert "dashboardSummary.dailyPnl = authoritativeDailyPnl" in holdings_js
    assert 'raw.includes("/api/portfolio/risk-disclosure")' in api_js
    assert "return 120_000" in api_js
