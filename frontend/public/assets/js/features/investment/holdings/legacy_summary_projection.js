import {
  cnyValue, accountPrimaryBalanceCny, accountNetLiquidationFromHoldings, fmtCny,
  fmtSignedCny, isInternalInvestmentLabel, latestSyncTimestamp, canonicalPortfolioDashboardSummary,
  categoryByLabel, isSameOrAfterDate, isFreshPayloadAsOf, makeMobileHeroProfiles,
  confirmedIbkrAccountSummary, summarizeRows, holdingReturnRateFromSummary, withReportedPnlRows,
  withEsopDisplayRows, applyReportedReturnTotal, categoryMetric, buildContributionRows,
  deriveDataQualityIssues
} from './summary_legacy_model.js?v=20261102-incomplete-scope-v1';
import { fmt2 } from '../../../api.js?v=20261031-modules-v1';
import { safeNum } from '../../../utils.js?v=20261031-modules-v1';
import { accountCashBreakdown } from '../../../account_cash.js?v=20261031-modules-v1';
import { CHART_THEME, SERIES_COLORS } from '../../../shared/chart_theme.js?v=20261031-modules-v1';

// Legacy projection statements retained verbatim; dependencies are page-owned.
export function applyLegacySummaryPayload(payloads, context) {
  const { document, Chart, showClosedPositions, view } = context;
  const { setKpiLabel, renderIbkrPerformance, renderAStockOwnerPerformance, renderRowStock, renderRowFundLike, renderSection, writeSectionFinancials, renderReturnNotes, applySectionReturnMetrics, applyStaticReturnMetrics, publishHoldingsKpiSummary, renderInvestmentDashboard } = view;
  const [aStockEquity, aFund, aFundAll, usStock, preciousMetal, esop, latestHoldings, accountOverview, fxData, ibkrPerf, aStockPerf, reportedReturns, portfolioSummary, portfolioValuationState] = payloads;
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
    const fundSummary = {
      ...fundSummaryBase,
      holdingReturnRate: holdingReturnRateFromSummary(fundDisplaySummary),
      totalReturnRate: holdingReturnRateFromSummary(fundDisplaySummary),
      activeHoldingPnl: fundDisplaySummary.activeHoldingPnl,
      activeTotalCost: fundDisplaySummary.activeTotalCost,
    };
    setKpiLabel('totalPnlAFund', '投资收益');
    writeSectionFinancials({ ...fundSummary, totalValue: fundDisplaySummary.totalValue }, 'totalValueAFund', 'totalPnlAFund', 'dailyPnlAFund', 'holdingPnlAFund');
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
    const ibkrSummary = renderIbkrPerformance(ibkrPerf, usStock.as_of);
    const confirmedIbkrSummary = confirmedIbkrAccountSummary(ibkrPerf, usStock.as_of);
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

    const ibkrFlexCoversPositionDate = Boolean(ibkrSummary?.flexCoversPositionDate);
    const ibkrHoldingsNet = accountNetLiquidationFromHoldings(latestHoldings, 'ibkr');
    const ibkrHoldingsCoversPositionDate = Boolean(
      ibkrHoldingsNet?.netValue != null &&
      isSameOrAfterDate(ibkrHoldingsNet.asOf, usStock.as_of)
    );
    const ibkrOverviewBalance = accountPrimaryBalanceCny(accountOverview, 'ibkr', fxRates);
    const ibkrOverviewCoversPositionDate = Boolean(
      ibkrOverviewBalance?.amountCny != null &&
      safeNum(ibkrOverviewBalance.amountCny) > 0 &&
      isSameOrAfterDate(ibkrOverviewBalance.asOf, usStock.as_of)
    );
    const ibkrHasAccountTotalPnl = ibkrSummary?.totalPnl != null && Number.isFinite(Number(ibkrSummary.totalPnl));
    const ibkrHasAccountNetDeposit = ibkrSummary?.totalCost != null && Number.isFinite(Number(ibkrSummary.totalCost)) && safeNum(ibkrSummary.totalCost) > 0;
    const useIbkrAccountTotals = Boolean(
      ibkrSummary &&
      ibkrFlexCoversPositionDate &&
      (ibkrHasAccountTotalPnl || ibkrHasAccountNetDeposit) &&
      (!ibkrPerf?.daily_close || Boolean(confirmedIbkrSummary))
    );
    const ibkrUsesNetLiquidation = Boolean(
      useIbkrAccountTotals || ibkrHoldingsCoversPositionDate || ibkrOverviewCoversPositionDate
    );
    const ibkrHasFlexCashValue = useIbkrAccountTotals && Number.isFinite(Number(ibkrSummary?.cashValue));
    const ibkrPositionValue = confirmedIbkrSummary
      ? confirmedIbkrSummary.positionValue
      : ibkrHoldingsCoversPositionDate
      ? safeNum(ibkrHoldingsNet.grossAssets)
      : safeNum(usSummary.totalValue);
    const ibkrCashValue = confirmedIbkrSummary
      ? confirmedIbkrSummary.cashValue
      : ibkrHasFlexCashValue
      ? safeNum(ibkrSummary.cashValue)
      : (ibkrHoldingsCoversPositionDate ? safeNum(ibkrHoldingsNet.liabilities) : 0);
    const ibkrAccountValue = confirmedIbkrSummary
      ? confirmedIbkrSummary.totalValue
      : useIbkrAccountTotals
      ? (ibkrHasFlexCashValue ? ibkrPositionValue + ibkrCashValue : safeNum(ibkrSummary?.totalValue))
      : (ibkrHoldingsCoversPositionDate
        ? safeNum(ibkrHoldingsNet.netValue)
        : (ibkrOverviewCoversPositionDate ? safeNum(ibkrOverviewBalance.amountCny) : usSummary.totalValue));
    const ibkrNetDeposit = useIbkrAccountTotals && ibkrHasAccountNetDeposit
      ? safeNum(ibkrSummary?.totalCost)
      : safeNum(usSummary.totalCost);
    const ibkrDerivedTotalPnl = confirmedIbkrSummary
      ? confirmedIbkrSummary.totalPnl
      : useIbkrAccountTotals
      ? (ibkrHasAccountNetDeposit && ibkrNetDeposit > 0
        ? ibkrAccountValue - ibkrNetDeposit
        : safeNum(ibkrSummary?.totalPnl))
      : safeNum(usSummary.totalPnl);
    const usGrossHoldingReturnRate = holdingReturnRateFromSummary(usSummary);
    const ibkrRateEvidence = confirmedIbkrAccountSummary(ibkrPerf);
    const ibkrAccountReturnRate = confirmedIbkrSummary
      ? confirmedIbkrSummary.totalReturnRate
      : useIbkrAccountTotals && ibkrNetDeposit > 0
      ? ibkrDerivedTotalPnl / ibkrNetDeposit
      : (useIbkrAccountTotals && ibkrSummary?.totalReturnRate != null && Number.isFinite(Number(ibkrSummary.totalReturnRate))
        ? safeNum(ibkrSummary.totalReturnRate)
        : null);
    const usCategorySummary = (ibkrSummary || ibkrUsesNetLiquidation)
      ? {
        ...usSummary,
        totalValue: ibkrUsesNetLiquidation ? ibkrAccountValue : usSummary.totalValue,
        totalCost: confirmedIbkrSummary ? confirmedIbkrSummary.totalCost : (ibkrUsesNetLiquidation ? (ibkrNetDeposit || usSummary.totalCost) : usSummary.totalCost),
        totalPnl: ibkrUsesNetLiquidation ? ibkrDerivedTotalPnl : usSummary.totalPnl,
        totalReturnRate: ibkrUsesNetLiquidation ? ibkrAccountReturnRate : usGrossHoldingReturnRate,
        holdingReturnRate: usGrossHoldingReturnRate,
        displayReturnRate: ibkrUsesNetLiquidation ? ibkrAccountReturnRate : usGrossHoldingReturnRate,
        accountReturnRate: ibkrAccountReturnRate,
        accountReturnRateUndefined: Boolean(ibkrUsesNetLiquidation && ibkrAccountReturnRate == null),
        accountReturnRateAsOf: confirmedIbkrSummary?.asOf || null,
        accountReturnRateEvidenceAsOf: ibkrRateEvidence?.asOf || null,
        grossHoldingReturnRate: usGrossHoldingReturnRate,
        returnRateLabel: ibkrUsesNetLiquidation ? '账户资金收益率' : '持仓收益率',
        activeHoldingPnl: usSummary.activeHoldingPnl,
        activeTotalCost: usSummary.activeTotalCost,
        positionValue: ibkrPositionValue,
        cashValue: ibkrCashValue,
        accountAsOf: confirmedIbkrSummary?.asOf || ibkrHoldingsNet?.asOf || ibkrSummary?.asOf,
        accountValueAsOf: confirmedIbkrSummary?.asOf || ibkrHoldingsNet?.asOf || ibkrOverviewBalance?.asOf || ibkrSummary?.asOf,
        accountValueSource: confirmedIbkrSummary ? 'ibkr_flex' : ibkrHoldingsCoversPositionDate
          ? 'holdings_snapshot_net_liquidation'
          : (ibkrOverviewCoversPositionDate ? ibkrOverviewBalance.source : 'holdings_position_value'),
        positionAsOf: usStock.as_of,
        flexCoversPositionDate: ibkrFlexCoversPositionDate,
        holdingsNetCoversPositionDate: ibkrHoldingsCoversPositionDate,
        dailyPnl: usSummary.dailyPnl,
        priceDailyPnl: usSummary.dailyPnl,
        fxDailyPnl: usSummary.fxDailyPnl,
        dailyTotalPnl: usSummary.dailyTotalPnl,
      }
      : usSummary;
    if (ibkrUsesNetLiquidation) {
      setKpiLabel('totalValueUSStock', '账户净值');
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
        cashLiabilities: accountCash.cashLiabilities + Math.max(-ibkrCashValue, 0),
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
      ibkrPerf,
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
      fundDisplaySummary,
      fundSummary: { ...fundSummary, totalReturnRate: categoryByLabel(dashboardCategories, '基金').totalReturnRate },
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
