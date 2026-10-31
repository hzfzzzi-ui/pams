"""Read real feature functions for existing static boundary assertions."""
from pathlib import Path
import re

ORDER = {'holdings.js': ['esc', 'cnyValue', 'safeNum', 'roundMoneyCents', 'subtractMoneyCents', 'formatNumber', 'formatCurrency', 'formatCompactCurrency', 'formatSignedCurrency', 'signedMoneyClass', 'formatNativeDaily', 'dailyFxText', 'dailyNativeText', 'holdingDailyPnlPending', 'hasDailyTotalPnl', 'datedReturnRow', 'displayDailyPnl', 'dailyPnlBreakdown', 'dataBadge', 'holdingDailyStatusBadge', 'mergedDailyPnlStatus', 'showClosedPositions', 'withClosedParam', 'initClosedToggle', 'closedBadge', 'displayPnlPct', 'grossCnyValue', 'cumulativePnl', 'currentFloatPnl', 'holdingCostValue', 'realizedPnl', 'hasRealizedPnl', 'realizedPnlNote', 'dilutedAveragePrice', 'dilutedAveragePriceText', 'holdingGroupLabel', 'holdingGroupRank', 'holdingGroupTone', 'renderRows', 'shortHoldingName', 'holdingPriceDigits', 'holdingPriceText', 'holdingQuantityText', 'holdingAveragePrice', 'holdingAveragePriceText', 'holdingPriceChangePct', 'signedPercentText', 'cashRow', 'buildAccountCashRows', 'shouldAggregateForMobile', 'aggregateMobileHoldingRows', 'holdingSyncTime', 'renderMobileHoldingRows', 'isInvestmentRow', 'setupHoldingGroupFilter', 'main'], 'holdings_categories.js': ['jgetFresh', 'cnyValue', 'fxRateForCurrency', 'accountPrimaryBalanceCny', 'accountNetLiquidationFromHoldings', 'fmtUsd', 'fmtCny', 'fmtSignedUsd', 'fmtSignedCny', 'fmtPctValue', 'formatNumber', 'formatCurrency', 'formatCompactCurrency', 'formatTableNumber', 'formatPercentRate', 'profitColor', 'statusBadge', 'ReturnMethodBadge', 'metricCard', 'InvestmentSummaryHeader', 'nullableMoney', 'toneFromNumber', 'isInternalInvestmentLabel', 'InvestmentSummaryHeaderCompact', 'DailyReturnBreakdown', 'pctFromAmount', 'signedPctText', 'parseSyncDate', 'latestSyncTimestamp', 'formatSyncTimestamp', 'normalizeMobileHeroGroup', 'currentMobileHeroGroup', 'shareTone', 'formatOptionalMoney', 'resolvePortfolioLiabilitiesCny', 'canonicalPortfolioDashboardSummary', 'mobileHeroMetaItem', 'MobilePortfolioHero', 'categoryByLabel', 'isoDateKey', 'isSameOrAfterDate', 'latestDateKey', 'isFreshPayloadAsOf', 'freshValuationStateForTarget', 'makeMobileHeroProfiles', 'updateMobilePortfolioHero', 'bindMobileMethodology', 'DataQualityBanner', 'contributionPct', 'classifyDataQuality', 'normalizeHolding', 'formatNativeDaily', 'contributionItem', 'ContributionList', 'ProfitContributionPanel', 'topMoverRow', 'topMoverGroup', 'TopContributionPanel', 'showClosedPositions', 'withClosedParam', 'initClosedToggle', 'esc', 'setKpiLabel', 'renderIbkrPerformance', 'confirmedIbkrAccountSummary', 'renderAStockOwnerPerformance', 'renderRowStock', 'renderRowFundLike', 'closedBadge', 'displayPnlPct', 'renderSection', 'summarizeRows', 'writeSectionFinancials', 'returnRateFromCapitalBase', 'holdingReturnRateFromSummary', 'renderReturnNotes', 'reportedEntryKey', 'findReportedEntry', 'withReportedPnlRows', 'withEsopDisplayRows', 'applyReportedReturnTotal', 'fmtSignedPctFromRate', 'latestSectionReturnAmount', 'sectionTodayReturnAmount', 'setKpiFxNote', 'applySectionReturnMetrics', 'annualizedRateFromPeriod', 'applyStaticReturnMetrics', 'renderReturnMethodBar', 'categoryReturnOverride', 'categoryMetric', 'buildContributionRows', 'publishHoldingsKpiSummary', 'buildInvestmentConclusion', 'deriveDataQualityIssues', 'renderDataQualityPanel', 'renderInvestmentDashboard', 'AssetAllocationPanel', 'bindContributionTabs', 'bindInvestmentPageTabs', 'runWhenInvestmentAnalysisVisible', 'canLoadAnalysisDetail', 'isInvestmentAnalysisHash', 'loadCategoryReturnChart', 'categoryDetailShell', 'isCategoryDetailOpen', 'openCategoryDetailShell', 'loadVisibleCategoryReturnChart', 'loadAllCategoryReturnCharts', 'queueVisibleCategoryReturnChart', 'getInvestmentCharts', 'getInvestmentChartState', 'loadInvestmentNavChartOnce', 'renderInvestmentReturnChart', 'loadInvestmentReturnChartOnce', 'applyInvestmentReturnWindow', 'disposeInvestmentCharts', 'setupAnalysisHeavyLoaders', 'CategoryDetailTabs', 'promoteHoldingsTable', 'main']}

def read_holdings_source(public: Path, entry: str) -> str:
    directory = public / 'assets/js/features/investment/holdings'
    names = (['list_legacy_model.js', 'list_view.js', 'list_controller.js'] if entry == 'holdings.js'
             else ['summary_legacy_model.js', 'summary_view.js', 'summary_controller.js', 'legacy_summary_projection.js'])
    names += ['constants.js', 'closed_positions.js', 'requests.js']
    functions, rest = {}, []
    for name in names:
        source = (directory / name).read_text(encoding='utf-8')
        spans = []
        for function_name in ORDER[entry]:
            start = re.search(r'^(?:export )?(?:async )?function ' + function_name + r'\(', source, re.M)
            if not start:
                continue
            end = re.search(r'^}\s*$', source[start.start():], re.M)
            stop = start.start() + end.end()
            functions[function_name] = source[start.start():stop].removeprefix('export ')
            spans.append((start.start(), stop))
        for start, stop in sorted(spans, reverse=True):
            source = source[:start] + source[stop:]
        rest.append(source)
    return '\n'.join(functions[name] for name in ORDER[entry] if name in functions) + '\n' + '\n'.join(rest)
