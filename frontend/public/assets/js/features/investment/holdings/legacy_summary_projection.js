import {
  cnyValue, accountPrimaryBalanceCny, accountNetLiquidationFromHoldings, fmtCny,
  fmtSignedCny, isInternalInvestmentLabel, latestSyncTimestamp, canonicalPortfolioDashboardSummary,
  categoryByLabel, isSameOrAfterDate, isFreshPayloadAsOf, makeMobileHeroProfiles,
  summarizeRows, holdingReturnRateFromSummary, withReportedPnlRows,
  withEsopDisplayRows, applyReportedReturnTotal, categoryMetric, buildContributionRows,
  deriveDataQualityIssues
} from './summary_legacy_model.js';
import { fmt2 } from '../../../api.js';
import { safeNum } from '../../../utils.js';
import { accountCashBreakdown } from '../../../account_cash.js';
import { CHART_THEME, SERIES_COLORS } from '../../../shared/chart_theme.js';

// Legacy projection statements retained verbatim; dependencies are page-owned.
export function applyLegacySummaryPayload(payloads, context) {
  const { document, Chart, showClosedPositions, view } = context;
  const { setKpiLabel, renderAStockOwnerPerformance, renderRowStock, renderRowFundLike, renderSection, writeSectionFinancials, renderReturnNotes, applySectionReturnMetrics, applyStaticReturnMetrics, publishHoldingsKpiSummary, renderInvestmentDashboard } = view;
  const [aStockEquity, aFund, aFundAll, usStock, preciousMetal, esop, latestHoldings, accountOverview, fxData, aStockPerf, reportedReturns, portfolioSummary, portfolioValuationState, accountPositions, closedTradeReturns] = payloads;
    const aStockReturn = null;
    const aFundReturn = null;
    const usStockReturn = null;
    const preciousMetalReturn = null;

    // 构建汇率映射: {CNY: 1, USD: 7.25, MXN: 0.35, ...}
    const fxRates = { CNY: 1 };
    for (const r of fxData.rates || []) {
      fxRates[r.quote_currency] = Number(r.rate);
    }

    const asof = aStockEquity.as_of || aFund.as_of || usStock.as_of || preciousMetal.as_of || esop.as_of || '--';
    const latestSyncAt = latestSyncTimestamp(latestHoldings);
    document.getElementById('asof').textContent = asof;

    const reportedReturnsFresh = isFreshPayloadAsOf(reportedReturns, asof);
    const fundReport = reportedReturnsFresh ? reportedReturns?.sections?.a_share_fund : null;
    const preciousReport = reportedReturnsFresh ? reportedReturns?.sections?.precious_metals : null;
    const aStockEquityRows = (aStockEquity.rows || []).filter((row) => {
      if (!row.is_closed || showClosedPositions()) return true;
      return Math.abs(safeNum(cnyValue(row, 'daily_pnl'))) > 0.005;
    });
    const fundRows = withReportedPnlRows(aFund.rows || [], fundReport);
    const fundSummaryRows = withReportedPnlRows(aFundAll.rows || aFund.rows || [], fundReport);
    const esopRows = withEsopDisplayRows(esop.rows || []);

    const aStockRowsSummary = renderSection(aStockEquityRows, renderRowStock, 'rowsAStockEquity', 'countAStockEquity', 'totalValueAStockEquity', 'totalPnlAStockEquity', 'dailyPnlAStockEquity', 'countKpiAStockEquity', 'holdingPnlAStockEquity', fxRates);
    const aStockHoldingRateSummary = summarizeRows((aStockEquity.rows || []).filter((row) => !row.is_closed));
    const aStockAccountSummary = renderAStockOwnerPerformance(aStockPerf);
    const aStockAccountDailyPnlEffective = Boolean(
      aStockAccountSummary &&
      aStockAccountSummary.dailyPnlEffective !== false &&
      aStockAccountSummary.dailyPnl != null &&
      Number.isFinite(Number(aStockAccountSummary.dailyPnl))
    );
    const aStockSummary = aStockAccountSummary
      ? {
        ...aStockRowsSummary,
        ...aStockAccountSummary,
        dailyPnl: aStockAccountDailyPnlEffective
          ? aStockAccountSummary.dailyPnl
          : aStockRowsSummary.dailyPnl,
        priceDailyPnl: aStockAccountDailyPnlEffective ? aStockAccountSummary.dailyPnl : aStockRowsSummary.dailyPnl,
        fxDailyPnl: aStockAccountDailyPnlEffective ? 0 : aStockRowsSummary.fxDailyPnl,
        dailyTotalPnl: aStockAccountDailyPnlEffective
          ? aStockAccountSummary.dailyPnl
          : aStockRowsSummary.dailyTotalPnl,
        dailyPnlEffective: aStockAccountDailyPnlEffective,
        totalPnl: aStockAccountSummary.accountTotalPnl,
        totalReturnRate: aStockAccountSummary.accountReturnRate,
        holdingReturnRate: holdingReturnRateFromSummary(aStockHoldingRateSummary),
        displayReturnRate: aStockAccountSummary.accountReturnRate,
        returnRateLabel: '持仓收益率',
        activeHoldingPnl: aStockHoldingRateSummary.activeHoldingPnl,
        activeTotalCost: aStockHoldingRateSummary.activeTotalCost,
        positionValue: aStockRowsSummary.totalValue,
        cashValue: Math.max(safeNum(aStockAccountSummary.totalValue) - safeNum(aStockRowsSummary.totalValue), 0),
      }
      : {
        ...aStockRowsSummary,
        holdingReturnRate: holdingReturnRateFromSummary(aStockHoldingRateSummary),
        activeHoldingPnl: aStockHoldingRateSummary.activeHoldingPnl,
        activeTotalCost: aStockHoldingRateSummary.activeTotalCost,
      };
    const fundDisplaySummary = renderSection(fundRows, renderRowStock, 'rowsAFund', 'countAFund', 'totalValueAFund', 'totalPnlAFund', 'dailyPnlAFund', 'countKpiAFund', 'holdingPnlAFund', fxRates);
    const fundSummaryBase = applyReportedReturnTotal(summarizeRows(fundSummaryRows), fundSummaryRows, fundReport);
    const fundReturns = closedTradeReturns?.a_fund;
    const knownFundPnl = fundReturns?.known_holding_and_closed_pnl;
    const fundSummary = {
      ...fundSummaryBase,
      holdingReturnRate: holdingReturnRateFromSummary(fundDisplaySummary),
      totalReturnRate: holdingReturnRateFromSummary(fundDisplaySummary),
      activeHoldingPnl: fundDisplaySummary.activeHoldingPnl,
      activeTotalCost: fundDisplaySummary.activeTotalCost,
    };
    setKpiLabel('totalPnlAFund', knownFundPnl != null ? '已知盈亏' : '投资收益');
    writeSectionFinancials({
      ...fundSummary,
      totalValue: fundDisplaySummary.totalValue,
      ...(knownFundPnl != null ? {
        totalPnl: Number(knownFundPnl),
        accountReturnRateUndefined: true,
        returnRateLabel: '累计收益率',
      } : {}),
    }, 'totalValueAFund', 'totalPnlAFund', 'dailyPnlAFund', 'holdingPnlAFund');
    const usRows = usStock.rows || [];
    const usDisplaySummary = renderSection(usRows, renderRowStock, 'rowsUSStock', 'countUSStock', 'totalValueUSStock', 'totalPnlUSStock', 'dailyPnlUSStock', 'countKpiUSStock', 'holdingPnlUSStock', fxRates);
    const usHoldingRateSummary = summarizeRows(usRows.filter((row) => !row.is_closed));
    const usSummary = {
      ...usDisplaySummary,
      holdingReturnRate: holdingReturnRateFromSummary(usHoldingRateSummary),
      totalReturnRate: holdingReturnRateFromSummary(usHoldingRateSummary),
      activeHoldingPnl: usHoldingRateSummary.activeHoldingPnl,
      activeTotalCost: usHoldingRateSummary.activeTotalCost,
    };
    const ibkrOfficialValue = accountPositions?.by_account?.find(account => account.account_code === 'ibkr')?.portfolio_value;
    const preciousRows = preciousMetal.rows || [];
    const preciousDisplaySummary = renderSection(preciousRows, renderRowFundLike, 'rowsPreciousMetal', 'countPreciousMetal', 'totalValuePreciousMetal', 'totalPnlPreciousMetal', 'dailyPnlPreciousMetal', 'countKpiPreciousMetal', 'holdingPnlPreciousMetal', fxRates);
    const preciousHoldingRateSummary = summarizeRows(preciousRows.filter((row) => !row.is_closed));
    const preciousSummary = {
      ...preciousDisplaySummary,
      holdingReturnRate: holdingReturnRateFromSummary(preciousHoldingRateSummary),
      totalReturnRate: holdingReturnRateFromSummary(preciousHoldingRateSummary),
      activeHoldingPnl: preciousHoldingRateSummary.activeHoldingPnl,
      activeTotalCost: preciousHoldingRateSummary.activeTotalCost,
    };
    const esopDisplaySummary = renderSection(esopRows, renderRowFundLike, 'rowsESOP', 'countESOP', 'totalValueESOP', 'totalPnlESOP', 'dailyPnlESOP', 'countKpiESOP', 'holdingPnlESOP', fxRates);
    const esopHoldingRateSummary = summarizeRows(esopRows.filter((row) => !row.is_closed));
    const esopSummary = {
      ...esopDisplaySummary,
      holdingReturnRate: holdingReturnRateFromSummary(esopHoldingRateSummary),
      totalReturnRate: holdingReturnRateFromSummary(esopHoldingRateSummary),
      activeHoldingPnl: esopHoldingRateSummary.activeHoldingPnl,
      activeTotalCost: esopHoldingRateSummary.activeTotalCost,
    };
    renderReturnNotes(fundReport, 'returnNotesAFund');
    renderReturnNotes(preciousReport, 'returnNotesPreciousMetal');
    applySectionReturnMetrics(aStockReturn, { totalPnlId: 'totalPnlAStockEquity', dailyPnlId: 'dailyPnlAStockEquity', returnId: 'holdingPnlAStockEquity' });
    applySectionReturnMetrics(aFundReturn, { totalPnlId: 'totalPnlAFund', dailyPnlId: 'dailyPnlAFund', returnId: 'holdingPnlAFund' });
    applySectionReturnMetrics(usStockReturn, { totalPnlId: 'totalPnlUSStock', dailyPnlId: 'dailyPnlUSStock', returnId: 'holdingPnlUSStock' }, {
      dailyPnl: usSummary.dailyTotalPnl,
      fxDailyPnl: usSummary.fxDailyPnl,
    });
    applySectionReturnMetrics(preciousMetalReturn, { totalPnlId: 'totalPnlPreciousMetal', dailyPnlId: 'dailyPnlPreciousMetal', returnId: 'holdingPnlPreciousMetal' });
    applyStaticReturnMetrics(esopSummary, { totalPnlId: 'totalPnlESOP', dailyPnlId: 'dailyPnlESOP', returnId: 'holdingPnlESOP' }, { oncePerYear: true });

    const ibkrHoldingsNet = accountNetLiquidationFromHoldings(latestHoldings, 'ibkr');
    const ibkrHoldingsCoversPositionDate = Boolean(ibkrHoldingsNet?.netValue != null &&
      isSameOrAfterDate(ibkrHoldingsNet.asOf, usStock.as_of));
    const ibkrOverviewBalance = accountPrimaryBalanceCny(accountOverview, 'ibkr', fxRates);
    const ibkrOverviewCoversPositionDate = Boolean(ibkrOverviewBalance?.amountCny > 0 &&
      isSameOrAfterDate(ibkrOverviewBalance.asOf, usStock.as_of));
    const ibkrUsesNetLiquidation = ibkrHoldingsCoversPositionDate || ibkrOverviewCoversPositionDate;
    const usCategorySummary = ibkrUsesNetLiquidation
      ? {
        ...usSummary,
        totalValue: ibkrHoldingsCoversPositionDate ? ibkrHoldingsNet.netValue : ibkrOverviewBalance.amountCny,
        positionValue: ibkrHoldingsCoversPositionDate ? ibkrHoldingsNet.grossAssets : usSummary.totalValue,
        cashValue: ibkrHoldingsCoversPositionDate ? ibkrHoldingsNet.liabilities : null,
        accountAsOf: ibkrHoldingsCoversPositionDate ? ibkrHoldingsNet.asOf : ibkrOverviewBalance.asOf,
        accountValueSource: ibkrHoldingsCoversPositionDate ? 'holdings_snapshot_net_liquidation' : ibkrOverviewBalance.source,
        holdingsNetCoversPositionDate: ibkrHoldingsCoversPositionDate,
        accountReturnRateUndefined: true,
      }
      : usSummary;
    if (ibkrUsesNetLiquidation) {
      setKpiLabel('totalValueUSStock', '净清算价值');
      writeSectionFinancials(
        { ...usCategorySummary, dailyPnl: usCategorySummary.dailyTotalPnl },
        'totalValueUSStock',
        'totalPnlUSStock',
        'dailyPnlUSStock',
        'holdingPnlUSStock'
      );
    }
    const accountCash = accountCashBreakdown(accountOverview, fxRates);
    const cashCategory = {
      label: '现金/活期',
      color: SERIES_COLORS[5],
      totalValue: accountCash.netCash,
      totalCost: Math.max(accountCash.cashAssets, 0),
      totalPnl: 0,
      dailyPnl: 0,
      priceDailyPnl: 0,
      fxDailyPnl: 0,
      dailyTotalPnl: 0,
      totalReturnRate: 0,
      cashAssets: accountCash.cashAssets,
      cashLiabilities: accountCash.cashLiabilities,
    };

    const categories = [
      { label: 'A股个股', color: SERIES_COLORS[0], ...aStockSummary },
      { label: '基金', color: SERIES_COLORS[1], ...fundSummary, totalValue: fundDisplaySummary.totalValue },
      { label: '美股', color: SERIES_COLORS[2], ...usCategorySummary },
      { label: '贵金属', color: SERIES_COLORS[3], ...preciousSummary },
      { label: '内部股份分红', color: SERIES_COLORS[4], ...esopSummary },
    ];
    if (Math.abs(cashCategory.totalValue) >= 0.005) categories.push(cashCategory);

    const canonicalSummary = canonicalPortfolioDashboardSummary({
      categories,
      portfolioSummary,
      portfolioValuationState,
      accountCash: {
        ...accountCash,
        cashLiabilities: accountCash.cashLiabilities + Math.max(-safeNum(usCategorySummary.cashValue), 0),
      },
      fallbackAsOf: asof,
    });
    let grandValue = canonicalSummary.marketValue;
    let grandPnl = canonicalSummary.totalPnl;
    let grandDaily = canonicalSummary.dailyPnl;
    const pnlPct = canonicalSummary.pnlPct;
    const currentFloatPnl = canonicalSummary.currentFloatPnl;

    document.getElementById('grandTotalValue').textContent = fmtCny(grandValue);

    const gpElem = document.getElementById('grandTotalPnl');
    gpElem.textContent = fmtSignedCny(grandPnl);
    gpElem.style.color = grandPnl >= 0 ? 'var(--good)' : 'var(--bad)';

    const pctElem = document.getElementById('grandTotalPnlPct');
    pctElem.textContent = (pnlPct >= 0 ? '+' : '') + pnlPct.toFixed(2) + '%';
    pctElem.style.color = pnlPct >= 0 ? 'var(--good)' : 'var(--bad)';

    const gdElem = document.getElementById('grandDailyPnl');
    gdElem.textContent = fmtSignedCny(grandDaily);
    gdElem.style.color = grandDaily >= 0 ? 'var(--good)' : 'var(--bad)';

    // 资产分布饼图（带百分比标签）
    const pieData = categories.filter(c => c.totalValue > 0);
    const pieTotal = pieData.reduce((s, c) => s + c.totalValue, 0);
    const pieCtx = document.getElementById('categoryPieChart');
    if (pieCtx && pieData.length) {
    new Chart(pieCtx, {
      type: 'doughnut',
      data: {
        labels: pieData.map(c => {
          const pct = ((c.totalValue / pieTotal) * 100).toFixed(1);
          return `${c.label} ${pct}%`;
        }),
        datasets: [{
          data: pieData.map(c => c.totalValue),
          backgroundColor: pieData.map(c => c.color),
          borderColor: CHART_THEME.background,
          borderWidth: 2
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: true,
        plugins: {
          legend: { position: 'bottom', labels: { color: CHART_THEME.text, padding: 16 } },
          tooltip: {
            callbacks: {
              label: (ctx) => {
                const pct = ((ctx.raw / pieTotal) * 100).toFixed(1);
                return `${ctx.label}: ¥${fmt2(ctx.raw)} (${pct}%)`;
              }
            }
          }
        }
      }
    });
    }

    const dashboardSummary = {
      scopeIncomplete: canonicalSummary.scopeIncomplete,
      marketValue: grandValue,
      totalPnl: grandPnl,
      currentFloatPnl,
      twr: pnlPct / 100,
      dailyPnl: grandDaily,
      ytdPnl: grandPnl,
      asOf: canonicalSummary.asOf,
      cashActiveValue: accountCash.netCash,
      cashActiveAssets: accountCash.cashAssets,
      cashActiveLiabilities: accountCash.cashLiabilities,
      totalLiabilities: canonicalSummary.totalLiabilities,
      valuationState: canonicalSummary.valuationState,
    };
    const dashboardCategories = [
      categoryMetric('A股个股', aStockSummary, aStockReturn),
      categoryMetric('基金', { ...fundSummary, totalValue: fundDisplaySummary.totalValue }, aFundReturn),
      categoryMetric('美股/海外', usCategorySummary, usStockReturn),
      categoryMetric('贵金属', preciousSummary, preciousMetalReturn),
      categoryMetric('内部股份/分红', esopSummary, null),
    ];
    if (Math.abs(cashCategory.totalValue) >= 0.005) dashboardCategories.push(cashCategory);
    const dashboardDailyCategories = dashboardCategories.filter(category => !isInternalInvestmentLabel(category.label));
    const dashboardDailyFromCategories = dashboardDailyCategories.reduce((sum, category) => sum + safeNum(category.dailyPnl), 0);
    const portfolioSummaryFreshForAsOf = isFreshPayloadAsOf(portfolioSummary, asof);
    const authoritativeDailyPnl = portfolioSummaryFreshForAsOf && portfolioSummary?.totals?.daily_pnl != null
      ? safeNum(portfolioSummary.totals.daily_pnl)
      : dashboardDailyFromCategories;
    dashboardSummary.dailyPnl = authoritativeDailyPnl;
    grandDaily = authoritativeDailyPnl;
    gdElem.textContent = fmtSignedCny(dashboardSummary.dailyPnl);
    gdElem.style.color = dashboardSummary.dailyPnl >= 0 ? 'var(--good)' : 'var(--bad)';
    dashboardSummary.dailyBreakdown = dashboardDailyCategories.map(category => ({
      label: category.label,
      dailyPnl: category.dailyPnl,
      priceDailyPnl: category.priceDailyPnl,
      fxDailyPnl: category.fxDailyPnl,
      dailyTotalPnl: category.dailyTotalPnl,
      totalPnl: category.totalPnl,
      totalValue: category.totalValue,
    }));
    publishHoldingsKpiSummary(dashboardSummary, dashboardCategories);
    const dataIssues = deriveDataQualityIssues({
      reportedReturns: reportedReturnsFresh ? reportedReturns : null,
      staleReportedReturns: Boolean(reportedReturns && !reportedReturnsFresh),
      ibkrOfficialValue,
      esopRows,
      fundRows,
      preciousRows: preciousMetal.rows || [],
    });
    const contributionRows = buildContributionRows([
      { label: 'A股个股', rows: aStockEquityRows },
      { label: '基金', rows: fundRows },
      { label: '美股/海外', rows: usStock.rows || [] },
      { label: '贵金属', rows: preciousMetal.rows || [] },
      { label: '内部股份/分红', rows: esopRows },
    ], dashboardSummary);
    const mobileProfiles = makeMobileHeroProfiles({
      summary: dashboardSummary,
      categories: dashboardCategories,
      accountOverview,
      fxRates,
      aStockRowsSummary,
      aStockSummary,
      aStockAccountPosition: accountPositions?.by_account?.find(account => account.account_code === 'tonghuashun'),
      aStockPositionsAsOf: accountPositions?.as_of,
      aStockAsOf: aStockEquity.as_of,
      aStockHoldingPerformance: aStockEquity.holding_performance,
      aStockAccountSummary,
      fundAccountPosition: accountPositions?.by_account?.find(account => account.account_code === 'tencent_licaitong'),
      ibkrOfficialValue,
      fundDisplaySummary,
      fundAccountPnl: fundReturns?.by_account?.tencent_licaitong,
      fundHoldingPerformance: aFundAll.holding_performance,
      usSummary,
      usRows,
      ibkrSummary: usCategorySummary,
      recentUsReturn: usStock.recent_confirmed_returns?.by_account?.ibkr || null,
      preciousSummary: { ...preciousSummary, totalReturnRate: categoryByLabel(dashboardCategories, '贵金属').totalReturnRate },
      esopSummary: { ...esopSummary, totalReturnRate: categoryByLabel(dashboardCategories, '内部股份/分红').totalReturnRate },
    });
    renderInvestmentDashboard({
      summary: dashboardSummary,
      categories: dashboardCategories,
      contributionRows,
      dataIssues,
      asOf: dashboardSummary.asOf,
      syncAt: latestSyncAt,
      mobileProfiles,
    });

}
