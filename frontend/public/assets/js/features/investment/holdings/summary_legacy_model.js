// Compatibility-only investment model; legacy field choices and formula bodies
// remain unchanged. Do not reuse these fallbacks as a new financial engine.
import { safeNum, pnlPct } from '../../../utils.js';
import { accountCashBreakdown } from '../../../account_cash.js';
import { accountLabel } from '../../../api.js';
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
  if (text === 'A股' || text.includes('A股个股')) return 'A股个股';
  if (text.includes('A股基金') || text === '基金') return 'A股基金';
  if (['美股', '港股', '韩股', '瑞典', '日股', '英股', '海外'].some(market => text.includes(market))) return '美股/海外';
  if (text.includes('贵金属') || text.includes('黄金')) return '贵金属';
  if (text === '员工持股' || text.includes('内部') || text.includes('ESOP')) return '内部股份/分红';
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
    knownInvestmentValue: (portfolioValuationState || portfolioSummary?.valuation_state)?.known_investment_value_cny ?? null,
    knownInvestmentAsOf: (portfolioValuationState || portfolioSummary?.valuation_state)?.as_of ?? null,
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
  accountCards = {},
  accountOverview,
  fxRates,
  aStockRowsSummary,
  aStockSummary,
  ibkrSummary,
  preciousSummary,
  esopSummary,
}) {
  const portfolioValue = safeNum(summary.marketValue);
  const aStockValue = safeNum(aStockSummary.totalValue);
  const aStockMarket = safeNum(aStockRowsSummary.totalValue);
  const aStockCash = Math.max(aStockValue - aStockMarket, 0);
  const ibkrHasFreshAccount = Boolean(
    ibkrSummary?.holdingsNetCoversPositionDate
  );
  const ibkrCash = ibkrHasFreshAccount && Number.isFinite(Number(ibkrSummary?.cashValue))
    ? safeNum(ibkrSummary.cashValue)
    : 0;
  const accountCash = accountCashBreakdown(accountOverview, fxRates);
  const brokerageCash = ibkrCash + aStockCash;
  const netCash = brokerageCash + accountCash.netCash;
  const cashAssets = Math.max(aStockCash, 0) + Math.max(ibkrCash, 0) + accountCash.cashAssets;
  const cashLiabilities = Math.max(-ibkrCash, 0) + accountCash.cashLiabilities;
  const allProfile = {
    key: '全部',
    hideHeader: true,
    valueLabel: '持仓净值',
    cashLabel: '投资账户现金',
    accountCard: accountCards.investment_overview ?? {},
  };
  const profiles = {
    '全部': allProfile,
    'A股个股': {
      key: 'A股个股',
      icon: '🇨🇳',
      identity: '同花顺',
      accountSubtitle: 'A 股证券 · 人民币 · 个人份额',
      accountCard: accountCards.tonghuashun ?? {},
    },
    'A股基金': {
      key: 'A股基金',
      icon: '🇨🇳',
      identity: '基金总览',
      accountSubtitle: '基金持仓与活期+ · 人民币',
      valueLabel: '基金净值',
      accountCard: accountCards.fund_overview ?? {},
    },
    '美股/海外': {
      key: '美股/海外',
      icon: '🇺🇸🇰🇷🇭🇰',
      identity: 'IBKR',
      accountSubtitle: '海外证券 · 美元账户',
      accountCard: accountCards.ibkr ?? {},
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
      dailyStatus: esopSummary.annualPerformance?.annual_pnl_cny === null ? 'pending' : undefined,
      dailyPnl: esopSummary.annualPerformance
        ? esopSummary.annualPerformance.annual_pnl_cny
        : safeNum(esopSummary.totalPnl),
      dailyRate: esopSummary.annualPerformance
        ? esopSummary.annualPerformance.annual_return_rate
        : safeNum(esopSummary.holdingReturnRate ?? categoryByLabel(categories, '内部股份/分红').holdingReturnRate),
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

export function deriveDataQualityIssues({ reportedReturns, staleReportedReturns = false, ibkrOfficialValue, esopRows, fundRows, preciousRows }) {
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
  if (ibkrOfficialValue?.display_period_pnl_base == null) {
    issues.push('IBKR 期间收盈缺少已核验日结');
  }
  if ((fundRows || []).some(row => !row.current_price && !row.quantity)) {
    issues.push('部分基金缺少份额/净值字段，收益率依赖平台口径');
  }
  if ((preciousRows || []).some(row => !row.current_price || !row.quantity)) {
    issues.push('贵金属成本和克数口径需要继续核准');
  }
  return [...new Set(issues)];
}
