// Compatibility-only investment model; legacy field choices and formula bodies
// remain unchanged. Do not reuse these fallbacks as a new financial engine.
import { safeNum, pnlPct } from '../../../utils.js?v=20261031-modules-v1';
import { accountCashBreakdown } from '../../../account_cash.js?v=20261031-modules-v1';
import { accountLabel } from '../../../api.js?v=20261031-modules-v1';
export function cnyValue(r, key) {
  const cnyKey = `${key}_cny`;
  return r[cnyKey] ?? r[key] ?? 0;
}

export function fxRateForCurrency(currency, fxRates = {}) {
  const ccy = String(currency || 'CNY').trim().toUpperCase();
  if (!ccy || ccy === 'CNY') return 1;
  const rate = Number(fxRates[ccy]);
  return Number.isFinite(rate) && rate > 0 ? rate : 1;
}

export function accountPrimaryBalanceCny(accountOverview, accountCode, fxRates = {}) {
  const account = (accountOverview?.accounts || []).find(item => String(item.account_code || '') === accountCode);
  const balance = account?.primary_balance || account?.platform_balance || null;
  if (!balance || balance.amount == null || balance.amount === '') return null;
  const amount = Number(balance.amount);
  if (!Number.isFinite(amount)) return null;
  const boundCny = balance.amount_cny;
  const hasBoundCny = account.daily_close?.status === 'ok' &&
    (typeof boundCny === 'number' || (typeof boundCny === 'string' && boundCny.trim() !== '')) &&
    Number.isFinite(Number(boundCny));
  return {
    amountCny: hasBoundCny ? Number(boundCny) : amount * fxRateForCurrency(balance.currency || account.base_currency || 'CNY', fxRates),
    currency: String(balance.currency || account.base_currency || 'CNY').toUpperCase(),
    asOf: balance.as_of || account.as_of || null,
    source: balance.source || account.truth_source || 'account_overview',
  };
}

export function accountNetLiquidationFromHoldings(holdingsPayload, accountCode) {
  const rows = Array.isArray(holdingsPayload?.rows) ? holdingsPayload.rows : [];
  const accountRows = rows.filter((row) => {
    const code = String(row.account || row.account_code || '').trim().toLowerCase();
    return code === String(accountCode || '').trim().toLowerCase();
  });
  if (!accountRows.length) return null;

  let grossAssets = 0;
  let liabilities = 0;
  let netValue = 0;
  let asOf = holdingsPayload?.as_of || '';
  for (const row of accountRows) {
    const value = safeNum(cnyValue(row, 'market_value'));
    const liability = String(row.asset_liability || '') === '负债';
    netValue += value;
    if (liability) liabilities += value;
    else grossAssets += value;

    const rowAsOf = isoDateKey(row.snapshot_as_of || row.as_of);
    if (rowAsOf && (!asOf || rowAsOf > isoDateKey(asOf))) asOf = rowAsOf;
  }

  return {
    asOf,
    grossAssets,
    liabilities,
    netValue,
    rows: accountRows.length,
  };
}

export function fmtUsd(x) {
  return formatCurrency(x, 'USD', { maximumFractionDigits: 2 });
}

export function fmtCny(x) {
  return formatCurrency(x, 'CNY', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export function fmtSignedUsd(x) {
  return formatCurrency(x, 'USD', { signed: true, maximumFractionDigits: 2 });
}

export function fmtSignedCny(x) {
  return formatCurrency(x, 'CNY', { signed: true, minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export function fmtPctValue(x) {
  return (safeNum(x) * 100).toFixed(2) + '%';
}

export function formatNumber(value, options = {}) {
  const n = safeNum(value);
  return n.toLocaleString('zh-CN', {
    minimumFractionDigits: options.minimumFractionDigits ?? 0,
    maximumFractionDigits: options.maximumFractionDigits ?? 0,
  });
}

export function formatCurrency(value, currency = 'CNY', options = {}) {
  const n = safeNum(value);
  const symbol = currency === 'USD' ? '$' : currency === 'CNY' ? '¥' : `${currency} `;
  const sign = options.signed ? (n >= 0 ? '+' : '-') : '';
  return `${sign}${symbol}${formatNumber(Math.abs(n), {
    minimumFractionDigits: options.minimumFractionDigits ?? options.maximumFractionDigits ?? (currency === 'USD' ? 2 : 2),
    maximumFractionDigits: options.maximumFractionDigits ?? options.minimumFractionDigits ?? (currency === 'USD' ? 2 : 2),
  })}`;
}

export function formatCompactCurrency(value, currency = 'CNY', options = {}) {
  return formatCurrency(value, currency, { ...options, minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export function formatTableNumber(value, digits = 2) {
  if (value == null || value === '') return '待核准';
  return formatNumber(value, { minimumFractionDigits: 0, maximumFractionDigits: digits });
}

export function formatPercentRate(rate) {
  const pct = safeNum(rate) * 100;
  return `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%`;
}

export function profitColor(value) {
  return safeNum(value) >= 0 ? 'var(--good)' : 'var(--bad)';
}

export function nullableMoney(value, fallback = '待拆分') {
  return Number.isFinite(Number(value)) ? formatCompactCurrency(Number(value), 'CNY', { signed: true }) : fallback;
}

export function toneFromNumber(value, fallback = 'pending') {
  if (!Number.isFinite(Number(value))) return fallback;
  return Number(value) >= 0 ? 'good' : 'bad';
}

export function isInternalInvestmentLabel(label) {
  const text = String(label || '');
  return /ESOP/i.test(text) || text.includes('\u5185\u90e8\u80a1\u4efd');
}

export function pctFromAmount(amount, base) {
  const amountNum = safeNum(amount);
  const baseNum = safeNum(base);
  if (!baseNum) return 0;
  return amountNum / baseNum;
}

export function signedPctText(rate) {
  const pct = safeNum(rate) * 100;
  return `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%`;
}

export function parseSyncDate(value) {
  if (!value) return null;
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;
  const raw = String(value);
  if (/^\d{4}-\d{2}-\d{2}$/.test(raw)) return new Date(`${raw}T00:00:00+08:00`);
  const normalized = /T/.test(raw) && !/(Z|[+-]\d{2}:?\d{2})$/.test(raw) ? `${raw}Z` : raw;
  const parsed = new Date(normalized);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

export function latestSyncTimestamp(...payloads) {
  let latest = null;
  const visit = (item) => {
    if (!item) return;
    if (Array.isArray(item)) {
      item.forEach(visit);
      return;
    }
    if (typeof item !== 'object') return;
    for (const key of ['updated_at', 'sync_at', 'imported_at']) {
      const parsed = parseSyncDate(item[key]);
      if (parsed && (!latest || parsed > latest)) latest = parsed;
    }
    if (Array.isArray(item.rows)) item.rows.forEach(visit);
    if (Array.isArray(item.rates)) item.rates.forEach(visit);
  };
  payloads.forEach(visit);
  return latest;
}

export function formatSyncTimestamp(value, fallbackDate) {
  const parsed = parseSyncDate(value) || parseSyncDate(fallbackDate);
  if (!parsed) return '待核准';
  const parts = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
    timeZoneName: 'shortOffset',
  }).formatToParts(parsed).reduce((acc, part) => {
    acc[part.type] = part.value;
    return acc;
  }, {});
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second} ${parts.timeZoneName || 'GMT+8'}`;
}

export function normalizeMobileHeroGroup(group) {
  const text = String(group || '全部');
  if (text === '全部') return '全部';
  if (text.includes('A股个股')) return 'A股个股';
  if (text.includes('A股基金') || text === '基金') return 'A股基金';
  if (text.includes('美股') || text.includes('海外')) return '美股/海外';
  if (text.includes('贵金属') || text.includes('黄金')) return '贵金属';
  if (text.includes('内部') || text.includes('ESOP')) return '内部股份/分红';
  if (text.includes('现金') || text.includes('活期')) return '现金/活期';
  return text;
}

export function shareTone(share) {
  const value = safeNum(share);
  if (value < 0) return 'risk';
  const absValue = Math.abs(value);
  if (absValue >= 0.35) return 'risk';
  if (absValue >= 0.20) return 'warn';
  return 'ok';
}

export function formatOptionalMoney(value, currency = 'CNY', options = {}) {
  return Number.isFinite(Number(value))
    ? formatCompactCurrency(Number(value), currency, options)
    : '--';
}

export function resolvePortfolioLiabilitiesCny(valuationState, portfolioSummary, fallback = 0) {
  const valuationLiabilities = valuationState?.liabilities_cny;
  if (Number.isFinite(Number(valuationLiabilities))) return Math.abs(safeNum(valuationLiabilities));

  const summaryLiabilities = portfolioSummary?.valuation_state?.liabilities_cny;
  if (Number.isFinite(Number(summaryLiabilities))) return Math.abs(safeNum(summaryLiabilities));

  const liabilityClass = (portfolioSummary?.by_asset_class || []).find(
    row => String(row.asset_class || '').toUpperCase() === 'LIABILITY'
  );
  if (liabilityClass && Number.isFinite(Number(liabilityClass.market_value))) {
    return Math.abs(safeNum(liabilityClass.market_value));
  }

  return Math.max(0, safeNum(fallback));
}

export function canonicalPortfolioDashboardSummary({
  categories = [],
  portfolioSummary = null,
  portfolioValuationState = null,
  accountCash = null,
  fallbackAsOf = '--',
}) {
  const categoryValue = categories.reduce((sum, category) => sum + safeNum(category.totalValue), 0);
  const categoryPnl = categories.reduce((sum, category) => sum + safeNum(category.totalPnl), 0);
  const categoryDaily = categories
    .filter(category => !isInternalInvestmentLabel(category.label))
    .reduce((sum, category) => sum + safeNum(category.dailyPnl), 0);
  const categoryCost = categories.reduce(
    (sum, category) => sum + safeNum(category.totalCost ?? Math.max(safeNum(category.totalValue) - safeNum(category.totalPnl), 0)),
    0
  );
  const targetAsOf = latestDateKey(fallbackAsOf, portfolioSummary?.data_as_of_max, portfolioSummary?.as_of);
  const summaryFresh = isFreshPayloadAsOf(portfolioSummary, targetAsOf);
  const totals = summaryFresh ? (portfolioSummary?.totals || {}) : {};
  const valuationState = freshValuationStateForTarget(
    portfolioValuationState || portfolioSummary?.valuation_state || null,
    targetAsOf
  );
  const marketValue = valuationState?.net_liquidation_value != null
    ? safeNum(valuationState.net_liquidation_value)
    : (totals.market_value != null ? safeNum(totals.market_value) : categoryValue);
  const totalPnl = totals.holding_pnl != null ? safeNum(totals.holding_pnl) : categoryPnl;
  const currentFloatPnl = totals.current_float_pnl != null ? safeNum(totals.current_float_pnl) : totalPnl;
  const dailyPnl = totals.daily_pnl != null ? safeNum(totals.daily_pnl) : categoryDaily;
  const totalCost = totals.cost != null ? safeNum(totals.cost) : categoryCost;
  const totalLiabilities = resolvePortfolioLiabilitiesCny(
    valuationState,
    summaryFresh ? portfolioSummary : null,
    safeNum(accountCash?.cashLiabilities)
  );
  return {
    scopeIncomplete: portfolioSummary?.snapshot_scope_status === 'incomplete',
    marketValue,
    totalPnl,
    currentFloatPnl,
    dailyPnl,
    totalCost,
    pnlPct: totalCost > 0 ? totalPnl / totalCost * 100 : 0,
    asOf: latestDateKey(valuationState?.as_of, summaryFresh ? portfolioSummary?.as_of : null, fallbackAsOf) || fallbackAsOf,
    totalLiabilities,
    valuationState,
  };
}

export function categoryByLabel(categories, label) {
  return (categories || []).find(item => item.label === label) || {};
}

export function isoDateKey(value) {
  const key = value ? String(value).slice(0, 10) : '';
  return /^\d{4}-\d{2}-\d{2}$/.test(key) ? key : '';
}

export function isSameOrAfterDate(left, right) {
  const leftKey = isoDateKey(left);
  const rightKey = isoDateKey(right);
  if (!leftKey || !rightKey) return false;
  return leftKey >= rightKey;
}

export function latestDateKey(...values) {
  const keys = values.map(isoDateKey).filter(Boolean);
  return keys.length ? keys.sort().at(-1) : '';
}

export function isFreshPayloadAsOf(payload, targetAsOf) {
  if (!payload) return false;
  const targetKey = isoDateKey(targetAsOf);
  if (!targetKey) return true;
  return isSameOrAfterDate(payload.as_of || payload.asOf || payload.valuation_date, targetKey);
}

export function freshValuationStateForTarget(valuationState, targetAsOf) {
  if (!valuationState) return null;
  return isFreshPayloadAsOf(
    {
      as_of: valuationState.as_of || valuationState.valuation_date,
      valuation_date: valuationState.valuation_date,
    },
    targetAsOf
  ) ? valuationState : null;
}

export function makeMobileHeroProfiles({
  summary,
  categories,
  accountOverview,
  fxRates,
  aStockRowsSummary,
  aStockSummary,
  fundDisplaySummary,
  fundSummary,
  usSummary,
  usRows = [],
  recentUsReturn = null,
  ibkrSummary,
  preciousSummary,
  esopSummary,
}) {
  const portfolioValue = Math.max(safeNum(summary.marketValue), 1);
  const fundValue = safeNum(fundDisplaySummary.totalValue);
  const fundNlv = fundValue;
  const aStockValue = safeNum(aStockSummary.totalValue);
  const aStockMarket = safeNum(aStockRowsSummary.totalValue);
  const aStockCash = Math.max(aStockValue - aStockMarket, 0);
  const aStockDailyPnl = aStockSummary.dailyPnl != null && Number.isFinite(Number(aStockSummary.dailyPnl))
    ? safeNum(aStockSummary.dailyPnl)
    : safeNum(aStockRowsSummary.dailyPnl);
  const ibkrHasFreshAccount = Boolean(
    ibkrSummary?.flexCoversPositionDate ||
    ibkrSummary?.holdingsNetCoversPositionDate ||
    ibkrSummary?.accountValueSource === 'holdings_snapshot_net_liquidation'
  );
  const ibkrNlv = safeNum(ibkrHasFreshAccount ? ibkrSummary?.totalValue : usSummary.totalValue);
  const ibkrMarket = safeNum(ibkrHasFreshAccount ? ibkrSummary?.positionValue : usSummary.totalValue);
  const ibkrCash = ibkrHasFreshAccount && Number.isFinite(Number(ibkrSummary?.cashValue))
    ? safeNum(ibkrSummary.cashValue)
    : 0;
  const accountCash = accountCashBreakdown(accountOverview, fxRates);
  const brokerageCash = ibkrCash + aStockCash;
  const netCash = brokerageCash + accountCash.netCash;
  const cashAssets = Math.max(aStockCash, 0) + Math.max(ibkrCash, 0) + accountCash.cashAssets;
  const cashLiabilities = Math.max(-ibkrCash, 0) + accountCash.cashLiabilities;
  const usProfileCategory = categoryByLabel(categories, '美股/海外');
  // Cash balances and historical closed positions do not establish today's price-return quality.
  const usDailyStatuses = usRows.filter(row => !row.is_closed
    && ![row.major_category, row.minor_category, row.type].some(category => ['现金', '现金/活期'].includes(category)))
    .map(row => row.daily_pnl_stale === true ? 'pending' : String(row.daily_return_status || row.daily_pnl_status || '').toLowerCase());
  const usDailyStatus = usDailyStatuses.includes('pending')
    ? (usDailyStatuses.some(status => ['estimated', 'confirmed'].includes(status)) ? 'partial' : 'pending')
    : usDailyStatuses.includes('estimated') ? 'estimated'
      : usDailyStatuses.length && usDailyStatuses.every(status => status === 'confirmed') ? 'confirmed' : '';
  const showRecentUsReturn = usDailyStatus === 'pending' && recentUsReturn?.amount_cny != null
    && Number.isFinite(Number(recentUsReturn.amount_cny));
  const ibkrTotalPnl = ibkrHasFreshAccount && Number.isFinite(Number(ibkrSummary?.totalPnl))
    ? safeNum(ibkrSummary.totalPnl)
    : safeNum(usProfileCategory.totalPnl);
  const ibkrTotalRate = safeNum(
    usProfileCategory.accountReturnRate ??
    usProfileCategory.displayReturnRate ??
    usProfileCategory.totalReturnRate
  );
  const ibkrGrossHoldingRate = Number.isFinite(Number(usProfileCategory.grossHoldingReturnRate))
    ? safeNum(usProfileCategory.grossHoldingReturnRate)
    : safeNum(usProfileCategory.holdingReturnRate);
  const ibkrRateAsOf = isoDateKey(usProfileCategory.accountReturnRateAsOf);
  const ibkrRateEvidenceAsOf = isoDateKey(usProfileCategory.accountReturnRateEvidenceAsOf);
  const ibkrPositionAsOf = isoDateKey(ibkrSummary?.positionAsOf);
  const ibkrRateTitle = usProfileCategory.accountReturnRateUndefined === true
    ? `账户收益率暂无同日口径${ibkrPositionAsOf ? `（当前估值 ${ibkrPositionAsOf}）` : ''}；${ibkrRateEvidenceAsOf ? `可验证官方日结 ${ibkrRateEvidenceAsOf}，无当前可绑定收益率` : '缺少可验证日结证据'}`
    : (ibkrRateAsOf ? `账户收益率对应官方日结 ${ibkrRateAsOf}` : '');
  const totalMarketValue = aStockMarket + fundValue + ibkrMarket + safeNum(preciousSummary.totalValue) + safeNum(esopSummary.totalValue);
  const positionRatio = pctFromAmount(totalMarketValue, summary.marketValue);
  const currentFloatPnl = Number.isFinite(Number(summary.currentFloatPnl)) ? safeNum(summary.currentFloatPnl) : safeNum(summary.totalPnl);
  const totalLiabilities = Math.max(0, safeNum(summary.totalLiabilities));
  const allProfile = {
    key: '全部',
    hideHeader: true,
    scopeIncomplete: summary.scopeIncomplete,
    valueLabel: '投资组合价值',
    value: summary.marketValue,
    dailyLabel: '今日盈亏',
    dailyPnl: summary.dailyPnl,
    dailyStatus: usDailyStatus === 'pending' ? 'pending' : '',
    dailyRate: pctFromAmount(summary.dailyPnl, Math.max(summary.marketValue - summary.dailyPnl, 1)),
    totalLabel: '总盈亏',
    totalPnl: summary.totalPnl,
    totalRate: summary.twr,
    share: 1,
    miniMetrics: [
      { label: '总市值', value: formatOptionalMoney(totalMarketValue) },
      { label: '当前浮盈', value: formatOptionalMoney(currentFloatPnl, 'CNY', { signed: true }), tone: currentFloatPnl < 0 ? 'down' : 'up' },
      { label: '总负债', value: formatOptionalMoney(totalLiabilities), tone: totalLiabilities > 0 ? 'down' : '' },
      { label: '现金/活期', value: formatOptionalMoney(netCash, 'CNY', { signed: netCash < 0 }), tone: netCash < 0 ? 'down' : '' },
      { label: '仓位占比', value: `${Math.max(0, positionRatio * 100).toFixed(1)}%` },
    ],
  };
  const profiles = {
    '全部': allProfile,
    'A股个股': {
      key: 'A股个股',
      scopeIncomplete: summary.scopeIncomplete,
      icon: '🇨🇳',
      identity: 'CNY · 同花顺',
      valueLabel: '账户净值',
      value: aStockValue,
      dailyLabel: '今日盈亏',
      dailyPnl: aStockDailyPnl,
      dailyRate: pctFromAmount(aStockDailyPnl, Math.max(aStockMarket - aStockDailyPnl, 1)),
      totalLabel: '总盈亏',
      totalPnl: safeNum(aStockSummary.totalPnl),
      totalRate: safeNum(
        aStockSummary.accountReturnRate ??
        aStockSummary.displayReturnRate ??
        aStockSummary.holdingReturnRate ??
        categoryByLabel(categories, 'A股个股').totalReturnRate
      ),
      share: aStockValue / portfolioValue,
      miniMetrics: summary.scopeIncomplete ? [
        { label: '现金余额', value: '待核验' },
      ] : [
        { label: '总市值', value: formatOptionalMoney(aStockMarket) },
        { label: '可用现金', value: formatOptionalMoney(aStockCash), tone: aStockCash < 0 ? 'down' : '' },
        { label: '仓位占比', value: `${Math.max(0, pctFromAmount(aStockMarket, aStockValue) * 100).toFixed(1)}%` },
      ],
    },
    'A股基金': {
      key: 'A股基金',
      icon: '🇨🇳',
      identity: 'CNY · 建行 / 腾讯理财通',
      value: fundNlv,
      dailyLabel: '今日盈亏',
      dailyPnl: categoryByLabel(categories, '基金').dailyPnl,
      totalLabel: '总盈亏',
      totalPnl: safeNum(fundSummary.totalPnl),
      totalRate: safeNum(fundSummary.holdingReturnRate ?? categoryByLabel(categories, '基金').holdingReturnRate),
      share: fundNlv / portfolioValue,
    },
    '美股/海外': {
      key: '美股/海外',
      icon: '🇺🇸🇰🇷🇭🇰',
      identity: 'USD · IBKR',
      value: ibkrNlv,
      dailyLabel: showRecentUsReturn ? `${recentUsReturn.label}${recentUsReturn.partial ? '（部分）' : ''}` : '今日盈亏',
      dailyPnl: showRecentUsReturn ? Number(recentUsReturn.amount_cny) : categoryByLabel(categories, '美股/海外').dailyPnl,
      dailyStatus: showRecentUsReturn ? 'historical' : usDailyStatus,
      dailyStatusTitle: showRecentUsReturn ? recentUsReturn.title : '',
      totalLabel: '总盈亏',
      totalPnl: ibkrTotalPnl,
      totalRate: ibkrTotalRate,
      totalRateUndefined: usProfileCategory.accountReturnRateUndefined === true,
      totalRateTitle: ibkrRateTitle,
      share: ibkrNlv / portfolioValue,
      miniMetrics: [
        { label: '总市值', value: formatOptionalMoney(ibkrMarket) },
        { label: '可用现金', value: ibkrCash < 0 ? formatOptionalMoney(ibkrCash, 'CNY', { signed: true }) : formatOptionalMoney(ibkrCash), tone: ibkrCash < 0 ? 'down' : '' },
        { label: '价格收益率', value: signedPctText(ibkrGrossHoldingRate), tone: ibkrGrossHoldingRate < 0 ? 'down' : 'up' },
        { label: '仓位占比', value: `${Math.max(0, pctFromAmount(ibkrMarket, ibkrNlv) * 100).toFixed(1)}%` },
      ],
    },
    '贵金属': {
      key: '贵金属',
      icon: '🟡',
      identity: 'CNY · 建行',
      value: safeNum(preciousSummary.totalValue),
      dailyLabel: '今日盈亏',
      dailyPnl: categoryByLabel(categories, '贵金属').dailyPnl,
      totalLabel: '总盈亏',
      totalPnl: safeNum(preciousSummary.totalPnl),
      totalRate: safeNum(preciousSummary.holdingReturnRate ?? categoryByLabel(categories, '贵金属').holdingReturnRate),
      share: safeNum(preciousSummary.totalValue) / portfolioValue,
    },
    '内部股份/分红': {
      key: '内部股份/分红',
      icon: 'H',
      identity: '股权分红',
      value: safeNum(esopSummary.totalValue),
      dailyLabel: '当年盈亏',
      dailyPnl: safeNum(esopSummary.totalPnl),
      dailyRate: safeNum(esopSummary.holdingReturnRate ?? categoryByLabel(categories, '内部股份/分红').holdingReturnRate),
      totalLabel: '总盈亏',
      totalPnl: safeNum(esopSummary.totalPnl),
      totalRate: safeNum(esopSummary.holdingReturnRate ?? categoryByLabel(categories, '内部股份/分红').holdingReturnRate),
      share: safeNum(esopSummary.totalValue) / portfolioValue,
    },
    '现金/活期': {
      key: '现金/活期',
      icon: '¥',
      identity: '银行卡 / 券商 · 现金',
      valueLabel: '净现金',
      value: netCash,
      valueSigned: true,
      dailyLabel: '今日盈亏',
      dailyPnl: 0,
      dailyRate: 0,
      totalLabel: '总盈亏',
      totalPnl: 0,
      totalRate: 0,
      share: netCash / portfolioValue,
      miniMetrics: [
        { label: '现金资产', value: formatOptionalMoney(cashAssets) },
        { label: '现金负债', value: formatOptionalMoney(-cashLiabilities, 'CNY', { signed: cashLiabilities > 0 }), tone: cashLiabilities > 0 ? 'down' : '' },
        { label: '银行卡/支付', value: formatOptionalMoney(accountCash.netCash, 'CNY', { signed: accountCash.netCash < 0 }), tone: accountCash.netCash < 0 ? 'down' : '' },
        { label: '券商现金', value: formatOptionalMoney(brokerageCash, 'CNY', { signed: brokerageCash < 0 }), tone: brokerageCash < 0 ? 'down' : '' },
      ],
    },
  };
  return profiles;
}

export function contributionPct(value, totalPnl) {
  const denom = Math.abs(safeNum(totalPnl));
  return denom > 0.0001 ? safeNum(value) / denom : 0;
}

export function classifyDataQuality(row) {
  const major = String(row.major_category || '');
  const minor = String(row.minor_category || '');
  if (major === 'ESOP') return { label: '待确认', kind: 'pending' };
  if (major === '贵金属' || minor.includes('基金')) return { label: '中可信', kind: 'warn' };
  if (!row.current_price && !row.buy_price && !row.quantity) return { label: '需核准', kind: 'pending' };
  return { label: '高可信', kind: 'ok' };
}

export function normalizeHolding(row, fallbackCategory = '') {
  const marketValue = safeNum(cnyValue(row, 'market_value'));
  const pnl = safeNum(cnyValue(row, 'holding_pnl'));
  const cost = safeNum(cnyValue(row, 'cost')) || Math.max(marketValue - pnl, 0);
  const quality = classifyDataQuality(row);
  const dailyPnl = safeNum(cnyValue(row, 'daily_pnl'));
  const fxDailyPnl = safeNum(cnyValue(row, 'daily_fx_pnl'));
  const totalDailyPnl = Number.isFinite(Number(cnyValue(row, 'daily_total_pnl')))
    ? safeNum(cnyValue(row, 'daily_total_pnl'))
    : dailyPnl + fxDailyPnl;
  return {
    id: `${row.account || ''}-${row.symbol || row.name || ''}`,
    name: row.name || row.symbol || '未命名资产',
    symbol: row.symbol || '',
    assetClass: fallbackCategory || row.major_category || row.minor_category || '其他',
    account: accountLabel(row.account || row.account_code || ''),
    currency: row.currency || 'CNY',
    marketValue,
    pnl,
    dailyPnl,
    fxDailyPnl,
    totalDailyPnl,
    nativeDailyPnl: safeNum(row.daily_pnl_native ?? row.daily_pnl),
    returnPct: cost > 0 ? pnl / cost : 0,
    weight: 0,
    quality,
    isClosed: Boolean(row.is_closed),
  };
}

export function formatNativeDaily(value, currency) {
  const cur = String(currency || 'CNY').toUpperCase();
  return formatCurrency(value, cur, {
    signed: true,
    minimumFractionDigits: cur === 'KRW' ? 0 : 2,
    maximumFractionDigits: cur === 'KRW' ? 0 : 2,
  });
}

export function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[ch]));
}

export function confirmedIbkrAccountSummary(perf, positionAsOf = null) {
  if (perf?.status !== 'ok' || perf.daily_close?.status !== 'ok' || perf.daily_close.fx_status !== 'available') return null;
  if (!isoDateKey(perf.as_of) || (positionAsOf && !isSameOrAfterDate(perf.as_of, positionAsOf))) return null;
  const account = perf.portfolio_account;
  if (account?.status !== 'ok' || account.account_code !== 'ibkr' || account.ownership_basis !== 'owner_adjusted' || account.currency !== 'CNY') return null;
  if (typeof perf.daily_close.source_account_id !== 'string' || !perf.daily_close.source_account_id || !/^[0-9a-f]{64}$/.test(perf.daily_close.source_sha256 || '') || account.source_account_id !== perf.daily_close.source_account_id || account.as_of !== perf.as_of || account.source_sha256 !== perf.daily_close.source_sha256) return null;
  const ratio = account.owner_ratio;
  if (!(typeof ratio === 'number' || (typeof ratio === 'string' && ratio.trim() !== '')) || !Number.isFinite(Number(ratio)) || Number(ratio) < 0) return null;
  const fields = ['net_liquidation_cny', 'total_pnl_cny', 'return_capital_cny', 'cash_cny', 'position_value_cny'];
  if (!fields.every(key => typeof account[key] === 'number' && Number.isFinite(account[key]))) return null;
  const rate = account.return_ratio == null ? null : account.return_ratio;
  if (rate != null && (typeof rate !== 'number' || !Number.isFinite(rate))) return null;
  return {
    totalValue: account.net_liquidation_cny,
    totalPnl: account.total_pnl_cny,
    totalCost: account.return_capital_cny,
    cashValue: account.cash_cny,
    positionValue: account.position_value_cny,
    totalReturnRate: rate,
    asOf: perf.as_of,
  };
}

export function displayPnlPct(r) {
  const cost = safeNum(cnyValue(r, 'cost'));
  if (cost > 0) return safeNum(cnyValue(r, 'holding_pnl')) / cost * 100;
  return r.is_closed ? 0 : pnlPct(r);
}

export function summarizeRows(rows) {
  let totalValue = 0;
  let totalPnl = 0;
  let dailyPnl = 0;
  let fxDailyPnl = 0;
  let totalCost = 0;
  let activeHoldingPnl = 0;
  let activeTotalCost = 0;
  for (const r of rows) {
    totalValue += safeNum(cnyValue(r, 'market_value'));
    totalPnl += safeNum(cnyValue(r, 'holding_pnl'));
    dailyPnl += safeNum(cnyValue(r, 'daily_pnl'));
    fxDailyPnl += safeNum(cnyValue(r, 'daily_fx_pnl'));
    const rowCost = safeNum(cnyValue(r, 'cost'));
    totalCost += rowCost;
    if (!r.is_closed) {
      activeHoldingPnl += safeNum(cnyValue(r, 'holding_pnl'));
      activeTotalCost += rowCost;
    }
  }
  return {
    totalValue,
    totalPnl,
    dailyPnl,
    fxDailyPnl,
    dailyTotalPnl: dailyPnl + fxDailyPnl,
    totalCost,
    activeHoldingPnl,
    activeTotalCost,
    holdingReturnRate: activeTotalCost > 0 ? activeHoldingPnl / activeTotalCost : 0,
  };
}

export function returnRateFromCapitalBase(summary, capitalBaseSummary = summary) {
  const totalPnl = safeNum(summary?.totalPnl);
  const capitalCost = safeNum(capitalBaseSummary?.totalCost);
  const impliedCapital = safeNum(capitalBaseSummary?.totalValue) - safeNum(capitalBaseSummary?.totalPnl);
  const denominator = capitalCost > 0 ? capitalCost : Math.max(impliedCapital, 0);
  return denominator > 0 ? totalPnl / denominator : 0;
}

export function holdingReturnRateFromSummary(summary) {
  if (Number.isFinite(Number(summary?.holdingReturnRate))) return safeNum(summary.holdingReturnRate);
  const activeCost = safeNum(summary?.activeTotalCost);
  if (activeCost > 0) return safeNum(summary?.activeHoldingPnl) / activeCost;
  return returnRateFromCapitalBase(summary);
}

export function reportedEntryKey(entry) {
  return entry?.symbol ? `symbol:${entry.symbol}` : `name:${entry?.name || ''}`;
}

export function findReportedEntry(row, section) {
  const entries = section?.entries || [];
  const rowSymbol = String(row.symbol || '');
  const rowName = String(row.name || '');
  return entries.find((entry) => (
    (entry.symbol && String(entry.symbol) === rowSymbol) ||
    (!entry.symbol && String(entry.name || '') === rowName)
  ));
}

export function withReportedPnlRows(rows, section) {
  if (!section?.entries?.length) return rows;
  return rows.map((row) => {
    const entry = findReportedEntry(row, section);
    if (!entry) return row;
    const pnl = safeNum(entry.ytd_pnl);
    return {
      ...row,
      holding_pnl: pnl,
      holding_pnl_native: pnl,
      holding_pnl_cny: pnl,
      reported_ytd_pnl_cny: pnl,
      cost: entry.bank_cash_cost ?? row.cost,
      cost_cny: entry.bank_cash_cost ?? cnyValue(row, 'cost'),
    };
  });
}

export function withEsopDisplayRows(rows) {
  return (rows || []).map((row) => ({
    ...row,
    name: '内部股份分红',
  }));
}

export function applyReportedReturnTotal(summary, rows, section) {
  if (!section || section.total_ytd_pnl == null) return summary;
  const represented = new Set();
  for (const row of rows) {
    const entry = findReportedEntry(row, section);
    if (entry) represented.add(reportedEntryKey(entry));
  }
  const representedPnl = (section.entries || [])
    .filter((entry) => represented.has(reportedEntryKey(entry)))
    .reduce((sum, entry) => sum + safeNum(entry.ytd_pnl), 0);
  const extraPnl = safeNum(section.total_ytd_pnl) - representedPnl;
  if (Math.abs(extraPnl) < 0.005) return summary;
  return { ...summary, totalPnl: summary.totalPnl + extraPnl };
}

export function fmtSignedPctFromRate(rate) {
  const pct = safeNum(rate) * 100;
  return (pct >= 0 ? '+' : '') + pct.toFixed(2) + '%';
}

export function latestSectionReturnAmount(row) {
  if (!row) return 0;
  if (row.total_pnl != null) return safeNum(row.total_pnl);
  return safeNum(row.market_value) - safeNum(row.cumulative_capital);
}

export function sectionTodayReturnAmount(resp) {
  if (resp?.today_window?.total_return != null) {
    return safeNum(resp.today_window.total_return);
  }
  const rows = Array.isArray(resp?.rows) ? resp.rows : [];
  if (!rows.length) return null;
  const latest = rows[rows.length - 1];
  const previous = rows.length > 1 ? rows[rows.length - 2] : null;
  return latestSectionReturnAmount(latest) - latestSectionReturnAmount(previous);
}

export function annualizedRateFromPeriod(rate, startDate, endDate) {
  const start = new Date(`${startDate || '2026-01-01'}T00:00:00`);
  const end = new Date(`${endDate || '2026-05-31'}T00:00:00`);
  const days = Math.max((end - start) / 86400000, 1);
  if (rate <= -1) return 0;
  return Math.pow(1 + rate, 365 / days) - 1;
}

export function categoryReturnOverride(sectionPayload) {
  if (!sectionPayload?.summary) return null;
  return {
    totalPnl: safeNum(sectionPayload.summary.total_return),
    totalReturnRate: safeNum(sectionPayload.summary.total_return_rate),
    annualizedReturnRate: safeNum(sectionPayload.summary.annualized_return_rate),
    dailyPnl: sectionTodayReturnAmount(sectionPayload),
    method: sectionPayload.method,
  };
}

export function categoryMetric(label, base, sectionPayload) {
  const override = categoryReturnOverride(sectionPayload);
  const totalPnl = override ? override.totalPnl : safeNum(base.totalPnl);
  const totalValue = safeNum(base.totalValue);
  const totalCost = safeNum(base.totalCost) || Math.max(totalValue - totalPnl, 0);
  const explicitTotalReturnRate = Number.isFinite(Number(base.totalReturnRate))
    ? safeNum(base.totalReturnRate)
    : null;
  const holdingReturnRate = holdingReturnRateFromSummary(base);
  const displayReturnRate = base.accountReturnRateUndefined === true
    ? null
    : Number.isFinite(Number(base.displayReturnRate))
    ? safeNum(base.displayReturnRate)
    : holdingReturnRate;
  // IBKR return-series reconstructs long-term return from trades and position
  // history. Around same-day sells it can move cash between position/cash
  // buckets, so US "today" must stay on the holdings/account daily PnL basis.
  const priceDailyPnl = label === '美股/海外'
    ? safeNum(base.dailyPnl)
    : (Number.isFinite(Number(override?.dailyPnl)) ? Number(override.dailyPnl) : safeNum(base.dailyPnl));
  const fxDailyPnl = safeNum(base.fxDailyPnl);
  const dailyPnl = priceDailyPnl + fxDailyPnl;
  return {
    label,
    totalValue,
    totalPnl,
    dailyPnl,
    priceDailyPnl,
    fxDailyPnl,
    dailyTotalPnl: dailyPnl,
    totalCost,
    totalReturnRate: Number.isFinite(Number(displayReturnRate))
      ? displayReturnRate
      : (explicitTotalReturnRate ?? (totalCost > 0 ? totalPnl / totalCost : 0)),
    holdingReturnRate,
    displayReturnRate,
    accountReturnRate: base.accountReturnRate != null && Number.isFinite(Number(base.accountReturnRate)) ? safeNum(base.accountReturnRate) : null,
    accountReturnRateUndefined: base.accountReturnRateUndefined === true,
    accountReturnRateAsOf: base.accountReturnRateAsOf || null,
    accountReturnRateEvidenceAsOf: base.accountReturnRateEvidenceAsOf || null,
    grossHoldingReturnRate: Number.isFinite(Number(base.grossHoldingReturnRate)) ? safeNum(base.grossHoldingReturnRate) : holdingReturnRate,
    annualizedReturnRate: override ? override.annualizedReturnRate : null,
  };
}

export function buildContributionRows(groups, summary) {
  const all = [];
  for (const group of groups) {
    for (const row of group.rows || []) {
      const item = normalizeHolding(row, group.label);
      item.weight = summary.marketValue > 0 ? item.marketValue / summary.marketValue : 0;
      all.push(item);
    }
  }
  const winners = all
    .filter(item => !item.isClosed && item.pnl > 0.005)
    .sort((a, b) => b.pnl - a.pnl);
  const losers = all
    .filter(item => !item.isClosed && item.pnl < -0.005)
    .sort((a, b) => a.pnl - b.pnl);
  const daily = all
    .filter(item => Math.abs(item.dailyPnl) > 0.005 || Math.abs(item.fxDailyPnl) > 0.005)
    .sort((a, b) => Math.abs(b.totalDailyPnl) - Math.abs(a.totalDailyPnl));
  return { winners, losers, daily, totalPnl: summary.totalPnl, dailyPnl: summary.dailyPnl };
}

export function buildInvestmentConclusion(summary, categories, issues) {
  const gainers = categories.filter(c => c.totalPnl > 0).sort((a, b) => b.totalPnl - a.totalPnl).slice(0, 3);
  const drags = categories.filter(c => c.totalPnl < 0).sort((a, b) => a.totalPnl - b.totalPnl).slice(0, 3);
  const chip = (label, kind = '') => `<span class="conclusionChip ${kind}">${esc(label)}</span>`;
  const gainChips = gainers.length
    ? gainers.map(c => chip(c.label.replace('/分红', ''), 'good')).join('')
    : chip('无', 'neutral');
  const dragChips = drags.length
    ? drags.map(c => chip(c.label, 'bad')).join('')
    : chip('无', 'neutral');
  return `<div class="conclusionChipRow">
    <span class="conclusionLabel">驱动</span>${gainChips}
    <span class="conclusionLabel">拖累</span>${dragChips}
  </div>`;
}

export function deriveDataQualityIssues({ reportedReturns, staleReportedReturns = false, ibkrPerf, esopRows, fundRows, preciousRows }) {
  const issues = [];
  if (staleReportedReturns) {
    issues.push('基金、贵金属平台收益日期早于当前持仓，已跳过旧口径覆盖');
  }
  if (reportedReturns?.sections?.a_share_fund || reportedReturns?.sections?.precious_metals) {
    issues.push('基金、贵金属包含平台收益中心/截图口径');
  }
  if (reportedReturns?.account_performance?.tencent_licaitong) {
    issues.push('腾讯理财通收益使用平台确认口径');
  }
  if ((esopRows || []).length) {
    issues.push('内部股份属于低流动性资产，估值和分红可持续性待确认');
  }
  if (ibkrPerf && ibkrPerf.status === 'ok' && ibkrPerf.other_pnl_base !== undefined) {
    issues.push('美股/海外今日收益已拆分价格与汇率贡献；账户层其他盈亏仍需核准');
  } else {
    issues.push('IBKR 账户表现字段可能缺失');
  }
  if ((fundRows || []).some(row => !row.current_price && !row.quantity)) {
    issues.push('部分基金缺少份额/净值字段，收益率依赖平台口径');
  }
  if ((preciousRows || []).some(row => !row.current_price || !row.quantity)) {
    issues.push('贵金属成本和克数口径需要继续核准');
  }
  return [...new Set(issues)];
}
