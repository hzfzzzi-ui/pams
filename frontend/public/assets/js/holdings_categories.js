import { jget, fmt2, accountLabel } from './api.js?v=20260619-investment-hardening-v1';
import { openBottomSheet, setActiveNav } from './ui.js?v=20260620-data-state-v1';
import { safeNum, pnlPct, createReturnRateChart } from './utils.js?v=20260606-astock-fact-return-v1';
import { accountCashBreakdown } from './account_cash.js?v=20260606-cash-active-v1';

function jgetFresh(path) {
  return jget(path, { cache: false });
}

function cnyValue(r, key) {
  const cnyKey = `${key}_cny`;
  return r[cnyKey] ?? r[key] ?? 0;
}

function fxRateForCurrency(currency, fxRates = {}) {
  const ccy = String(currency || 'CNY').trim().toUpperCase();
  if (!ccy || ccy === 'CNY') return 1;
  const rate = Number(fxRates[ccy]);
  return Number.isFinite(rate) && rate > 0 ? rate : 1;
}

function accountPrimaryBalanceCny(accountOverview, accountCode, fxRates = {}) {
  const account = (accountOverview?.accounts || []).find(item => String(item.account_code || '') === accountCode);
  const balance = account?.primary_balance || account?.platform_balance || null;
  if (!balance || balance.amount == null || balance.amount === '') return null;
  const amount = Number(balance.amount);
  if (!Number.isFinite(amount)) return null;
  return {
    amountCny: amount * fxRateForCurrency(balance.currency || account.base_currency || 'CNY', fxRates),
    currency: String(balance.currency || account.base_currency || 'CNY').toUpperCase(),
    asOf: balance.as_of || account.as_of || null,
    source: balance.source || account.truth_source || 'account_overview',
  };
}

function accountNetLiquidationFromHoldings(holdingsPayload, accountCode) {
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

function fmtUsd(x) {
  return formatCurrency(x, 'USD', { maximumFractionDigits: 2 });
}

function fmtCny(x) {
  return formatCurrency(x, 'CNY', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function fmtSignedUsd(x) {
  return formatCurrency(x, 'USD', { signed: true, maximumFractionDigits: 2 });
}

function fmtSignedCny(x) {
  return formatCurrency(x, 'CNY', { signed: true, minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function fmtPctValue(x) {
  return (safeNum(x) * 100).toFixed(2) + '%';
}

const CATEGORY_SECTION_IDS = [
  'section-a-stock-equity',
  'section-a-fund',
  'section-us-stock',
  'section-precious-metal',
  'section-esop',
];

const RETURN_METHODS = [
  {
    key: 'twr',
    label: 'TWR',
    text: '剔除资金流。',
  },
  {
    key: 'cash-flow',
    label: '资金流调整',
    text: '分类线参考。',
  },
  {
    key: 'reported',
    label: '截图收益',
    text: '需核准。',
  },
  {
    key: 'fx',
    label: '多币种',
    text: '含汇率。',
  },
];

let mobileHeroContext = null;

function formatNumber(value, options = {}) {
  const n = safeNum(value);
  return n.toLocaleString('zh-CN', {
    minimumFractionDigits: options.minimumFractionDigits ?? 0,
    maximumFractionDigits: options.maximumFractionDigits ?? 0,
  });
}

function formatCurrency(value, currency = 'CNY', options = {}) {
  const n = safeNum(value);
  const symbol = currency === 'USD' ? '$' : currency === 'CNY' ? '¥' : `${currency} `;
  const sign = options.signed ? (n >= 0 ? '+' : '-') : '';
  return `${sign}${symbol}${formatNumber(Math.abs(n), {
    minimumFractionDigits: options.minimumFractionDigits ?? options.maximumFractionDigits ?? (currency === 'USD' ? 2 : 2),
    maximumFractionDigits: options.maximumFractionDigits ?? options.minimumFractionDigits ?? (currency === 'USD' ? 2 : 2),
  })}`;
}

function formatCompactCurrency(value, currency = 'CNY', options = {}) {
  return formatCurrency(value, currency, { ...options, minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function formatTableNumber(value, digits = 2) {
  if (value == null || value === '') return '待核准';
  return formatNumber(value, { minimumFractionDigits: 0, maximumFractionDigits: digits });
}

function formatPercentRate(rate) {
  const pct = safeNum(rate) * 100;
  return `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%`;
}

function profitColor(value) {
  return safeNum(value) >= 0 ? 'var(--good)' : 'var(--bad)';
}

function statusBadge(label, kind = 'data') {
  return `<span class="statusBadge statusBadge-${kind}">${esc(label)}</span>`;
}

function ReturnMethodBadge(method) {
  return `
    <div class="returnMethodBadge" title="${esc(method.text)}">
      <span>${esc(method.label)}</span>
    </div>
  `;
}

function metricCard(label, value, help, kind = '', extra = '') {
  const extraClass = extra ? ' hasBreakdown' : '';
  return `
    <div class="investmentHeroMetric ${kind}${extraClass}">
      <div class="label">${esc(label)}</div>
      <div class="value">${value}</div>
      <div class="help">${esc(help)}</div>
      ${extra}
    </div>
  `;
}

function InvestmentSummaryHeader(summary) {
  return `
    ${metricCard('市值', formatCompactCurrency(summary.marketValue), '含持仓与现金/活期', 'info')}
    ${metricCard('累计收益', formatCompactCurrency(summary.totalPnl, 'CNY', { signed: true }), '综合口径', summary.totalPnl >= 0 ? 'good' : 'bad')}
    ${metricCard('TWR', formatPercentRate(summary.twr), '时间加权', summary.twr >= 0 ? 'good' : 'bad')}
    ${metricCard('今日', formatCompactCurrency(summary.dailyPnl, 'CNY', { signed: true }), '日度变动', summary.dailyPnl >= 0 ? 'good' : 'bad')}
  `;
}

function nullableMoney(value, fallback = '待拆分') {
  return Number.isFinite(Number(value)) ? formatCompactCurrency(Number(value), 'CNY', { signed: true }) : fallback;
}

function toneFromNumber(value, fallback = 'pending') {
  if (!Number.isFinite(Number(value))) return fallback;
  return Number(value) >= 0 ? 'good' : 'bad';
}

function isInternalInvestmentLabel(label) {
  const text = String(label || '');
  return /ESOP/i.test(text) || text.includes('\u5185\u90e8\u80a1\u4efd');
}

function InvestmentSummaryHeaderCompact(summary) {
  const hasReturnRate = Number.isFinite(Number(summary.twr));
  const returnRate = hasReturnRate ? Number(summary.twr) : null;
  const dailyPnl = Number.isFinite(Number(summary.dailyPnl)) ? Number(summary.dailyPnl) : null;
  const currentFloatPnl = Number.isFinite(Number(summary.currentFloatPnl)) ? Number(summary.currentFloatPnl) : null;
  return `
    ${metricCard('投资组合价值', formatCompactCurrency(summary.marketValue), '含持仓与现金/活期', 'info')}
    ${metricCard('收益率', hasReturnRate ? formatPercentRate(returnRate) : '待核准', '时间加权收益率', toneFromNumber(returnRate))}
    ${metricCard('今日收益', dailyPnl == null ? '待核准' : formatCompactCurrency(dailyPnl, 'CNY', { signed: true }), '今日投资收益变化', toneFromNumber(dailyPnl), DailyReturnBreakdown(summary.dailyBreakdown))}
    ${metricCard('总收益', formatCompactCurrency(summary.totalPnl, 'CNY', { signed: true }), '累计投资收益', summary.totalPnl >= 0 ? 'good' : 'bad')}
    ${metricCard('当前浮盈', currentFloatPnl == null ? '待核准' : formatCompactCurrency(currentFloatPnl, 'CNY', { signed: true }), '仍持仓标的浮动盈亏', toneFromNumber(currentFloatPnl))}
  `;
}

function DailyReturnBreakdown(items = []) {
  const rows = (items || []).filter(item => !isInternalInvestmentLabel(item.label)).map(item => {
    const price = Number.isFinite(Number(item.priceDailyPnl)) ? safeNum(item.priceDailyPnl) : safeNum(item.dailyPnl);
    const fx = safeNum(item.fxDailyPnl);
    const value = Number.isFinite(Number(item.dailyTotalPnl)) ? safeNum(item.dailyTotalPnl) : price + fx;
    return `
      <span class="dailyReturnChip ${value >= 0 ? 'good' : 'bad'}" title="${esc(item.label)} 今日收益">
        <span>${esc(item.label)}</span>
        <strong>${formatCompactCurrency(value, 'CNY', { signed: true })}</strong>
        ${Math.abs(fx) > 0.005 ? `<em>价格 ${formatCompactCurrency(price, 'CNY', { signed: true })} · 汇率 ${formatCompactCurrency(fx, 'CNY', { signed: true })}</em>` : ''}
      </span>
    `;
  }).join('');
  return rows ? `<div class="dailyReturnBreakdown">${rows}</div>` : '';
}

function pctFromAmount(amount, base) {
  const amountNum = safeNum(amount);
  const baseNum = safeNum(base);
  if (!baseNum) return 0;
  return amountNum / baseNum;
}

function signedPctText(rate) {
  const pct = safeNum(rate) * 100;
  return `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%`;
}

function parseSyncDate(value) {
  if (!value) return null;
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;
  const raw = String(value);
  if (/^\d{4}-\d{2}-\d{2}$/.test(raw)) return new Date(`${raw}T00:00:00+08:00`);
  const normalized = /T/.test(raw) && !/(Z|[+-]\d{2}:?\d{2})$/.test(raw) ? `${raw}Z` : raw;
  const parsed = new Date(normalized);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function latestSyncTimestamp(...payloads) {
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

function formatSyncTimestamp(value, fallbackDate) {
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

function normalizeMobileHeroGroup(group) {
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

function currentMobileHeroGroup() {
  return '全部';
}

function shareTone(share) {
  const value = safeNum(share);
  if (value < 0) return 'risk';
  const absValue = Math.abs(value);
  if (absValue >= 0.35) return 'risk';
  if (absValue >= 0.20) return 'warn';
  return 'ok';
}

function formatOptionalMoney(value, currency = 'CNY', options = {}) {
  return Number.isFinite(Number(value))
    ? formatCompactCurrency(Number(value), currency, options)
    : '--';
}

function resolvePortfolioLiabilitiesCny(valuationState, portfolioSummary, fallback = 0) {
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


function canonicalPortfolioDashboardSummary({
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

function mobileHeroMetaItem(label, value, tone = '') {
  if (value == null || value === '') return '';
  return `
    <div class="mobileHeroMiniMetric ${tone}">
      <span>${esc(label)}</span>
      <strong>${value}</strong>
    </div>
  `;
}

function MobilePortfolioHero(profile, asOf, dataIssues = [], syncAt = null) {
  const value = safeNum(profile.value);
  const dailyPnl = Number.isFinite(Number(profile.dailyPnl)) ? safeNum(profile.dailyPnl) : 0;
  const dailyBase = Math.max(value - dailyPnl, 1);
  const dailyRate = Number.isFinite(Number(profile.dailyRate)) ? safeNum(profile.dailyRate) : pctFromAmount(dailyPnl, dailyBase);
  const totalPnl = Number.isFinite(Number(profile.totalPnl)) ? safeNum(profile.totalPnl) : 0;
  const totalRate = Number.isFinite(Number(profile.totalRate)) ? safeNum(profile.totalRate) : 0;
  const valueDisplay = formatOptionalMoney(value, 'CNY', { signed: Boolean(profile.valueSigned) });
  const freshness = formatSyncTimestamp(syncAt, asOf);
  const rawShare = safeNum(profile.share);
  const shareWidth = Math.abs(rawShare);
  const tone = shareTone(rawShare);
  const miniMetrics = (profile.miniMetrics || []).map(item => mobileHeroMetaItem(item.label, item.value, item.tone)).join('');
  return `
    ${profile.hideHeader ? '' : `<div class="mobileAccountHeroTop">
      <div class="mobileAccountIdentity">
        <span class="mobileAccountIcon">${profile.icon || '◌'}</span>
        <span>${esc(profile.identity || 'CNY PAMS')}</span>
      </div>
      <div class="mobileAccountWeight ${tone}">
        <span>仓位 ${signedPctText(rawShare).replace('+', '')}</span>
        <i><b style="width:${Math.max(2, Math.min(100, shareWidth * 100))}%"></b></i>
      </div>
    </div>`}
    <div class="mobileHeroMeta mobileHeroMetaTop">
      <span>${`更新 ${esc(freshness)}`}</span>
    </div>
    <div class="mobileHeroPrimaryGrid">
      <div class="mobileHeroPrimaryMetric main">
        <span>${esc(profile.valueLabel || '净清算价值')}</span>
        <strong class="mobileHeroValue ${value < 0 ? 'down' : ''}">${valueDisplay}</strong>
      </div>
      <div class="mobileHeroPrimaryMetric">
        <span>${esc(profile.dailyLabel || '今日盈亏')}</span>
        <strong class="${dailyPnl >= 0 ? 'up' : 'down'}">${formatOptionalMoney(dailyPnl, 'CNY', { signed: true })}</strong>
        <em class="${dailyPnl >= 0 ? 'up' : 'down'}">${signedPctText(dailyRate)}</em>
      </div>
      <div class="mobileHeroPrimaryMetric">
        <span>${esc(profile.totalLabel || '总盈亏')}</span>
        <strong class="${totalPnl >= 0 ? 'up' : 'down'}">${formatOptionalMoney(totalPnl, 'CNY', { signed: true })}</strong>
        <em class="${totalPnl >= 0 ? 'up' : 'down'}">${signedPctText(totalRate)}</em>
      </div>
    </div>
    <div class="mobileHeroMiniGrid ${miniMetrics ? '' : 'mobileHeroMiniGrid-empty'}">${miniMetrics}</div>
  `;
}

function categoryByLabel(categories, label) {
  return (categories || []).find(item => item.label === label) || {};
}

function isoDateKey(value) {
  const key = value ? String(value).slice(0, 10) : '';
  return /^\d{4}-\d{2}-\d{2}$/.test(key) ? key : '';
}

function isSameOrAfterDate(left, right) {
  const leftKey = isoDateKey(left);
  const rightKey = isoDateKey(right);
  if (!leftKey || !rightKey) return false;
  return leftKey >= rightKey;
}

function latestDateKey(...values) {
  const keys = values.map(isoDateKey).filter(Boolean);
  return keys.length ? keys.sort().at(-1) : '';
}

function isFreshPayloadAsOf(payload, targetAsOf) {
  if (!payload) return false;
  const targetKey = isoDateKey(targetAsOf);
  if (!targetKey) return true;
  return isSameOrAfterDate(payload.as_of || payload.asOf || payload.valuation_date, targetKey);
}

function freshValuationStateForTarget(valuationState, targetAsOf) {
  if (!valuationState) return null;
  return isFreshPayloadAsOf(
    {
      as_of: valuationState.as_of || valuationState.valuation_date,
      valuation_date: valuationState.valuation_date,
    },
    targetAsOf
  ) ? valuationState : null;
}

function makeMobileHeroProfiles({
  summary,
  categories,
  accountOverview,
  fxRates,
  aStockRowsSummary,
  aStockSummary,
  fundDisplaySummary,
  fundSummary,
  usSummary,
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
  const aStockDailyPnl = Number.isFinite(Number(aStockSummary.dailyPnl))
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
  const ibkrTotalPnl = ibkrHasFreshAccount && Number.isFinite(Number(ibkrSummary?.totalPnl))
    ? safeNum(ibkrSummary.totalPnl)
    : safeNum(usProfileCategory.totalPnl);
  const ibkrTotalRate = ibkrHasFreshAccount && Number.isFinite(Number(ibkrSummary?.totalReturnRate))
    ? safeNum(ibkrSummary.totalReturnRate)
    : safeNum(usProfileCategory.totalReturnRate);
  const totalMarketValue = aStockMarket + fundValue + ibkrMarket + safeNum(preciousSummary.totalValue) + safeNum(esopSummary.totalValue);
  const positionRatio = pctFromAmount(totalMarketValue, summary.marketValue);
  const currentFloatPnl = Number.isFinite(Number(summary.currentFloatPnl)) ? safeNum(summary.currentFloatPnl) : safeNum(summary.totalPnl);
  const totalLiabilities = Math.max(0, safeNum(summary.totalLiabilities));
  const allProfile = {
    key: '全部',
    hideHeader: true,
    valueLabel: '投资组合价值',
    value: summary.marketValue,
    dailyLabel: '今日盈亏',
    dailyPnl: summary.dailyPnl,
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
      icon: '🇨🇳',
      identity: 'CNY · 同花顺',
      valueLabel: '账户净值',
      value: aStockValue,
      dailyLabel: '今日盈亏',
      dailyPnl: aStockDailyPnl,
      dailyRate: pctFromAmount(aStockDailyPnl, Math.max(aStockMarket - aStockDailyPnl, 1)),
      totalLabel: '总盈亏',
      totalPnl: safeNum(aStockSummary.totalPnl),
      totalRate: safeNum(aStockSummary.totalReturnRate || categoryByLabel(categories, 'A股个股').totalReturnRate),
      share: aStockValue / portfolioValue,
      miniMetrics: [
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
      totalRate: safeNum(fundSummary.totalReturnRate || categoryByLabel(categories, '基金').totalReturnRate),
      share: fundNlv / portfolioValue,
    },
    '美股/海外': {
      key: '美股/海外',
      icon: '🇺🇸🇰🇷🇭🇰',
      identity: 'USD · IBKR',
      value: ibkrNlv,
      dailyLabel: '今日盈亏',
      dailyPnl: categoryByLabel(categories, '美股/海外').dailyPnl,
      totalLabel: '总盈亏',
      totalPnl: ibkrTotalPnl,
      totalRate: ibkrTotalRate,
      share: ibkrNlv / portfolioValue,
      miniMetrics: [
        { label: '总市值', value: formatOptionalMoney(ibkrMarket) },
        { label: '可用现金', value: ibkrCash < 0 ? formatOptionalMoney(ibkrCash, 'CNY', { signed: true }) : formatOptionalMoney(ibkrCash), tone: ibkrCash < 0 ? 'down' : '' },
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
      totalRate: safeNum(preciousSummary.totalReturnRate || categoryByLabel(categories, '贵金属').totalReturnRate),
      share: safeNum(preciousSummary.totalValue) / portfolioValue,
    },
    '内部股份/分红': {
      key: '内部股份/分红',
      icon: 'H',
      identity: '股权分红',
      value: safeNum(esopSummary.totalValue),
      dailyLabel: '当年盈亏',
      dailyPnl: safeNum(esopSummary.totalPnl),
      dailyRate: safeNum(esopSummary.totalReturnRate || categoryByLabel(categories, '内部股份/分红').totalReturnRate),
      totalLabel: '总盈亏',
      totalPnl: safeNum(esopSummary.totalPnl),
      totalRate: safeNum(esopSummary.totalReturnRate || categoryByLabel(categories, '内部股份/分红').totalReturnRate),
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

function updateMobilePortfolioHero(group = currentMobileHeroGroup()) {
  if (!mobileHeroContext) return;
  const mobileHeroEl = document.getElementById('mobilePortfolioHero');
  if (!mobileHeroEl) return;
  const selected = normalizeMobileHeroGroup(group);
  const profile = mobileHeroContext.profiles[selected] || mobileHeroContext.profiles['全部'];
  mobileHeroEl.classList.toggle('mobileHeroAll', Boolean(profile.hideHeader));
  mobileHeroEl.classList.toggle('mobileHeroCategory', !profile.hideHeader);
  mobileHeroEl.innerHTML = MobilePortfolioHero(profile, mobileHeroContext.asOf, mobileHeroContext.dataIssues, mobileHeroContext.syncAt);
}

window.addEventListener('pams:holding-group-change', (event) => {
  updateMobilePortfolioHero(event.detail?.group);
});

function bindMobileMethodology(summary, dataIssues, asOf) {
  const btn = document.getElementById('mobileMethodologyTrigger');
  if (!btn) return;
  btn.addEventListener('click', () => {
    openBottomSheet('投资口径', `
      <div class="rowDetailGrid">
        <div><strong>快照日期</strong><span>${esc(asOf || '待核准')}</span></div>
        <div><strong>组合价值</strong><span class="mono">${formatCompactCurrency(summary.marketValue)}</span></div>
        <div><strong>总负债</strong><span class="mono">${formatCompactCurrency(safeNum(summary.totalLiabilities))}</span></div>
        <div><strong>现金/活期</strong><span class="mono">${formatCompactCurrency(summary.cashActiveValue, 'CNY', { signed: safeNum(summary.cashActiveValue) < 0 })}</span></div>
        <div><strong>现金负债</strong><span class="mono">${formatCompactCurrency(-safeNum(summary.cashActiveLiabilities), 'CNY', { signed: safeNum(summary.cashActiveLiabilities) > 0 })}</span></div>
        <div><strong>今日收益</strong><span class="mono">${formatCompactCurrency(summary.dailyPnl, 'CNY', { signed: true })}</span></div>
        <div><strong>总收益率</strong><span class="mono">${formatPercentRate(summary.twr)}</span></div>
      </div>
      ${dataIssues.length ? `<div class="issueList" style="margin-top:12px">${dataIssues.map(issue => `<div class="issueRow"><span>${esc(issue)}</span></div>`).join('')}</div>` : ''}
    `);
  });
}

function DataQualityBanner(issues) {
  if (!issues.length) return '';
  return `
    <a class="investmentDataDetails investmentDataLink" href="data_sources.html#dataQuality">口径 ${issues.length} 项</a>
  `;
}

function contributionPct(value, totalPnl) {
  const denom = Math.abs(safeNum(totalPnl));
  return denom > 0.0001 ? safeNum(value) / denom : 0;
}

function classifyDataQuality(row) {
  const major = String(row.major_category || '');
  const minor = String(row.minor_category || '');
  if (major === 'ESOP') return { label: '待确认', kind: 'pending' };
  if (major === '贵金属' || minor.includes('基金')) return { label: '中可信', kind: 'warn' };
  if (!row.current_price && !row.buy_price && !row.quantity) return { label: '需核准', kind: 'pending' };
  return { label: '高可信', kind: 'ok' };
}

function normalizeHolding(row, fallbackCategory = '') {
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

function formatNativeDaily(value, currency) {
  const cur = String(currency || 'CNY').toUpperCase();
  return formatCurrency(value, cur, {
    signed: true,
    minimumFractionDigits: cur === 'KRW' ? 0 : 2,
    maximumFractionDigits: cur === 'KRW' ? 0 : 2,
  });
}

function contributionItem(item, totalPnl, mode = 'total') {
  const value = mode === 'daily' ? item.totalDailyPnl : item.pnl;
  const rate = item.returnPct;
  const pct = contributionPct(value, totalPnl);
  const dailyMeta = mode === 'daily'
    ? [
        `${formatNativeDaily(item.nativeDailyPnl, item.currency)}`,
        Math.abs(safeNum(item.fxDailyPnl)) > 0.005 ? `汇率 ${fmtSignedCny(item.fxDailyPnl)}` : '',
      ].filter(Boolean).join(' · ')
    : formatPercentRate(rate);
  return `
    <div class="contributionItem" title="${esc([item.assetClass, item.account].filter(Boolean).join(' / ') || '')}">
      <div class="contributionMain">
        <strong>${esc(item.name)}</strong>
        <span>${statusBadge(item.assetClass, item.quality.kind)}</span>
      </div>
      <div class="contributionNumbers">
        <span style="color:${profitColor(value)}">${formatCompactCurrency(value, 'CNY', { signed: true })}</span>
        <small>${esc(dailyMeta)}</small>
      </div>
    </div>
  `;
}

function ContributionList(title, items, totalPnl, options = {}) {
  const mode = options.mode || 'total';
  const body = items.length
    ? items.slice(0, 5).map(item => contributionItem(item, totalPnl, mode)).join('')
    : `<div class="emptyState">${esc(options.emptyText || '现有接口没有足够数据，待补充。')}</div>`;
  return `
    <div class="contributionCard">
      ${title ? `<div class="sectionTitle">
        <h3>${esc(title)}</h3>
        ${options.badge ? statusBadge(options.badge, options.badgeKind || 'data') : ''}
      </div>` : ''}
      ${body}
    </div>
  `;
}

function ProfitContributionPanel(model) {
  const groups = [
    { key: 'win', label: '赚钱', html: ContributionList('', model.winners, model.totalPnl) },
    { key: 'loss', label: '亏损', html: ContributionList('', model.losers, model.totalPnl) },
    { key: 'day', label: '今日', html: ContributionList('', model.daily, model.dailyPnl, {
      mode: 'daily',
      emptyText: '单资产日度待补充。',
    }) },
  ];
  return `<div class="contributionTabs">
    <div class="segmented" data-contribution-tabs>
      ${groups.map((g, i) => `<button type="button" class="${i === 0 ? 'active' : ''}" data-contribution-tab="${esc(g.key)}">${esc(g.label)}</button>`).join('')}
    </div>
    ${groups.map((g, i) => `<div class="contributionPane" data-contribution-pane="${esc(g.key)}" ${i === 0 ? '' : 'hidden'}>${g.html}</div>`).join('')}
  </div>`;
}

function topMoverRow(item, options = {}) {
  const mode = options.mode || 'total';
  const value = mode === 'daily' ? safeNum(item.totalDailyPnl) : safeNum(item.pnl);
  const totalPnl = safeNum(options.totalPnl);
  const name = item.name || item.label || '未命名';
  const detail = [item.assetClass, item.account].filter(Boolean).join(' / ');
  const meta = mode === 'period'
    ? [
        formatPercentRate(item.returnPct),
        `贡献 ${formatPercentRate(contributionPct(value, totalPnl))}`,
        `期末占比 ${(safeNum(item.weight) * 100).toFixed(1)}%`,
      ].join(' · ')
    : mode === 'daily'
    ? [formatNativeDaily(item.nativeDailyPnl, item.currency), Math.abs(safeNum(item.fxDailyPnl)) > 0.005 ? `汇率 ${fmtSignedCny(item.fxDailyPnl)}` : '']
      .filter(Boolean).join(' · ')
    : `${formatPercentRate(item.returnPct)} · 权重 ${(safeNum(item.weight) * 100).toFixed(1)}%`;
  return `
    <div class="topMoverRow ${value >= 0 ? 'good' : 'bad'}">
      <div class="topMoverName">
        <strong>${esc(name)}</strong>
        <span>${esc(detail)}</span>
      </div>
      <div class="topMoverValue">
        <strong>${formatCompactCurrency(value, 'CNY', { signed: true })}</strong>
        <span>${esc(meta)}</span>
      </div>
    </div>
  `;
}

function topMoverGroup(title, rows, emptyText, options = {}) {
  return `
    <div class="topMoverGroup">
      <div class="topMoverGroupTitle">${esc(title)}</div>
      ${rows.length
        ? rows.slice(0, 3).map(item => topMoverRow(item, options)).join('')
        : `<div class="returnInsightEmpty">${esc(emptyText)}</div>`}
    </div>
  `;
}

function TopContributionPanel(model) {
  const winners = model?.winners || [];
  const losers = model?.losers || [];
  const daily = model?.daily || [];
  const unattributed = model?.unattributed || null;
  const mode = model?.mode || 'total';
  const windowLabel = model?.windowLabel || '当前';
  const totalPnl = safeNum(model?.totalPnl);
  const titleHint = mode === 'period'
    ? `${windowLabel}区间收益变化，随上方日期段切换`
    : '当前持仓收益贡献，不含自动调仓建议';
  const badge = mode === 'period' ? statusBadge('区间口径', 'data') : statusBadge('持仓快照', 'data');
  if (mode === 'period') {
    return `
      <div class="returnInsightHead">
        <div><h3>Top 贡献</h3><span>${esc(titleHint)}</span></div>
        ${badge}
      </div>
      <div class="topMoverTotal ${totalPnl >= 0 ? 'good' : 'bad'}">
        <span>${esc(windowLabel)}合计</span>
        <strong>${formatCompactCurrency(totalPnl, 'CNY', { signed: true })}</strong>
      </div>
      <div class="topMoverGrid">
        ${topMoverGroup('贡献最大', winners, '当前区间没有正贡献资产桶', { mode: 'period', totalPnl })}
        ${topMoverGroup('拖累最大', losers, '当前区间没有明显拖累资产桶', { mode: 'period', totalPnl })}
      </div>
      ${unattributed ? `<div class="returnInsightEmpty">
        未归因差额 ${formatCompactCurrency(unattributed.pnl, 'CNY', { signed: true })} · ${esc(unattributed.note)}
      </div>` : ''}
    `;
  }
  return `
    <div class="returnInsightHead">
      <div><h3>Top 贡献</h3><span>${esc(titleHint)}</span></div>
      ${badge}
    </div>
    <div class="topMoverGrid">
      ${topMoverGroup('贡献最大', winners, '暂无正贡献标的')}
      ${topMoverGroup('拖累最大', losers, '暂无明显拖累标的')}
    </div>
    ${daily.length ? `<div class="topMoverToday">
      <div class="topMoverGroupTitle">今日波动</div>
      ${daily.slice(0, 2).map(item => topMoverRow(item, { mode: 'daily' })).join('')}
    </div>` : ''}
  `;
}

async function renderInvestmentReturnChart() {
  const el = document.getElementById('investmentReturnChart');
  if (!el) return null;

  const sectionSpecs = [
    { key: 'a_stock', label: 'A股个股', color: '#f85149', url: '/investment/section-return-series?section=a_stock&start=2026-01-01&interval=daily&baseCurrency=CNY' },
    { key: 'a_fund', label: '基金', color: '#d29922', url: '/investment/section-return-series?section=a_fund&start=2026-01-01&interval=daily&baseCurrency=CNY' },
    { key: 'us_stock', label: '美股', color: '#58a6ff', url: '/investment/section-return-series?section=us_stock&start=2026-01-01&interval=daily&baseCurrency=CNY' },
    { key: 'precious_metal', label: '贵金属', color: '#a371f7', url: '/investment/section-return-series?section=precious_metal&start=2026-01-01&interval=daily&baseCurrency=CNY' },
  ];
  const [resp, navResp, ...sectionResponses] = await Promise.all([
    jget('/investment/return-series?start=2026-01-01&interval=daily&baseCurrency=CNY').catch(() => ({ rows: [] })),
    jget('/nav/weekly?start=2026-01-01&baseCurrency=CNY&latest=true&interval=daily').catch(() => ({ rows: [] })),
    ...sectionSpecs.map(spec => jget(spec.url).catch(() => ({ rows: [] }))),
  ]);
  const rows = resp.rows || [];
  const navRows = normalizeInvestmentNavRows(navResp.rows || []);
  const isMobileReturnChart = window.matchMedia?.('(max-width: 760px)')?.matches;
  const sectionRowsByKey = new Map(sectionSpecs.map((spec, index) => [spec.key, sectionResponses[index]?.rows || []]));
  const chartState = {
    rows,
    navRows,
    sectionRowsByKey,
    sectionSpecs,
    activeWindow: readReturnWindowPreference(),
    isMobile: isMobileReturnChart,
    events: buildReturnEvents(rows),
    visibleRows: [],
    visibleEvents: [],
    topContribution: null,
    pnlMap: new Map(),
    valueMap: new Map(),
    navChart: null,
  };
  const initialWindowData = buildInvestmentReturnWindowData(
    chartState.rows,
    chartState.sectionRowsByKey,
    chartState.sectionSpecs,
    chartState.activeWindow,
    chartState.isMobile,
    chartState.events,
  );
  chartState.visibleRows = initialWindowData.rows;
  chartState.visibleEvents = initialWindowData.visibleEvents;
  chartState.topContribution = initialWindowData.topContribution;
  chartState.pnlMap = initialWindowData.pnlMap;
  chartState.valueMap = initialWindowData.valueMap;
  const sectionDatasets = sectionSpecs.map((spec) => {
    const sectionData = initialWindowData.sectionDataByKey.get(spec.key) || { data: [], pointRadius: 2 };
    return {
      returnSeriesKey: spec.key,
      label: spec.label,
      data: sectionData.data,
      borderColor: spec.color,
      backgroundColor: `${spec.color}1a`,
      tension: 0.25,
      fill: false,
      pointRadius: sectionData.pointRadius,
      borderWidth: 1.8,
      hidden: true,
      spanGaps: true,
      order: 1,
    };
  });
  const noteEl = document.getElementById('investmentReturnNote');
  if (noteEl) {
    const notes = resp.coverage_notes || [];
    noteEl.style.display = notes.length ? 'block' : 'none';
    noteEl.textContent = notes.length
      ? `含多口径数据，分类线仅作参考。${notes.join(' ')}`
      : '';
  }

  if (window.__investmentReturnChart) window.__investmentReturnChart.destroy();
  if (typeof Chart === 'undefined') {
    window.__investmentReturnChart = createInvestmentReturnCanvasFallback(el, {
      labels: initialWindowData.labels,
      datasets: [{
        returnSeriesKey: 'total',
        label: '总收益率',
        data: initialWindowData.totalData,
        borderColor: '#58a6ff',
        hidden: false,
      }, {
        returnSeriesKey: 'events',
        label: '关键事件',
        data: initialWindowData.eventData,
        borderColor: '#f5a524',
        hidden: false,
      }, ...sectionDatasets],
    });
    bindReturnEventPopover(window.__investmentReturnChart, chartState);
    renderReturnEventHotspots(window.__investmentReturnChart, chartState);
    chartState.navChart = renderInvestmentNavChart(chartState.navRows, chartState.activeWindow, chartState.isMobile);
    renderInvestmentReturnToggle(window.__investmentReturnChart, sectionSpecs, chartState);
    renderInvestmentReturnWindowToggle(window.__investmentReturnChart, chartState);
    renderReturnContributionPanel(chartState.topContribution);
    document.body.dataset.investmentReturnChartLoaded = 'fallback';
    return rows.length
      ? { latest: rows[rows.length - 1], previous: rows.length > 1 ? rows[rows.length - 2] : null, rows, todayWindow: resp.today_window || null }
      : null;
  }
  window.__investmentReturnChart = new Chart(el, {
    type: 'line',
    data: {
      labels: initialWindowData.labels,
      datasets: [{
        returnSeriesKey: 'total',
        label: '总收益率',
        data: initialWindowData.totalData,
        borderColor: '#58a6ff',
        backgroundColor: 'rgba(88,166,255,.08)',
        tension: 0.25,
        fill: true,
        pointRadius: initialWindowData.totalPointRadius,
        borderWidth: 2.4,
        order: 0,
      }, {
        returnSeriesKey: 'events',
        label: '关键事件',
        data: initialWindowData.eventData,
        borderColor: '#f5a524',
        backgroundColor: '#f5a524',
        tension: 0,
        fill: false,
        showLine: false,
        pointStyle: 'triangle',
        pointRadius: initialWindowData.eventPointRadius,
        pointHoverRadius: 7,
        hitRadius: 12,
        borderWidth: 0,
        spanGaps: false,
        order: -1,
      }, ...sectionDatasets],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: 'index', intersect: false },
      layout: { padding: { bottom: isMobileReturnChart ? 8 : 0 } },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (ctx) => {
              const date = ctx.label;
              const value = safeNum(ctx.raw);
              if (ctx.dataset.returnSeriesKey === 'events') {
                const event = (chartState.visibleEvents || []).find(item => item.date === date);
                return event
                  ? [`${event.date} · ${event.label}`, returnEventSummary(event)]
                  : '关键事件';
              }
              if (ctx.dataset.label !== '总收益率') {
                return `${ctx.dataset.label}区间收益率：${value >= 0 ? '+' : ''}${value.toFixed(2)}%`;
              }
              const visibleRows = chartState.visibleRows || [];
              const row = visibleRows[ctx.dataIndex] || {};
              return [
                `区间总收益率：${value >= 0 ? '+' : ''}${value.toFixed(2)}%`,
                `单日收益率：${safeNum(row.daily_return_rate) * 100 >= 0 ? '+' : ''}${(safeNum(row.daily_return_rate) * 100).toFixed(2)}%`,
                `累计投资收益：¥${fmt2(chartState.pnlMap.get(date) || 0)}`,
                `投资市值：¥${fmt2(chartState.valueMap.get(date) || 0)}`,
              ];
            },
          },
        },
      },
      scales: {
        x: {
          ticks: {
            color: '#6e7681',
            maxRotation: 0,
            autoSkip: true,
            autoSkipPadding: isMobileReturnChart ? 34 : 12,
            maxTicksLimit: isMobileReturnChart ? 3 : 8,
            font: { size: isMobileReturnChart ? 11 : 12 },
            padding: isMobileReturnChart ? 8 : 3,
            callback: function(value) {
              const label = this.getLabelForValue(value);
              if (!isMobileReturnChart) return label;
              return /^\d{4}-\d{2}-\d{2}$/.test(label) ? label.slice(5).replace('-', '/') : label;
            },
          },
          grid: { color: 'rgba(110,118,129,.12)' },
        },
        y: {
          ticks: {
            color: '#6e7681',
            font: { size: isMobileReturnChart ? 11 : 12 },
            callback: v => `${safeNum(v).toFixed(1)}%`,
          },
          grid: { color: 'rgba(110,118,129,.12)' },
        },
      },
    },
    plugins: [{
      id: 'returnEventHotspots',
      afterRender(chart) {
        renderReturnEventHotspots(chart, chartState);
      },
    }],
  });
  bindReturnEventPopover(window.__investmentReturnChart, chartState);
  renderReturnEventHotspots(window.__investmentReturnChart, chartState);
  chartState.navChart = renderInvestmentNavChart(chartState.navRows, chartState.activeWindow, chartState.isMobile);
  renderInvestmentReturnToggle(window.__investmentReturnChart, sectionSpecs, chartState);
  renderInvestmentReturnWindowToggle(window.__investmentReturnChart, chartState);
  renderReturnContributionPanel(chartState.topContribution);
  document.body.dataset.investmentReturnChartLoaded = 'chart';
  return rows.length
    ? { latest: rows[rows.length - 1], previous: rows.length > 1 ? rows[rows.length - 2] : null, rows, todayWindow: resp.today_window || null }
    : null;
}

function showClosedPositions() {
  return new URLSearchParams(window.location.search).get('show_closed') === '1';
}

function withClosedParam(path) {
  if (!showClosedPositions()) return path;
  const separator = path.includes('?') ? '&' : '?';
  return `${path}${separator}include_closed=true`;
}

function initClosedToggle() {
  const toggles = Array.from(document.querySelectorAll('.showClosedPositionsToggle'));
  const legacyToggle = document.getElementById('showClosedPositions');
  if (!toggles.length && legacyToggle) toggles.push(legacyToggle);
  if (!toggles.length) return;
  const checked = showClosedPositions();
  for (const el of toggles) {
    el.checked = checked;
  }
  for (const el of toggles) {
    el.addEventListener('change', () => {
      const nextChecked = el.checked;
      for (const other of toggles) {
        other.checked = nextChecked;
      }
      const url = new URL(window.location.href);
      if (nextChecked) {
        url.searchParams.set('show_closed', '1');
      } else {
        url.searchParams.delete('show_closed');
      }
      window.location.href = url.toString();
    });
  }
}

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[ch]));
}

function setKpiLabel(valueId, label) {
  const valueEl = document.getElementById(valueId);
  const labelEl = valueEl?.closest('.kpi')?.querySelector('.kpi-label');
  if (labelEl) labelEl.textContent = label;
}

function renderIbkrPerformance(perf, positionAsOf = null) {
  if (!perf || perf.status !== 'ok') return null;

  const flexCoversPositionDate = !positionAsOf || isSameOrAfterDate(perf.as_of, positionAsOf);
  const summary = {
    totalValue: safeNum(perf.net_liquidation_cny),
    totalCost: safeNum(perf.net_deposit_cny),
    totalPnl: safeNum(perf.total_pnl_cny),
    dailyPnl: null,
    positionValue: safeNum(perf.position_value_cny),
    cashValue: safeNum(perf.cash_cny),
    totalReturnRate: safeNum(perf.return_ratio),
    asOf: perf.as_of,
    flexCoversPositionDate,
  };

  if (!flexCoversPositionDate) return summary;

  setKpiLabel('totalValueUSStock', '账户净值');
  setKpiLabel('totalPnlUSStock', '账户收益');
  setKpiLabel('countKpiUSStock', '杠杆比例');
  setKpiLabel('holdingPnlUSStock', '收益率');

  const totalPnl = safeNum(perf.total_pnl_base);
  const totalPnlEl = document.getElementById('totalPnlUSStock');
  const returnEl = document.getElementById('holdingPnlUSStock');

  document.getElementById('totalValueUSStock').textContent = fmtUsd(perf.net_liquidation_base);
  totalPnlEl.textContent = fmtSignedUsd(totalPnl);
  totalPnlEl.style.color = totalPnl >= 0 ? 'var(--good)' : 'var(--bad)';
  document.getElementById('countKpiUSStock').textContent = fmtPctValue(perf.leverage_ratio);
  returnEl.textContent = (safeNum(perf.return_ratio) >= 0 ? '+' : '') + fmtPctValue(perf.return_ratio);
  returnEl.style.color = safeNum(perf.return_ratio) >= 0 ? 'var(--good)' : 'var(--bad)';

  return summary;
}

function renderAStockOwnerPerformance(perf) {
  if (!perf || perf.status !== 'ok') return null;

  setKpiLabel('totalValueAStockEquity', '个人净值');
  setKpiLabel('totalPnlAStockEquity', '账户收益');
  setKpiLabel('countKpiAStockEquity', '持股比例');
  setKpiLabel('holdingPnlAStockEquity', '收益率');

  const totalPnl = safeNum(perf.total_pnl_cny);
  const returnRatio = safeNum(perf.return_ratio);
  const dailyPnlEffective = perf.daily_pnl_effective !== false;
  const dailyPnl = dailyPnlEffective && Number.isFinite(Number(perf.daily_pnl_cny))
    ? safeNum(perf.daily_pnl_cny)
    : null;

  const totalPnlEl = document.getElementById('totalPnlAStockEquity');
  const dailyPnlEl = document.getElementById('dailyPnlAStockEquity');
  const returnEl = document.getElementById('holdingPnlAStockEquity');

  document.getElementById('totalValueAStockEquity').textContent = fmtCny(perf.net_liquidation_cny);
  totalPnlEl.textContent = fmtSignedCny(totalPnl);
  totalPnlEl.style.color = totalPnl >= 0 ? 'var(--good)' : 'var(--bad)';
  if (dailyPnlEl && dailyPnl !== null) {
    dailyPnlEl.textContent = fmtSignedCny(dailyPnl);
    dailyPnlEl.style.color = dailyPnl >= 0 ? 'var(--good)' : 'var(--bad)';
  }
  document.getElementById('countKpiAStockEquity').textContent = fmtPctValue(perf.owner_ratio);
  returnEl.textContent = (returnRatio >= 0 ? '+' : '') + fmtPctValue(returnRatio);
  returnEl.style.color = returnRatio >= 0 ? 'var(--good)' : 'var(--bad)';

  return {
    totalValue: safeNum(perf.net_liquidation_cny),
    totalPnl,
    dailyPnl,
    dailyPnlEffective,
    totalReturnRate: returnRatio,
  };
}

// 渲染行: A股/美股 (7列: 代码,名称,买入价格,数量,当前价格,持仓市值,收益率)
function renderRowStock(r){
  const pct = displayPnlPct(r);
  const symbol = r.symbol || '—';
  const name = r.name || '';
  const quantityText = r.quantity ? formatTableNumber(r.quantity, 3) : '数量缺失';
  return `
    <td class="nowrap" title="${esc(symbol)}">${esc(symbol)}</td>
    <td class="name" title="${esc(name)}"><span class="mobileHoldingQtyPill">${esc(quantityText)}</span><span class="holdingNameText">${esc(name)}</span>${closedBadge(r)}</td>
    <td class="mono num">${r.buy_price ? formatTableNumber(r.buy_price, 3) : '价格缺失'}</td>
    <td class="mono num">${quantityText}</td>
    <td class="mono num">${r.current_price ? formatTableNumber(r.current_price, 3) : '价格缺失'}</td>
    <td class="mono num">${formatCurrency(cnyValue(r, 'market_value'), 'CNY', { maximumFractionDigits: 2 })}</td>
    <td class="mono num" style="color:${pct>=0?'var(--good)':'var(--bad)'}">${pct>=0?'+':''}${pct.toFixed(2)}%</td>
  `;
}

// 渲染行: 基金/贵金属 (6列: 名称,买入价格,数量,当前价格,持仓市值,收益率)
function renderRowFundLike(r){
  const pct = displayPnlPct(r);
  const name = r.name || '';
  const quantityText = r.quantity ? formatTableNumber(r.quantity, 3) : '份额缺失';
  return `
    <td class="name" title="${esc(name)}"><span class="mobileHoldingQtyPill">${esc(quantityText)}</span><span class="holdingNameText">${esc(name)}</span>${closedBadge(r)}</td>
    <td class="mono num">${r.buy_price ? formatTableNumber(r.buy_price, 3) : '净值型/待核准'}</td>
    <td class="mono num">${quantityText}</td>
    <td class="mono num">${r.current_price ? formatTableNumber(r.current_price, 3) : '净值型/待核准'}</td>
    <td class="mono num">${formatCurrency(cnyValue(r, 'market_value'), 'CNY', { maximumFractionDigits: 2 })}</td>
    <td class="mono num" style="color:${pct>=0?'var(--good)':'var(--bad)'}">${pct>=0?'+':''}${pct.toFixed(2)}%</td>
  `;
}

function closedBadge(r) {
  return r.is_closed ? '<span class="closedBadge">已平仓</span>' : '';
}

function displayPnlPct(r) {
  const cost = safeNum(cnyValue(r, 'cost'));
  if (cost > 0) return safeNum(cnyValue(r, 'holding_pnl')) / cost * 100;
  return r.is_closed ? 0 : pnlPct(r);
}

// 渲染持仓表格和汇总统计
function renderSection(rows, renderRowFn, tbodyId, countId, totalValueId, totalPnlId, dailyPnlId, countKpiId, holdingPnlId, fxRates){
  const tbody = document.getElementById(tbodyId);
  tbody.innerHTML='';

  let totalValue = 0;
  let totalPnl = 0;
  let dailyPnl = 0;
  let fxDailyPnl = 0;
  let dailyTotalPnl = 0;
  let totalCost = 0;

  for(const r of rows){
    const tr=document.createElement('tr');
    tr.innerHTML = renderRowFn(r);
    tbody.appendChild(tr);

    totalValue += safeNum(cnyValue(r, 'market_value'));
    totalPnl += safeNum(cnyValue(r, 'holding_pnl'));
    const rowDailyPnl = safeNum(cnyValue(r, 'daily_pnl'));
    const rowFxDailyPnl = safeNum(cnyValue(r, 'daily_fx_pnl'));
    const rowDailyTotalPnl = (r.daily_total_pnl_cny != null || r.daily_total_pnl != null)
      ? safeNum(cnyValue(r, 'daily_total_pnl'))
      : rowDailyPnl + rowFxDailyPnl;
    dailyPnl += rowDailyPnl;
    fxDailyPnl += rowFxDailyPnl;
    dailyTotalPnl += rowDailyTotalPnl;
    totalCost += safeNum(cnyValue(r, 'cost'));
  }

  document.getElementById(countId).textContent = rows.length;
  document.getElementById(countKpiId).textContent = rows.length;
  document.getElementById(totalValueId).textContent = '¥' + fmt2(totalValue);

  const pnlElem = document.getElementById(totalPnlId);
  pnlElem.textContent = (totalPnl >= 0 ? '+¥' : '-¥') + fmt2(Math.abs(totalPnl));
  pnlElem.style.color = totalPnl >= 0 ? 'var(--good)' : 'var(--bad)';

  const dailyElem = document.getElementById(dailyPnlId);
  dailyElem.textContent = fmtSignedCny(dailyTotalPnl);
  dailyElem.style.color = dailyTotalPnl >= 0 ? 'var(--good)' : 'var(--bad)';
  setKpiFxNote(dailyPnlId, fxDailyPnl);

  if (holdingPnlId) {
    const hpElem = document.getElementById(holdingPnlId);
    const sectionCost = totalCost > 0 ? totalCost : totalValue - totalPnl;
    const sectionPct = sectionCost > 0 ? (totalPnl / sectionCost * 100) : 0;
    hpElem.textContent = (sectionPct >= 0 ? '+' : '') + sectionPct.toFixed(2) + '%';
    hpElem.style.color = sectionPct >= 0 ? 'var(--good)' : 'var(--bad)';
  }

  return { totalValue, totalPnl, dailyPnl, fxDailyPnl, dailyTotalPnl, totalCost };
}

function summarizeRows(rows) {
  let totalValue = 0;
  let totalPnl = 0;
  let dailyPnl = 0;
  let fxDailyPnl = 0;
  let totalCost = 0;
  for (const r of rows) {
    totalValue += safeNum(cnyValue(r, 'market_value'));
    totalPnl += safeNum(cnyValue(r, 'holding_pnl'));
    dailyPnl += safeNum(cnyValue(r, 'daily_pnl'));
    fxDailyPnl += safeNum(cnyValue(r, 'daily_fx_pnl'));
    totalCost += safeNum(cnyValue(r, 'cost'));
  }
  return { totalValue, totalPnl, dailyPnl, fxDailyPnl, dailyTotalPnl: dailyPnl + fxDailyPnl, totalCost };
}

function writeSectionFinancials(summary, totalValueId, totalPnlId, dailyPnlId, holdingPnlId) {
  const { totalValue, totalPnl, dailyPnl, totalCost } = summary;
  document.getElementById(totalValueId).textContent = fmtCny(totalValue);

  const pnlElem = document.getElementById(totalPnlId);
  pnlElem.textContent = fmtSignedCny(totalPnl);
  pnlElem.style.color = totalPnl >= 0 ? 'var(--good)' : 'var(--bad)';

  const dailyElem = document.getElementById(dailyPnlId);
  dailyElem.textContent = fmtSignedCny(dailyPnl);
  dailyElem.style.color = dailyPnl >= 0 ? 'var(--good)' : 'var(--bad)';

  if (holdingPnlId) {
    const hpElem = document.getElementById(holdingPnlId);
    const explicitRate = Number(summary.totalReturnRate);
    const sectionCost = totalCost > 0 ? totalCost : totalValue - totalPnl;
    const sectionPct = Number.isFinite(explicitRate)
      ? explicitRate * 100
      : (sectionCost > 0 ? (totalPnl / sectionCost * 100) : 0);
    hpElem.textContent = (sectionPct >= 0 ? '+' : '') + sectionPct.toFixed(2) + '%';
    hpElem.style.color = sectionPct >= 0 ? 'var(--good)' : 'var(--bad)';
  }
}

function returnRateFromCapitalBase(summary, capitalBaseSummary = summary) {
  const totalPnl = safeNum(summary?.totalPnl);
  const capitalCost = safeNum(capitalBaseSummary?.totalCost);
  const impliedCapital = safeNum(capitalBaseSummary?.totalValue) - safeNum(capitalBaseSummary?.totalPnl);
  const denominator = capitalCost > 0 ? capitalCost : Math.max(impliedCapital, 0);
  return denominator > 0 ? totalPnl / denominator : 0;
}

const RETURN_WINDOW_STORAGE_KEY = 'pams.investmentReturn.window';
const RETURN_WINDOW_OPTIONS = [
  { key: 'ytd', label: '年初至今' },
  { key: '1m', label: '近1月' },
  { key: '1w', label: '近1周' },
];
const RETURN_EVENT_LIMIT = 4;

function normalizeReturnWindowKey(key) {
  return RETURN_WINDOW_OPTIONS.some(item => item.key === key) ? key : 'ytd';
}

function readReturnWindowPreference() {
  try {
    if (typeof localStorage === 'undefined') return 'ytd';
    return normalizeReturnWindowKey(localStorage.getItem(RETURN_WINDOW_STORAGE_KEY));
  } catch (_) {
    return 'ytd';
  }
}

function writeReturnWindowPreference(key) {
  try {
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem(RETURN_WINDOW_STORAGE_KEY, normalizeReturnWindowKey(key));
    }
  } catch (_) {
    // Some embedded browser contexts disable storage; chart controls should still work.
  }
}

function parseReturnSeriesDate(value) {
  if (!value) return null;
  const raw = String(value).slice(0, 10);
  const match = raw.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!match) return null;
  const parsed = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])));
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function returnWindowStartDate(rows, windowKey) {
  const dates = (rows || []).map(row => parseReturnSeriesDate(row.date)).filter(Boolean);
  if (!dates.length) return null;
  const latest = new Date(Math.max(...dates.map(date => date.getTime())));
  const start = new Date(latest);
  if (windowKey === '1w') {
    start.setUTCDate(start.getUTCDate() - 7);
    return start;
  }
  if (windowKey === '1m') {
    start.setUTCMonth(start.getUTCMonth() - 1);
    return start;
  }
  start.setUTCMonth(0, 1);
  return start;
}

function filterReturnWindowRows(rows, windowKey) {
  const start = returnWindowStartDate(rows, windowKey);
  if (!start) return rows || [];
  return (rows || []).filter(row => {
    const date = parseReturnSeriesDate(row.date);
    return date && date >= start;
  });
}

function returnRateForChart(row) {
  return safeNum(row?.display_return_rate ?? row?.time_weighted_return_rate ?? row?.return_rate);
}

function rebaseReturnRate(rate, baseRate) {
  const denominator = 1 + safeNum(baseRate);
  if (Math.abs(denominator) < 0.000001) return safeNum(rate) - safeNum(baseRate);
  return ((1 + safeNum(rate)) / denominator) - 1;
}

function returnWindowLabel(windowKey) {
  return RETURN_WINDOW_OPTIONS.find(item => item.key === windowKey)?.label || '年初至今';
}

function returnEventCapitalBase(row, prev) {
  const prevMarketValue = Math.abs(safeNum(prev?.market_value));
  if (prevMarketValue > 0.005) return prevMarketValue;
  const prevCost = Math.abs(safeNum(prev?.cost));
  if (prevCost > 0.005) return prevCost;
  return Math.abs(safeNum(row?.market_value));
}

function returnEventAdjustedAmount(row, prev) {
  return safeNum(row?.daily_return_rate) * returnEventCapitalBase(row, prev);
}

function returnEventMeta(row, prev) {
  const dailyRate = safeNum(row?.daily_return_rate);
  const realizedDelta = safeNum(row?.realized_income) - safeNum(prev?.realized_income);
  const costDelta = safeNum(row?.cost) - safeNum(prev?.cost);
  const adjustment = safeNum(row?.reported_return_adjustment);
  if (Math.abs(realizedDelta) >= 1000) {
    return { label: '分红/已实现', tone: realizedDelta >= 0 ? 'good' : 'bad' };
  }
  if (Math.abs(adjustment) >= 100) {
    return { label: '口径修正', tone: 'warn' };
  }
  if (Math.abs(costDelta) >= 10000) {
    return { label: '持仓/资金变动', tone: 'info' };
  }
  return { label: dailyRate >= 0 ? '价格推动' : '价格拖累', tone: dailyRate >= 0 ? 'good' : 'bad' };
}

function buildReturnEvents(rows) {
  const events = [];
  for (let index = 1; index < (rows || []).length; index += 1) {
    const row = rows[index];
    const prev = rows[index - 1];
    const dailyRate = safeNum(row.daily_return_rate);
    const pnlDelta = returnEventAdjustedAmount(row, prev);
    const rawPnlDelta = safeNum(row.total_pnl) - safeNum(prev.total_pnl);
    const realizedDelta = safeNum(row.realized_income) - safeNum(prev.realized_income);
    const costDelta = safeNum(row.cost) - safeNum(prev.cost);
    const adjustment = safeNum(row.reported_return_adjustment);
    const hasSignal = (
      Math.abs(dailyRate) >= 0.008 ||
      Math.abs(pnlDelta) >= 5000 ||
      Math.abs(realizedDelta) >= 1000 ||
      Math.abs(costDelta) >= 12000 ||
      Math.abs(adjustment) >= 100
    );
    if (!hasSignal) continue;
    const meta = returnEventMeta(row, prev);
    const score = (
      Math.abs(dailyRate) * 100 +
      Math.min(Math.abs(pnlDelta) / 20000, 3) +
      Math.min(Math.abs(realizedDelta) / 10000, 3) +
      Math.min(Math.abs(costDelta) / 50000, 2)
    );
    events.push({
      date: row.date,
      label: meta.label,
      tone: meta.tone,
      dailyRate,
      pnlDelta,
      rawPnlDelta,
      realizedDelta,
      costDelta,
      score,
      direction: dailyRate >= 0 ? 'up' : 'down',
    });
  }
  return events.sort((a, b) => b.score - a.score).slice(0, 12);
}

function pointRadiusForReturnSeries(pointCount, isMobile) {
  if (pointCount <= 2) return 3;
  return pointCount > (isMobile ? 28 : 45) ? 0 : 2;
}

function buildInvestmentReturnWindowData(rows, sectionRowsByKey, sectionSpecs, windowKey, isMobile, events = []) {
  const windowRows = filterReturnWindowRows(rows, windowKey);
  const labels = windowRows.map(row => row.date);
  const totalBaseRate = windowRows.length ? returnRateForChart(windowRows[0]) : 0;
  const totalData = windowRows.map(row => rebaseReturnRate(returnRateForChart(row), totalBaseRate) * 100);
  const labelSet = new Set(labels);
  const visibleEvents = (events || [])
    .filter(event => labelSet.has(event.date))
    .sort((a, b) => b.score - a.score)
    .slice(0, RETURN_EVENT_LIMIT);
  const eventDates = new Set(visibleEvents.map(event => event.date));
  const eventData = labels.map((date, index) => eventDates.has(date) ? totalData[index] : null);
  const sectionDataByKey = new Map();
  for (const spec of sectionSpecs) {
    const rateByDate = new Map((sectionRowsByKey.get(spec.key) || []).map(row => [row.date, returnRateForChart(row)]));
    let sectionBaseRate = null;
    const sectionData = labels.map(date => {
      if (!rateByDate.has(date)) return null;
      const rate = rateByDate.get(date);
      if (!Number.isFinite(Number(rate))) return null;
      if (sectionBaseRate == null) sectionBaseRate = rate;
      return rebaseReturnRate(rate, sectionBaseRate) * 100;
    });
    sectionDataByKey.set(spec.key, {
      data: sectionData,
      pointRadius: pointRadiusForReturnSeries(sectionData.filter(value => value != null).length, isMobile),
    });
  }
  return {
    labels,
    rows: windowRows,
    totalData,
    totalPointRadius: pointRadiusForReturnSeries(totalData.length, isMobile),
    sectionDataByKey,
    eventData,
    eventPointRadius: eventData.map(value => value == null ? 0 : 4.5),
    visibleEvents,
    pnlMap: new Map(windowRows.map(row => [row.date, safeNum(row.total_pnl)])),
    valueMap: new Map(windowRows.map(row => [row.date, safeNum(row.market_value)])),
    topContribution: buildTopContributionForWindow(sectionRowsByKey, sectionSpecs, windowKey, rows),
  };
}

function normalizeInvestmentNavRows(rows) {
  return (rows || [])
    .map(row => {
      const date = row.date || row.asOfDate || row.snapshot_date;
      const marketValue = safeNum(row.netWorth ?? row.net_worth ?? row.market_value);
      if (!date || !Number.isFinite(marketValue)) return null;
      return {
        ...row,
        date,
        market_value: marketValue,
        assets: safeNum(row.assets),
        liabilities: safeNum(row.liabilities),
      };
    })
    .filter(Boolean)
    .sort((a, b) => String(a.date).localeCompare(String(b.date)));
}

function buildInvestmentNavChartData(windowRows, isMobile) {
  const rows = windowRows || [];
  const labels = rows.map(row => row.date);
  const values = rows.map(row => safeNum(row.market_value));
  const first = values.length ? values[0] : 0;
  const latest = values.length ? values[values.length - 1] : 0;
  const change = latest - first;
  const changeRate = Math.abs(first) > 0.005 ? change / first : 0;
  let peak = first;
  let maxDrawdown = 0;
  for (const value of values) {
    peak = Math.max(peak, value);
    if (peak > 0.005) maxDrawdown = Math.min(maxDrawdown, value / peak - 1);
  }
  return {
    labels,
    values,
    first,
    latest,
    change,
    changeRate,
    maxDrawdown,
    pointRadius: pointRadiusForReturnSeries(values.length, isMobile),
  };
}

function drawInvestmentReturnCanvasFallback(canvas, chartData) {
  if (!canvas) return;
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(rect.width || canvas.clientWidth || 320, 240);
  const height = Math.max(rect.height || canvas.clientHeight || 220, 160);
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.floor(width * dpr);
  canvas.height = Math.floor(height * dpr);
  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);
  const labels = chartData?.labels || [];
  const visibleDatasets = (chartData?.datasets || [])
    .filter(dataset => !dataset.hidden && dataset.returnSeriesKey !== 'events')
    .map(dataset => ({
      ...dataset,
      values: (dataset.data || []).map(value => Number(value)).filter(value => Number.isFinite(value)),
    }))
    .filter(dataset => dataset.values.length);
  if (!labels.length || !visibleDatasets.length) {
    ctx.fillStyle = '#8b949e';
    ctx.font = '12px sans-serif';
    ctx.fillText('暂无收益曲线数据', 16, 28);
    return;
  }
  const allValues = visibleDatasets.flatMap(dataset => dataset.values);
  const minValue = Math.min(...allValues);
  const maxValue = Math.max(...allValues);
  const range = Math.max(maxValue - minValue, 1);
  const pad = { left: 18, right: 18, top: 14, bottom: 24 };
  const innerW = Math.max(width - pad.left - pad.right, 1);
  const innerH = Math.max(height - pad.top - pad.bottom, 1);
  const xAt = (index) => pad.left + (labels.length === 1 ? innerW : innerW * index / (labels.length - 1));
  const yAt = (value) => pad.top + innerH - ((value - minValue) / range) * innerH;

  ctx.strokeStyle = 'rgba(110,118,129,.14)';
  ctx.lineWidth = 1;
  for (let i = 0; i < 4; i += 1) {
    const y = pad.top + innerH * i / 3;
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(width - pad.right, y);
    ctx.stroke();
  }

  for (const dataset of visibleDatasets) {
    const values = dataset.data || [];
    ctx.beginPath();
    let hasPoint = false;
    values.forEach((value, index) => {
      const n = Number(value);
      if (!Number.isFinite(n)) return;
      const x = xAt(index);
      const y = yAt(n);
      if (!hasPoint) {
        ctx.moveTo(x, y);
        hasPoint = true;
      } else {
        ctx.lineTo(x, y);
      }
    });
    if (!hasPoint) continue;
    ctx.strokeStyle = dataset.borderColor || '#58a6ff';
    ctx.lineWidth = dataset.returnSeriesKey === 'total' ? 2.2 : 1.6;
    ctx.stroke();
  }

  ctx.fillStyle = '#8b949e';
  ctx.font = '11px sans-serif';
  ctx.fillText(`${maxValue.toFixed(1)}%`, pad.left, 11);
  ctx.fillText(`${minValue.toFixed(1)}%`, pad.left, height - 6);
}

function createInvestmentReturnCanvasFallback(canvas, data) {
  const chart = {
    __fallback: true,
    canvas,
    data,
    options: {},
    scales: {},
    destroy() {},
    update() {
      drawInvestmentReturnCanvasFallback(canvas, this.data);
    },
    getDatasetMeta() {
      return { data: [] };
    },
    getElementsAtEventForMode() {
      return [];
    },
  };
  chart.update();
  return chart;
}

function navAxisLabel(value) {
  const n = safeNum(value);
  if (Math.abs(n) >= 10000) return `${(n / 10000).toFixed(Math.abs(n) >= 1000000 ? 0 : 1)}万`;
  return formatNumber(n, { maximumFractionDigits: 0 });
}

function renderInvestmentNavSummary(data, windowKey) {
  const rangeEl = document.getElementById('investmentNavRange');
  const statsEl = document.getElementById('investmentNavStats');
  if (rangeEl) rangeEl.textContent = returnWindowLabel(windowKey);
  if (!statsEl) return;
  if (!data || !data.values.length) {
    statsEl.innerHTML = '<span class="muted">暂无NAV数据</span>';
    return;
  }
  statsEl.innerHTML = `
    <span><em>当前</em><strong>${fmtCny(data.latest)}</strong></span>
    <span class="${data.change >= 0 ? 'up' : 'down'}"><em>区间</em><strong>${fmtSignedCny(data.change)}</strong></span>
    <span class="${data.changeRate >= 0 ? 'up' : 'down'}"><em>变化</em><strong>${formatPercentRate(data.changeRate)}</strong></span>
  `;
}

function drawInvestmentNavCanvasFallback(canvas, data) {
  if (!canvas) return;
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(rect.width || canvas.clientWidth || 320, 240);
  const height = Math.max(rect.height || canvas.clientHeight || 160, 120);
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.floor(width * dpr);
  canvas.height = Math.floor(height * dpr);
  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);
  const values = data?.values || [];
  if (!values.length) {
    ctx.fillStyle = '#8b949e';
    ctx.font = '12px sans-serif';
    ctx.fillText('暂无NAV数据', 16, 28);
    return;
  }
  const pad = { left: 14, right: 16, top: 12, bottom: 22 };
  const innerW = Math.max(width - pad.left - pad.right, 1);
  const innerH = Math.max(height - pad.top - pad.bottom, 1);
  const minValue = Math.min(...values);
  const maxValue = Math.max(...values);
  const range = Math.max(maxValue - minValue, Math.max(Math.abs(maxValue), 1) * 0.02);
  const xAt = (index) => pad.left + (values.length === 1 ? innerW : innerW * index / (values.length - 1));
  const yAt = (value) => pad.top + innerH - ((value - minValue) / range) * innerH;

  ctx.strokeStyle = 'rgba(110,118,129,.16)';
  ctx.lineWidth = 1;
  for (let i = 0; i < 4; i += 1) {
    const y = pad.top + innerH * i / 3;
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(width - pad.right, y);
    ctx.stroke();
  }

  const gradient = ctx.createLinearGradient(0, pad.top, 0, height - pad.bottom);
  gradient.addColorStop(0, 'rgba(126,231,135,.18)');
  gradient.addColorStop(1, 'rgba(126,231,135,0)');
  ctx.beginPath();
  values.forEach((value, index) => {
    const x = xAt(index);
    const y = yAt(value);
    if (index === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.lineTo(xAt(values.length - 1), height - pad.bottom);
  ctx.lineTo(xAt(0), height - pad.bottom);
  ctx.closePath();
  ctx.fillStyle = gradient;
  ctx.fill();

  ctx.beginPath();
  values.forEach((value, index) => {
    const x = xAt(index);
    const y = yAt(value);
    if (index === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.strokeStyle = '#7ee787';
  ctx.lineWidth = 2;
  ctx.stroke();

  const lastX = xAt(values.length - 1);
  const lastY = yAt(values[values.length - 1]);
  ctx.beginPath();
  ctx.arc(lastX, lastY, 3.5, 0, Math.PI * 2);
  ctx.fillStyle = '#7ee787';
  ctx.fill();

  ctx.fillStyle = '#8b949e';
  ctx.font = '11px sans-serif';
  ctx.fillText(navAxisLabel(maxValue), pad.left, 11);
  ctx.fillText(navAxisLabel(minValue), pad.left, height - 6);
}

function renderInvestmentNavChart(rows, windowKey, isMobile) {
  const el = document.getElementById('investmentNavChart');
  if (!el) return null;
  const data = buildInvestmentNavChartData(filterReturnWindowRows(rows, windowKey), isMobile);
  renderInvestmentNavSummary(data, windowKey);
  if (typeof Chart === 'undefined') {
    drawInvestmentNavCanvasFallback(el, data);
    window.__investmentNavChart = {
      __fallback: true,
      canvas: el,
      data: { labels: data.labels, datasets: [{ label: 'NAV', data: data.values }] },
      destroy() {},
      update() {
        drawInvestmentNavCanvasFallback(el, {
          labels: this.data.labels,
          values: this.data.datasets[0].data,
        });
      },
    };
    return window.__investmentNavChart;
  }
  if (window.__investmentNavChart) window.__investmentNavChart.destroy();
  window.__investmentNavChart = new Chart(el, {
    type: 'line',
    data: {
      labels: data.labels,
      datasets: [{
        label: 'NAV',
        data: data.values,
        borderColor: '#7ee787',
        backgroundColor: 'rgba(126,231,135,.10)',
        tension: 0.25,
        fill: true,
        pointRadius: data.pointRadius,
        borderWidth: 2,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (ctx) => `NAV：${fmtCny(ctx.raw)}`,
          },
        },
      },
      scales: {
        x: {
          ticks: {
            color: '#6e7681',
            maxRotation: 0,
            autoSkip: true,
            maxTicksLimit: isMobile ? 3 : 7,
            callback: function(value) {
              const label = this.getLabelForValue(value);
              return isMobile && /^\d{4}-\d{2}-\d{2}$/.test(label) ? label.slice(5).replace('-', '/') : label;
            },
          },
          grid: { color: 'rgba(110,118,129,.10)' },
        },
        y: {
          beginAtZero: false,
          ticks: {
            color: '#6e7681',
            callback: navAxisLabel,
          },
          grid: { color: 'rgba(110,118,129,.10)' },
        },
      },
    },
  });
  return window.__investmentNavChart;
}

function updateInvestmentNavChart(state) {
  const chart = state?.navChart || window.__investmentNavChart;
  if (!chart || !state) return;
  const windowRows = filterReturnWindowRows(state.navRows || state.rows || [], state.activeWindow);
  const data = buildInvestmentNavChartData(windowRows, state.isMobile);
  chart.data.labels = data.labels;
  chart.data.datasets[0].data = data.values;
  if (chart.__fallback) {
    chart.update();
    renderInvestmentNavSummary(data, state.activeWindow);
    return;
  }
  chart.data.datasets[0].pointRadius = data.pointRadius;
  chart.update('none');
  renderInvestmentNavSummary(data, state.activeWindow);
}

function returnEventSummary(event) {
  const parts = [
    `单日 ${fmtSignedPctFromRate(event.dailyRate)}`,
    `收益 ${fmtSignedCny(event.pnlDelta)}`,
  ];
  if (Math.abs(event.realizedDelta) >= 1000) parts.push(`已实现 ${fmtSignedCny(event.realizedDelta)}`);
  if (Math.abs(event.costDelta) >= 12000) parts.push(`成本 ${fmtSignedCny(event.costDelta)}`);
  return parts.join(' · ');
}

function sectionReturnAmount(row) {
  if (!row) return 0;
  if (Number.isFinite(Number(row.total_pnl))) return safeNum(row.total_pnl);
  if (Number.isFinite(Number(row.position_pnl))) return safeNum(row.position_pnl);
  return safeNum(row.market_value) - safeNum(row.cumulative_capital);
}

function sectionPeriodPnl(rows) {
  const ordered = rows || [];
  if (ordered.length < 2) return 0;
  return sectionReturnAmount(ordered[ordered.length - 1]) - sectionReturnAmount(ordered[0]);
}

function sectionPeriodReturnRate(rows, pnl) {
  const first = rows?.[0];
  const last = rows?.[rows.length - 1];
  const baseValue = Math.abs(safeNum(first?.market_value));
  if (baseValue > 0.005) return safeNum(pnl) / baseValue;
  return rebaseReturnRate(returnRateForChart(last), returnRateForChart(first));
}

function buildTopContributionForWindow(sectionRowsByKey, sectionSpecs, windowKey, totalRows) {
  const totalWindowRows = filterReturnWindowRows(totalRows, windowKey);
  const startDate = totalWindowRows[0]?.date || null;
  const endDate = totalWindowRows[totalWindowRows.length - 1]?.date || null;
  const seriesTotalPnl = totalWindowRows.length >= 2
    ? safeNum(totalWindowRows[totalWindowRows.length - 1].total_pnl) - safeNum(totalWindowRows[0].total_pnl)
    : 0;
  const totalPnl = seriesTotalPnl;
  const latestTotalValue = safeNum(totalWindowRows[totalWindowRows.length - 1]?.market_value);
  const rows = sectionSpecs.map((spec) => {
    const periodRows = (sectionRowsByKey.get(spec.key) || []).filter((row) => {
      const date = String(row.date || '');
      return (!startDate || date >= startDate) && (!endDate || date <= endDate);
    });
    const pnl = sectionPeriodPnl(periodRows);
    const latestValue = safeNum(periodRows[periodRows.length - 1]?.market_value);
    return {
      key: spec.key,
      name: spec.label,
      assetClass: '资产桶',
      account: returnWindowLabel(windowKey),
      pnl,
      returnPct: sectionPeriodReturnRate(periodRows, pnl),
      weight: latestTotalValue > 0 ? latestValue / latestTotalValue : 0,
    };
  }).filter(item => Math.abs(item.pnl) > 0.005 || Math.abs(item.weight) > 0.0001);
  const sectionPnl = rows.reduce((sum, item) => sum + safeNum(item.pnl), 0);
  const residualPnl = safeNum(totalPnl) - sectionPnl;
  const residualThreshold = Math.max(1, Math.abs(safeNum(totalPnl)) * 0.002);
  const unattributed = Math.abs(residualPnl) > residualThreshold
    ? {
        key: 'unattributed_residual',
        label: residualPnl >= 0 ? '未归因正差额' : '未归因负差额',
        pnl: residualPnl,
        note: 'ESOP、现金/负债、分红/已实现收益或尚未纳入分区归因的数据差额',
      }
    : null;
  return {
    mode: 'period',
    windowKey,
    windowLabel: returnWindowLabel(windowKey),
    totalPnl: Number.isFinite(totalPnl) ? totalPnl : rows.reduce((sum, item) => sum + item.pnl, 0),
    winners: rows.filter(item => item.pnl > 0.005).sort((a, b) => b.pnl - a.pnl),
    losers: rows.filter(item => item.pnl < -0.005).sort((a, b) => a.pnl - b.pnl),
    unattributed,
  };
}

function renderReturnContributionPanel(model) {
  const el = document.getElementById('profitContributionPanel');
  if (!el) return;
  el.innerHTML = TopContributionPanel(model || {
    mode: 'period',
    windowLabel: '当前窗口',
    totalPnl: 0,
    winners: [],
    losers: [],
  });
}

function hideReturnEventPopover() {
  const el = document.getElementById('returnEventPopover');
  if (!el) return;
  el.hidden = true;
  el.innerHTML = '';
}

function showReturnEventPopover(event, point) {
  const el = document.getElementById('returnEventPopover');
  const wrap = el?.closest?.('.chartWrap');
  if (!el || !wrap || !event) return;
  const width = Math.min(280, Math.max(220, wrap.clientWidth - 16));
  const left = Math.min(Math.max(safeNum(point?.x) + 12, 8), Math.max(8, wrap.clientWidth - width - 8));
  const top = Math.min(Math.max(safeNum(point?.y) - 8, 8), Math.max(8, wrap.clientHeight - 116));
  el.style.left = `${left}px`;
  el.style.top = `${top}px`;
  el.style.maxWidth = `${width}px`;
  el.innerHTML = `
    <div class="returnEventPopoverHead">
      <strong>${esc(event.date)} · ${esc(event.label)}</strong>
      ${statusBadge('关键点', event.tone === 'bad' ? 'pending' : event.tone === 'warn' ? 'warn' : 'data')}
    </div>
    <div class="returnEventPopoverBody">${esc(returnEventSummary(event))}</div>
  `;
  el.hidden = false;
}

function renderReturnEventHotspots(chart, state) {
  if (!chart || !state) return;
  const layer = document.getElementById('returnEventHotspots');
  if (!layer) return;
  const eventDatasetIndex = chart.data.datasets.findIndex(dataset => dataset.returnSeriesKey === 'events');
  const eventDataset = eventDatasetIndex >= 0 ? chart.data.datasets[eventDatasetIndex] : null;
  const eventMeta = eventDatasetIndex >= 0 ? chart.getDatasetMeta(eventDatasetIndex) : null;
  const labels = chart.data.labels || [];
  const events = state.visibleEvents || [];
  if (!eventDataset || !events.length) {
    layer.innerHTML = '';
    return;
  }
  const eventByDate = new Map(events.map(event => [event.date, event]));
  const buttons = [];
  labels.forEach((date, index) => {
    const event = eventByDate.get(date);
    const value = eventDataset.data?.[index];
    if (!event || value == null || !Number.isFinite(Number(value))) return;
    const point = eventMeta?.data?.[index];
    const x = Number.isFinite(Number(point?.x)) ? point.x : chart.scales?.x?.getPixelForValue?.(index);
    const y = Number.isFinite(Number(point?.y)) ? point.y : chart.scales?.y?.getPixelForValue?.(value);
    if (!Number.isFinite(Number(x)) || !Number.isFinite(Number(y))) return;
    buttons.push(`
      <button
        type="button"
        class="returnEventHotspot ${esc(event.tone)}"
        data-return-event-date="${esc(event.date)}"
        style="left:${safeNum(x).toFixed(1)}px; top:${safeNum(y).toFixed(1)}px"
        aria-label="${esc(`${event.date} ${event.label}`)}"
      ></button>
    `);
  });
  layer.innerHTML = buttons.join('');
  layer.querySelectorAll('[data-return-event-date]').forEach((btn) => {
    btn.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      const item = eventByDate.get(btn.dataset.returnEventDate);
      showReturnEventPopover(item, {
        x: parseFloat(btn.style.left || '0'),
        y: parseFloat(btn.style.top || '0'),
      });
    });
  });
}

function bindReturnEventPopover(chart, state) {
  if (!chart || !state) return;
  chart.options.onClick = (nativeEvent) => {
    const elements = chart.getElementsAtEventForMode(
      nativeEvent,
      'nearest',
      { intersect: true },
      true,
    );
    const eventPoint = elements.find(item => chart.data.datasets[item.datasetIndex]?.returnSeriesKey === 'events');
    if (!eventPoint) {
      hideReturnEventPopover();
      return;
    }
    const date = chart.data.labels[eventPoint.index];
    const event = (state.visibleEvents || []).find(item => item.date === date);
    showReturnEventPopover(event, eventPoint.element);
  };
}

function applyInvestmentReturnWindow(chart, state, nextWindowKey, persist = true) {
  if (!chart || !state) return;
  state.activeWindow = normalizeReturnWindowKey(nextWindowKey);
  if (persist) {
    writeReturnWindowPreference(state.activeWindow);
  }
  const windowData = buildInvestmentReturnWindowData(
    state.rows,
    state.sectionRowsByKey,
    state.sectionSpecs,
    state.activeWindow,
    state.isMobile,
    state.events,
  );
  state.visibleRows = windowData.rows;
  state.visibleEvents = windowData.visibleEvents;
  state.topContribution = windowData.topContribution;
  state.pnlMap = windowData.pnlMap;
  state.valueMap = windowData.valueMap;
  chart.data.labels = windowData.labels;
  chart.data.datasets.forEach(dataset => {
    if (dataset.returnSeriesKey === 'events') {
      dataset.data = windowData.eventData;
      dataset.pointRadius = windowData.eventPointRadius;
      return;
    }
    if (dataset.returnSeriesKey === 'total') {
      dataset.data = windowData.totalData;
      dataset.pointRadius = windowData.totalPointRadius;
      return;
    }
    const sectionData = windowData.sectionDataByKey.get(dataset.returnSeriesKey);
    if (sectionData) {
      dataset.data = sectionData.data;
      dataset.pointRadius = sectionData.pointRadius;
    }
  });
  chart.update('none');
  hideReturnEventPopover();
  renderReturnEventHotspots(chart, state);
  updateInvestmentNavChart(state);
  renderReturnContributionPanel(windowData.topContribution);
}

function renderInvestmentReturnWindowToggle(chart, state) {
  const el = document.getElementById('investmentReturnWindowToggle');
  if (!el || !chart || !state) return;
  const activeWindow = normalizeReturnWindowKey(state.activeWindow);
  el.innerHTML = RETURN_WINDOW_OPTIONS.map(item => `
    <button type="button" data-return-window="${esc(item.key)}" class="${item.key === activeWindow ? 'active' : ''}" aria-pressed="${item.key === activeWindow ? 'true' : 'false'}">
      ${esc(item.label)}
    </button>
  `).join('');
  el.querySelectorAll('[data-return-window]').forEach(btn => {
    btn.addEventListener('click', () => {
      const key = normalizeReturnWindowKey(btn.dataset.returnWindow);
      applyInvestmentReturnWindow(chart, state, key);
      el.querySelectorAll('[data-return-window]').forEach(item => {
        const active = item.dataset.returnWindow === key;
        item.classList.toggle('active', active);
        item.setAttribute('aria-pressed', active ? 'true' : 'false');
      });
    });
  });
}

function renderReturnNotes(section, containerId) {
  const el = document.getElementById(containerId);
  if (!el) return;
  const entries = section?.entries || [];
  if (!entries.length) {
    el.innerHTML = '';
    return;
  }

  const total = section.total_ytd_pnl ?? entries.reduce((sum, item) => sum + safeNum(item.ytd_pnl), 0);
  el.innerHTML = `
    <div class="sectionTitle" style="margin-top:18px">
      <h3>${esc(section.label || '收益明细')}</h3>
      <div class="muted">合计：<span style="color:${safeNum(total) >= 0 ? 'var(--good)' : 'var(--bad)'}">${fmtSignedCny(total)}</span></div>
    </div>
    <div class="muted" style="margin-top:6px">${esc(section.summary_note || '')}</div>
    <div class="tableWrap" style="margin-top:10px">
      <table class="tableWide">
        <thead>
          <tr>
            <th style="width:260px">名称</th>
            <th class="nowrap num" style="width:120px">2026收益</th>
            <th style="width:360px">口径</th>
          </tr>
        </thead>
        <tbody>
          ${entries.map((item) => {
            const pnl = safeNum(item.ytd_pnl);
            const period = item.first_buy && item.last_buy ? `${item.first_buy} 至 ${item.last_buy}` : '';
            const basis = [period, item.basis].filter(Boolean).join('；');
            return `
              <tr>
                <td class="name">${esc(item.name || '')}</td>
                <td class="mono num" style="color:${pnl >= 0 ? 'var(--good)' : 'var(--bad)'}">${fmtSignedCny(pnl)}</td>
                <td>${esc(basis || '-')}</td>
              </tr>
            `;
          }).join('')}
        </tbody>
      </table>
    </div>
  `;
}

function reportedEntryKey(entry) {
  return entry?.symbol ? `symbol:${entry.symbol}` : `name:${entry?.name || ''}`;
}

function findReportedEntry(row, section) {
  const entries = section?.entries || [];
  const rowSymbol = String(row.symbol || '');
  const rowName = String(row.name || '');
  return entries.find((entry) => (
    (entry.symbol && String(entry.symbol) === rowSymbol) ||
    (!entry.symbol && String(entry.name || '') === rowName)
  ));
}

function withReportedPnlRows(rows, section) {
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

function withEsopDisplayRows(rows) {
  return (rows || []).map((row) => ({
    ...row,
    name: '内部股份分红',
  }));
}

function applyReportedReturnTotal(summary, rows, section) {
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

function fmtSignedPctFromRate(rate) {
  const pct = safeNum(rate) * 100;
  return (pct >= 0 ? '+' : '') + pct.toFixed(2) + '%';
}

function latestSectionReturnAmount(row) {
  if (!row) return 0;
  if (row.total_pnl != null) return safeNum(row.total_pnl);
  return safeNum(row.market_value) - safeNum(row.cumulative_capital);
}

function sectionTodayReturnAmount(resp) {
  if (resp?.today_window?.total_return != null) {
    return safeNum(resp.today_window.total_return);
  }
  const rows = Array.isArray(resp?.rows) ? resp.rows : [];
  if (!rows.length) return null;
  const latest = rows[rows.length - 1];
  const previous = rows.length > 1 ? rows[rows.length - 2] : null;
  return latestSectionReturnAmount(latest) - latestSectionReturnAmount(previous);
}

function setKpiFxNote(valueId, fxDailyPnl) {
  const valueEl = document.getElementById(valueId);
  if (!valueEl) return;
  const parent = valueEl.parentElement;
  if (!parent) return;
  let note = parent.querySelector('.kpiFxNote');
  if (!note) {
    note = document.createElement('div');
    note.className = 'kpiFxNote muted';
    parent.appendChild(note);
  }
  const fx = safeNum(fxDailyPnl);
  note.textContent = Math.abs(fx) > 0.005 ? `汇率影响 ${fmtSignedCny(fx)}` : '';
  note.style.color = fx >= 0 ? 'var(--good)' : 'var(--bad)';
}

function applySectionReturnMetrics(resp, ids, options = {}) {
  if (!resp?.summary) return null;
  const totalReturn = safeNum(resp.summary.total_return);
  const totalReturnRate = safeNum(resp.summary.total_return_rate);
  const annualizedRate = safeNum(resp.summary.annualized_return_rate);
  const todayReturn = Number.isFinite(Number(options.dailyPnl))
    ? safeNum(options.dailyPnl)
    : (sectionTodayReturnAmount(resp) ?? 0);
  const fxDailyPnl = safeNum(options.fxDailyPnl);

  setKpiLabel(ids.totalPnlId, '总收益');
  setKpiLabel(ids.dailyPnlId, '今日收益');
  setKpiLabel(ids.returnId, '总收益率');

  const totalEl = document.getElementById(ids.totalPnlId);
  const todayEl = document.getElementById(ids.dailyPnlId);
  const rateEl = document.getElementById(ids.returnId);
  if (totalEl) {
    totalEl.textContent = fmtSignedCny(totalReturn);
    totalEl.style.color = totalReturn >= 0 ? 'var(--good)' : 'var(--bad)';
  }
  if (todayEl) {
    todayEl.textContent = fmtSignedCny(todayReturn);
    todayEl.style.color = todayReturn >= 0 ? 'var(--good)' : 'var(--bad)';
  }
  setKpiFxNote(ids.dailyPnlId, fxDailyPnl);
  if (rateEl) {
    rateEl.textContent = fmtSignedPctFromRate(totalReturnRate);
    rateEl.style.color = totalReturnRate >= 0 ? 'var(--good)' : 'var(--bad)';
  }
  return { totalReturn, totalReturnRate, annualizedRate, todayReturn, fxDailyPnl };
}

function annualizedRateFromPeriod(rate, startDate, endDate) {
  const start = new Date(`${startDate || '2026-01-01'}T00:00:00`);
  const end = new Date(`${endDate || '2026-05-31'}T00:00:00`);
  const days = Math.max((end - start) / 86400000, 1);
  if (rate <= -1) return 0;
  return Math.pow(1 + rate, 365 / days) - 1;
}

function applyStaticReturnMetrics(summary, ids, options = {}) {
  if (!summary) return null;
  const totalReturn = safeNum(summary.totalPnl);
  const denominator = safeNum(summary.totalCost) || Math.max(safeNum(summary.totalValue), 0);
  const totalReturnRate = denominator > 0 ? totalReturn / denominator : 0;
  const annualizedRate = options.oncePerYear
    ? totalReturnRate
    : annualizedRateFromPeriod(
      totalReturnRate,
      options.startDate || '2026-01-01',
      options.endDate || '2026-05-31',
    );

  setKpiLabel(ids.totalPnlId, '总收益');
  setKpiLabel(ids.dailyPnlId, '今日收益');
  setKpiLabel(ids.returnId, '总收益率');

  const totalEl = document.getElementById(ids.totalPnlId);
  const todayEl = document.getElementById(ids.dailyPnlId);
  const rateEl = document.getElementById(ids.returnId);
  if (totalEl) {
    totalEl.textContent = fmtSignedCny(totalReturn);
    totalEl.style.color = totalReturn >= 0 ? 'var(--good)' : 'var(--bad)';
  }
  if (todayEl) {
    todayEl.textContent = fmtSignedCny(safeNum(summary.dailyPnl));
    todayEl.style.color = safeNum(summary.dailyPnl) >= 0 ? 'var(--good)' : 'var(--bad)';
  }
  if (rateEl) {
    rateEl.textContent = fmtSignedPctFromRate(totalReturnRate);
    rateEl.style.color = totalReturnRate >= 0 ? 'var(--good)' : 'var(--bad)';
  }
  return { totalReturn, totalReturnRate, annualizedRate, todayReturn: safeNum(summary.dailyPnl) };
}

function renderReturnMethodBar() {
  const el = document.getElementById('returnMethodBar');
  if (!el) return;
  el.innerHTML = RETURN_METHODS.map(ReturnMethodBadge).join('');
}

function renderInvestmentReturnToggle(chart, sectionSpecs, state = null) {
  const el = document.getElementById('investmentReturnToggle');
  if (!el || !chart) return;
  el.setAttribute('aria-multiselectable', 'true');
  const options = [
    { key: 'total', label: '总收益率', datasetLabel: '总收益率' },
    ...sectionSpecs.map(spec => ({ key: spec.key, label: spec.label, datasetLabel: spec.label })),
  ];
  const syncButtons = () => {
    el.querySelectorAll('[data-return-series]').forEach(btn => {
      const item = options.find(option => option.key === btn.dataset.returnSeries);
      const dataset = chart.data.datasets.find(row => row.label === item?.datasetLabel);
      const active = dataset ? !dataset.hidden : false;
      btn.classList.toggle('active', active);
      btn.disabled = false;
      btn.title = '';
      btn.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
  };
  const toggleSeries = (mode) => {
    const selected = options.find(item => item.key === mode) || options[0];
    const dataset = chart.data.datasets.find(row => row.label === selected.datasetLabel);
    if (!dataset) return;
    const visibleCount = chart.data.datasets.filter(row => row.returnSeriesKey !== 'events' && !row.hidden).length;
    if (!dataset.hidden && visibleCount <= 1) return;
    dataset.hidden = !dataset.hidden;
    syncButtons();
    chart.update('none');
    if (state) renderReturnEventHotspots(chart, state);
  };
  el.innerHTML = options.map((item, index) => {
    return `
    <button type="button" data-return-series="${esc(item.key)}" class="${index === 0 ? 'active' : ''}" aria-pressed="${index === 0 ? 'true' : 'false'}">
      ${esc(item.label)}
    </button>
  `;
  }).join('');
  el.querySelectorAll('[data-return-series]').forEach(btn => {
    btn.addEventListener('click', () => toggleSeries(btn.dataset.returnSeries));
  });
  chart.data.datasets.forEach((dataset) => {
    dataset.hidden = dataset.returnSeriesKey === 'events' ? false : dataset.label !== '总收益率';
  });
  syncButtons();
  chart.update('none');
  if (state) renderReturnEventHotspots(chart, state);
}

function categoryReturnOverride(sectionPayload) {
  if (!sectionPayload?.summary) return null;
  return {
    totalPnl: safeNum(sectionPayload.summary.total_return),
    totalReturnRate: safeNum(sectionPayload.summary.total_return_rate),
    annualizedReturnRate: safeNum(sectionPayload.summary.annualized_return_rate),
    dailyPnl: sectionTodayReturnAmount(sectionPayload),
    method: sectionPayload.method,
  };
}

function categoryMetric(label, base, sectionPayload) {
  const override = categoryReturnOverride(sectionPayload);
  const totalPnl = override ? override.totalPnl : safeNum(base.totalPnl);
  const totalValue = safeNum(base.totalValue);
  const totalCost = safeNum(base.totalCost) || Math.max(totalValue - totalPnl, 0);
  const explicitTotalReturnRate = Number.isFinite(Number(base.totalReturnRate))
    ? safeNum(base.totalReturnRate)
    : null;
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
    totalReturnRate: override ? override.totalReturnRate : (explicitTotalReturnRate ?? (totalCost > 0 ? totalPnl / totalCost : 0)),
    annualizedReturnRate: override ? override.annualizedReturnRate : null,
  };
}

function buildContributionRows(groups, summary) {
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

function publishHoldingsKpiSummary(summary, categories) {
  const categoryRows = (categories || []).map((category) => ({
    label: category.label,
    totalValue: safeNum(category.totalValue),
    totalPnl: safeNum(category.totalPnl),
    dailyPnl: safeNum(category.dailyPnl),
    priceDailyPnl: safeNum(category.priceDailyPnl ?? category.dailyPnl),
    fxDailyPnl: safeNum(category.fxDailyPnl),
    dailyTotalPnl: safeNum(category.dailyTotalPnl ?? category.dailyPnl),
  }));
  const authoritative = {
    source: 'holdings_page',
    marketValue: safeNum(summary.marketValue),
    totalPnl: safeNum(summary.totalPnl),
    currentFloatPnl: safeNum(summary.currentFloatPnl),
    dailyPnl: safeNum(summary.dailyPnl),
    dailyPricePnl: categoryRows
      .filter((row) => !isInternalInvestmentLabel(row.label))
      .reduce((sum, row) => sum + row.priceDailyPnl, 0),
    dailyFxPnl: categoryRows
      .filter((row) => !isInternalInvestmentLabel(row.label))
      .reduce((sum, row) => sum + row.fxDailyPnl, 0),
    twr: safeNum(summary.twr),
    asOf: summary.asOf,
    totalLiabilities: safeNum(summary.totalLiabilities),
    cashActiveValue: safeNum(summary.cashActiveValue),
    cashActiveAssets: safeNum(summary.cashActiveAssets),
    cashActiveLiabilities: safeNum(summary.cashActiveLiabilities),
    categories: categoryRows,
  };
  window.__pamsHoldingsKpiSummary = authoritative;
  window.dispatchEvent(new CustomEvent('pams:holdings-kpi-ready', { detail: authoritative }));
}

function buildInvestmentConclusion(summary, categories, issues) {
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

function deriveDataQualityIssues({ reportedReturns, staleReportedReturns = false, ibkrPerf, esopRows, fundRows, preciousRows }) {
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

function renderDataQualityPanel(issues, asOf) {
  const el = document.getElementById('investmentDataQualityPanel');
  if (!el) return;
  const rows = [
    { title: '高可信', desc: 'A股、美股等有代码、数量、价格和接口同步记录的资产。', kind: 'ok' },
    { title: '中可信', desc: '基金、理财通和贵金属包含净值或平台收益中心口径，需要和账户核准交叉确认。', kind: 'warn' },
    { title: '待确认', desc: '内部股份、截图收益、手工估值、缺少份额或缺少汇率收益拆分的资产。', kind: 'pending' },
  ];
  el.innerHTML = `
    <div class="qualitySummary">
      <div>${statusBadge('当前评分：中', 'warn')}</div>
      <div class="muted">截至 ${esc(asOf || '待确认')}，投资页优先展示已确认金额；缺字段处不做精确假设。</div>
    </div>
    <div class="qualityGrid">
      ${rows.map(row => `
        <div class="qualityCard">
          ${statusBadge(row.title, row.kind)}
          <p>${esc(row.desc)}</p>
        </div>
      `).join('')}
    </div>
    <div class="issueList">
      ${issues.map(issue => `<div class="issueRow">${statusBadge('需核准', 'pending')}<span>${esc(issue)}</span></div>`).join('')}
    </div>
  `;
}

function renderInvestmentDashboard({
  summary,
  categories,
  contributionRows,
  dataIssues,
  asOf,
  syncAt,
  mobileProfiles,
}) {
  const summaryEl = document.getElementById('investmentSummaryHeader');
  if (summaryEl) summaryEl.innerHTML = InvestmentSummaryHeaderCompact(summary);

  mobileHeroContext = {
    profiles: mobileProfiles || { '全部': { value: summary.marketValue, dailyPnl: summary.dailyPnl, totalPnl: summary.totalPnl, currentFloatPnl: summary.currentFloatPnl, totalRate: summary.twr, share: 1 } },
    dataIssues,
    asOf,
    syncAt,
  };
  updateMobilePortfolioHero();

  const conclusionEl = document.getElementById('investmentConclusionText');
  if (conclusionEl) {
    conclusionEl.classList.remove('skeletonLine');
    conclusionEl.innerHTML = buildInvestmentConclusion(summary, categories, dataIssues);
  }

  const bannerEl = document.getElementById('dataQualityBanner');
  if (bannerEl) {
    const banner = DataQualityBanner(dataIssues);
    bannerEl.style.display = banner ? 'flex' : 'none';
    bannerEl.innerHTML = banner;
  }

  const freshnessEl = document.getElementById('investmentDataFreshness');
  if (freshnessEl) {
    freshnessEl.textContent = `更新 ${formatSyncTimestamp(syncAt, asOf)}`;
    freshnessEl.className = 'statusBadge statusBadge-data';
  }

  void contributionRows;

  const allocationEl = document.getElementById('assetAllocationPanel');
  if (allocationEl) allocationEl.innerHTML = AssetAllocationPanel(categories, summary.marketValue);

  if (document.getElementById('investmentDataQualityPanel')) {
    renderDataQualityPanel(dataIssues, asOf);
  }
}

function AssetAllocationPanel(categories, totalValue) {
  const rows = (categories || [])
    .filter(c => safeNum(c.totalValue) > 0)
    .sort((a, b) => safeNum(b.totalValue) - safeNum(a.totalValue));
  return rows.map(row => {
    const ratio = totalValue > 0 ? safeNum(row.totalValue) / totalValue : 0;
    const dailyPnl = safeNum(row.dailyPnl);
    const totalPnl = safeNum(row.totalPnl);
    return `<div class="allocationRow">
      <div class="allocationMeta">
        <strong>${esc(row.label)}</strong>
        <span>${formatCompactCurrency(row.totalValue)} · ${formatPercentRate(ratio)}</span>
      </div>
      <div class="allocationTrack" aria-hidden="true"><span style="width:${Math.min(100, ratio * 100)}%"></span></div>
      <div class="allocationNumbers">
        <span class="${dailyPnl >= 0 ? 'up' : 'down'}">${fmtSignedCny(dailyPnl)}</span>
        <small class="${totalPnl >= 0 ? 'up' : 'down'}">${fmtSignedCny(totalPnl)}</small>
      </div>
    </div>`;
  }).join('') || '<div class="muted">暂无分布数据</div>';
}

function bindContributionTabs() {
  document.addEventListener('click', (event) => {
    const btn = event.target?.closest?.('[data-contribution-tab]');
    if (!btn) return;
    const root = btn.closest('.contributionTabs');
    if (!root) return;
    const key = btn.dataset.contributionTab;
    root.querySelectorAll('[data-contribution-tab]').forEach(el => el.classList.toggle('active', el === btn));
    root.querySelectorAll('[data-contribution-pane]').forEach(el => { el.hidden = el.dataset.contributionPane !== key; });
  });
}

function bindInvestmentPageTabs() {
  const links = Array.from(document.querySelectorAll('[data-invest-tab]'));
  const panels = Array.from(document.querySelectorAll('[data-invest-panel]'));
  if (!links.length || !panels.length) return;

  const media = window.matchMedia('(max-width: 760px)');
  let activeTab = 'holdings';

  const tabFromHash = (hash) => {
    const key = String(hash || '').replace(/^#/, '');
    if (
      key === 'return-trend' ||
      key === 'category-details' ||
      key === 'return-attribution' ||
      key === 'trade-attribution-analysis' ||
      key === 'asset-master-panel' ||
      CATEGORY_SECTION_IDS.includes(key)
    ) return 'analysis';
    if (key === 'investment-ledger') return 'transactions';
    if (key === 'portfolio-risk-disclosure' || key === 'lookthrough-exposure') return 'risk';
    if (key === 'portfolio-constraints') return 'allocation';
    return 'holdings';
  };

  const activeCategoryTarget = () => (
    document.querySelector('[data-category-target].active')?.dataset.categoryTarget || CATEGORY_SECTION_IDS[0]
  );

  const syncCategoryVisibility = () => {
    const target = activeCategoryTarget();
    for (const id of CATEGORY_SECTION_IDS) {
      const el = document.getElementById(id);
      if (el) el.hidden = media.matches ? (activeTab !== 'analysis' || id !== target) : id !== target;
    }
  };

  const setActive = (nextTab, options = {}) => {
    activeTab = nextTab || 'holdings';
    document.body.dataset.investTab = activeTab;
    for (const link of links) {
      const active = link.dataset.investTab === activeTab;
      link.classList.toggle('is-active', active);
      if (active) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    }

    for (const panel of panels) {
      if (panel.classList.contains('investmentCategorySection')) continue;
      panel.hidden = media.matches ? panel.dataset.investPanel !== activeTab : false;
    }
    syncCategoryVisibility();

    if (options.updateHash) {
      const href = links.find(link => link.dataset.investTab === activeTab)?.getAttribute('href');
      if (href) history.replaceState(null, '', href);
    }
  };

  for (const link of links) {
    link.addEventListener('click', (event) => {
      if (!media.matches) return;
      event.preventDefault();
      setActive(link.dataset.investTab, { updateHash: true });
    });
  }

  window.addEventListener('hashchange', () => {
    const key = String(window.location.hash || '').replace(/^#/, '');
    if (key === 'category-details' || CATEGORY_SECTION_IDS.includes(key)) openCategoryDetailShell();
    setActive(tabFromHash(window.location.hash));
  });
  media.addEventListener?.('change', () => setActive(activeTab));
  document.addEventListener('click', (event) => {
    if (event.target?.closest?.('[data-category-target]')) {
      window.setTimeout(syncCategoryVisibility, 0);
    }
  });

  setActive(tabFromHash(window.location.hash));
}

function runWhenInvestmentAnalysisVisible(callback, options = {}) {
  const media = window.matchMedia('(max-width: 760px)');
  let started = false;
  const delayMs = Number(options.delayMs || 0);
  const start = () => {
    if (started) return;
    started = true;
    setTimeout(() => callback(), delayMs);
  };
  const maybeStart = () => {
    if (started) return;
    if (!media.matches || document.body.dataset.investTab === 'analysis') start();
  };

  window.addEventListener('hashchange', maybeStart);
  media.addEventListener?.('change', maybeStart);
  if (typeof MutationObserver === 'function') {
    new MutationObserver(maybeStart).observe(document.body, {
      attributes: true,
      attributeFilter: ['data-invest-tab'],
    });
  }
  setTimeout(maybeStart, media.matches ? 0 : 1200);
}

const CATEGORY_RETURN_CHART_SPECS = [
  {
    targetId: 'section-a-stock-equity',
    chartId: 'chartAStockEquity',
    url: '/investment/section-return-series?section=a_stock&start=2026-01-01&interval=daily&baseCurrency=CNY',
    color: '#f85149',
    title: 'A股个股收益率',
  },
  {
    targetId: 'section-a-fund',
    chartId: 'chartAFund',
    url: '/investment/section-return-series?section=a_fund&start=2026-01-01&interval=daily&baseCurrency=CNY',
    color: '#d29922',
    title: '基金收益率',
  },
  {
    targetId: 'section-us-stock',
    chartId: 'chartUSStock',
    url: '/investment/section-return-series?section=us_stock&start=2026-01-01&interval=daily&baseCurrency=CNY',
    color: '#58a6ff',
    title: '美股收益率',
  },
  {
    targetId: 'section-precious-metal',
    chartId: 'chartPreciousMetal',
    url: '/investment/section-return-series?section=precious_metal&start=2026-01-01&interval=daily&baseCurrency=CNY',
    color: '#a371f7',
    title: '贵金属收益率',
  },
];
const loadedCategoryReturnCharts = new Set();

function canLoadAnalysisDetail() {
  return document.body.dataset.investTab === 'analysis';
}

function isInvestmentAnalysisHash(hash) {
  const key = String(hash || '').replace(/^#/, '');
  return (
    key === 'return-trend' ||
    key === 'return-attribution' ||
    key === 'trade-attribution-analysis' ||
    key === 'asset-master-panel'
  );
}

function loadCategoryReturnChart(spec) {
  if (!spec || loadedCategoryReturnCharts.has(spec.chartId) || !canLoadAnalysisDetail()) return;
  loadedCategoryReturnCharts.add(spec.chartId);
  createReturnRateChart(spec.chartId, spec.url, spec.color, spec.title, 'daily').catch(() => {
    loadedCategoryReturnCharts.delete(spec.chartId);
  });
}

function categoryDetailShell() {
  return document.getElementById('category-detail-shell');
}

function isCategoryDetailOpen() {
  const shell = categoryDetailShell();
  return Boolean(shell && !shell.hidden && shell.open);
}

function openCategoryDetailShell() {
  const shell = categoryDetailShell();
  if (shell && !shell.hidden) shell.open = true;
}

function loadVisibleCategoryReturnChart() {
  if (!canLoadAnalysisDetail() || !isCategoryDetailOpen()) return;
  const spec = CATEGORY_RETURN_CHART_SPECS.find(item => {
    const section = document.getElementById(item.targetId);
    return section && section.hidden === false;
  }) || CATEGORY_RETURN_CHART_SPECS[0];
  loadCategoryReturnChart(spec);
}

function loadAllCategoryReturnCharts() {
  if (!canLoadAnalysisDetail() || !isCategoryDetailOpen()) return;
  CATEGORY_RETURN_CHART_SPECS.forEach(loadCategoryReturnChart);
}

function queueVisibleCategoryReturnChart(delayMs = 0) {
  window.setTimeout(loadVisibleCategoryReturnChart, delayMs);
}

let investmentReturnChartPromise = null;
let analysisHeavyLoadersStarted = false;

function loadInvestmentReturnChartOnce() {
  if (!investmentReturnChartPromise) {
    investmentReturnChartPromise = renderInvestmentReturnChart().catch((error) => {
      investmentReturnChartPromise = null;
      document.body.dataset.investmentReturnChartError = String(error);
      console.error('收益曲线加载失败', error);
      throw error;
    });
  }
  return investmentReturnChartPromise;
}

function setupAnalysisHeavyLoaders() {
  if (analysisHeavyLoadersStarted) return;
  analysisHeavyLoadersStarted = true;
  document.body.dataset.analysisHeavyLoadersStarted = '1';
  const panel = document.querySelector('.returnChartPanel');
  const chartWrap = panel?.querySelector('.chartWrap');
  const toggle = document.getElementById('investmentReturnToggle');
  if (chartWrap) chartWrap.hidden = false;
  if (toggle) toggle.hidden = false;
  window.setTimeout(() => loadInvestmentReturnChartOnce().catch(() => null), 300);
  window.setTimeout(loadVisibleCategoryReturnChart, 900);
}

function CategoryDetailTabs() {
  const buttons = Array.from(document.querySelectorAll('[data-category-target]'));
  const shell = categoryDetailShell();
  if (shell?.hidden) return;
  if (shell) {
    shell.addEventListener('toggle', () => {
      if (shell.open) queueVisibleCategoryReturnChart(120);
    });
  }
  if (!buttons.length) return;
  const show = (targetId, options = {}) => {
    if (options.openShell) openCategoryDetailShell();
    for (const id of CATEGORY_SECTION_IDS) {
      const el = document.getElementById(id);
      if (el) el.hidden = id !== targetId;
    }
    for (const btn of buttons) {
      btn.classList.toggle('active', btn.dataset.categoryTarget === targetId);
    }
    queueVisibleCategoryReturnChart(150);
  };
  const hashTarget = window.location.hash ? window.location.hash.slice(1) : '';
  const shouldOpenShell = hashTarget === 'category-details' || CATEGORY_SECTION_IDS.includes(hashTarget);
  if (shouldOpenShell) {
    openCategoryDetailShell();
  }
  const initial = CATEGORY_SECTION_IDS.includes(hashTarget) ? hashTarget : CATEGORY_SECTION_IDS[0];
  show(initial);
  window.addEventListener('hashchange', () => {
    const key = window.location.hash ? window.location.hash.slice(1) : '';
    if (key === 'category-details') openCategoryDetailShell();
    if (CATEGORY_SECTION_IDS.includes(key)) show(key, { openShell: true });
  });
  for (const btn of buttons) {
    btn.addEventListener('click', () => {
      show(btn.dataset.categoryTarget, { openShell: true });
      const anchor = document.getElementById('category-details');
      if (anchor) anchor.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  }
}

function promoteHoldingsTable() {
  if (window.matchMedia('(max-width: 760px)').matches) return;
  const hero = document.getElementById('investment-conclusion');
  const holdings = document.getElementById('holdings-smart-table');
  if (hero && holdings && hero.nextElementSibling !== holdings) {
    hero.after(holdings);
  }
}

async function main(){
  setActiveNav();
  initClosedToggle();
  renderReturnMethodBar();
  CategoryDetailTabs();
  bindContributionTabs();
  bindInvestmentPageTabs();
  promoteHoldingsTable();

  try {
    const [
      aStockEquity,
      aFund,
      aFundAll,
      usStock,
      preciousMetal,
      esop,
      latestHoldings,
      accountOverview,
      fxData,
      ibkrPerf,
      aStockPerf,
      reportedReturns,
      portfolioSummary,
      portfolioValuationState,
    ] = await Promise.all([
      jgetFresh('/holdings/by-category/A股?minor_category=A股&limit=500&include_closed=true'),
      jgetFresh(withClosedParam('/holdings/by-category/A股?minor_category=A股基金&limit=500')),
      jgetFresh('/holdings/by-category/A股?minor_category=A股基金&limit=500&include_closed=true'),
      jgetFresh(withClosedParam('/holdings/by-category/美股?limit=500')),
      jgetFresh(withClosedParam('/holdings/by-category/贵金属?limit=500')),
      jgetFresh(withClosedParam('/holdings/by-category/ESOP?limit=500')),
      jgetFresh('/holdings/latest?limit=500').catch(() => null),
      jgetFresh('/api/accounts/overview').catch(() => null),
      jgetFresh('/api/fx-rates/latest?base_currency=CNY'),
      jgetFresh('/api/ibkr/performance').catch(() => null),
      jgetFresh('/api/account-performance/tonghuashun').catch(() => null),
      jgetFresh('/api/investment-returns/reported').catch(() => null),
      jgetFresh('/summary2/latest?baseCurrency=CNY').catch(() => null),
      jgetFresh('/api/portfolio/valuation-state?baseCurrency=CNY').catch(() => null),
    ]);
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
    const fundActiveRows = fundRows.filter((row) => !row.is_closed);
    const fundSummaryRows = withReportedPnlRows(aFundAll.rows || aFund.rows || [], fundReport);
    const esopRows = withEsopDisplayRows(esop.rows || []);

    const aStockRowsSummary = renderSection(aStockEquityRows, renderRowStock, 'rowsAStockEquity', 'countAStockEquity', 'totalValueAStockEquity', 'totalPnlAStockEquity', 'dailyPnlAStockEquity', 'countKpiAStockEquity', 'holdingPnlAStockEquity', fxRates);
    const aStockAccountSummary = renderAStockOwnerPerformance(aStockPerf);
    const aStockSummary = aStockAccountSummary
      ? {
        ...aStockRowsSummary,
        ...aStockAccountSummary,
        dailyPnl: Number.isFinite(Number(aStockAccountSummary.dailyPnl))
          ? aStockAccountSummary.dailyPnl
          : aStockRowsSummary.dailyPnl,
        priceDailyPnl: aStockRowsSummary.priceDailyPnl,
        fxDailyPnl: aStockRowsSummary.fxDailyPnl,
        dailyTotalPnl: Number.isFinite(Number(aStockAccountSummary.dailyPnl))
          ? aStockAccountSummary.dailyPnl
          : aStockRowsSummary.dailyTotalPnl,
        positionValue: aStockRowsSummary.totalValue,
        cashValue: Math.max(safeNum(aStockAccountSummary.totalValue) - safeNum(aStockRowsSummary.totalValue), 0),
      }
      : aStockRowsSummary;
    const fundDisplaySummary = renderSection(fundRows, renderRowStock, 'rowsAFund', 'countAFund', 'totalValueAFund', 'totalPnlAFund', 'dailyPnlAFund', 'countKpiAFund', 'holdingPnlAFund', fxRates);
    const fundActiveSummary = summarizeRows(fundActiveRows);
    const fundSummaryBase = applyReportedReturnTotal(summarizeRows(fundSummaryRows), fundSummaryRows, fundReport);
    const fundSummary = {
      ...fundSummaryBase,
      totalReturnRate: returnRateFromCapitalBase(fundSummaryBase, fundActiveSummary),
    };
    setKpiLabel('totalPnlAFund', '投资收益');
    writeSectionFinancials({ ...fundSummary, totalValue: fundDisplaySummary.totalValue }, 'totalValueAFund', 'totalPnlAFund', 'dailyPnlAFund', 'holdingPnlAFund');
    const usSummary = renderSection(usStock.rows || [], renderRowStock, 'rowsUSStock', 'countUSStock', 'totalValueUSStock', 'totalPnlUSStock', 'dailyPnlUSStock', 'countKpiUSStock', 'holdingPnlUSStock', fxRates);
    const ibkrSummary = renderIbkrPerformance(ibkrPerf, usStock.as_of);
    const preciousSummary = renderSection(preciousMetal.rows || [], renderRowFundLike, 'rowsPreciousMetal', 'countPreciousMetal', 'totalValuePreciousMetal', 'totalPnlPreciousMetal', 'dailyPnlPreciousMetal', 'countKpiPreciousMetal', 'holdingPnlPreciousMetal', fxRates);
    const esopSummary = renderSection(esopRows, renderRowFundLike, 'rowsESOP', 'countESOP', 'totalValueESOP', 'totalPnlESOP', 'dailyPnlESOP', 'countKpiESOP', 'holdingPnlESOP', fxRates);
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
    const useIbkrAccountTotals = Boolean(ibkrSummary && ibkrFlexCoversPositionDate);
    const ibkrUsesNetLiquidation = Boolean(
      useIbkrAccountTotals || ibkrHoldingsCoversPositionDate || ibkrOverviewCoversPositionDate
    );
    const ibkrHasFlexCashValue = useIbkrAccountTotals && Number.isFinite(Number(ibkrSummary?.cashValue));
    const ibkrPositionValue = ibkrHoldingsCoversPositionDate
      ? safeNum(ibkrHoldingsNet.grossAssets)
      : safeNum(usSummary.totalValue);
    const ibkrCashValue = ibkrHasFlexCashValue
      ? safeNum(ibkrSummary.cashValue)
      : (ibkrHoldingsCoversPositionDate ? safeNum(ibkrHoldingsNet.liabilities) : 0);
    const ibkrAccountValue = useIbkrAccountTotals
      ? (ibkrHasFlexCashValue ? ibkrPositionValue + ibkrCashValue : safeNum(ibkrSummary?.totalValue))
      : (ibkrHoldingsCoversPositionDate
        ? safeNum(ibkrHoldingsNet.netValue)
        : (ibkrOverviewCoversPositionDate ? safeNum(ibkrOverviewBalance.amountCny) : usSummary.totalValue));
    const ibkrNetDeposit = Number.isFinite(Number(ibkrSummary?.totalCost)) && safeNum(ibkrSummary?.totalCost) > 0
      ? safeNum(ibkrSummary?.totalCost)
      : safeNum(usSummary.totalCost);
    const ibkrDerivedTotalPnl = ibkrNetDeposit > 0 ? ibkrAccountValue - ibkrNetDeposit : safeNum(ibkrSummary?.totalPnl);
    const usCategorySummary = (ibkrSummary || ibkrUsesNetLiquidation)
      ? {
        ...usSummary,
        totalValue: ibkrUsesNetLiquidation ? ibkrAccountValue : usSummary.totalValue,
        totalCost: ibkrUsesNetLiquidation ? (ibkrNetDeposit || usSummary.totalCost) : usSummary.totalCost,
        totalPnl: ibkrUsesNetLiquidation ? ibkrDerivedTotalPnl : usSummary.totalPnl,
        totalReturnRate: ibkrUsesNetLiquidation && ibkrNetDeposit > 0 ? ibkrDerivedTotalPnl / ibkrNetDeposit : usSummary.totalReturnRate,
        positionValue: ibkrPositionValue,
        cashValue: ibkrCashValue,
        accountAsOf: ibkrHoldingsNet?.asOf || ibkrSummary?.asOf,
        accountValueAsOf: ibkrHoldingsNet?.asOf || ibkrOverviewBalance?.asOf || ibkrSummary?.asOf,
        accountValueSource: ibkrHoldingsCoversPositionDate
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
    if (ibkrUsesNetLiquidation && !useIbkrAccountTotals) {
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
      color: '#8b949e',
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
      { label: 'A股个股', color: '#f85149', ...aStockSummary },
      { label: '基金', color: '#d29922', ...fundSummary, totalValue: fundDisplaySummary.totalValue },
      { label: '美股', color: '#58a6ff', ...usCategorySummary },
      { label: '贵金属', color: '#a371f7', ...preciousSummary },
      { label: '内部股份分红', color: '#7ee787', ...esopSummary },
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
          borderColor: '#161a1e',
          borderWidth: 2
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: true,
        plugins: {
          legend: { position: 'bottom', labels: { color: '#e1e4e8', padding: 16 } },
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
      ibkrSummary: usCategorySummary,
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

    runWhenInvestmentAnalysisVisible(() => {
      setupAnalysisHeavyLoaders();
    });
    if (isInvestmentAnalysisHash(window.location.hash)) {
      window.setTimeout(() => setupAnalysisHeavyLoaders(), 500);
    }

  } catch (err) {
    document.getElementById('err').style.display='block';
    document.getElementById('err').textContent = '加载失败：'+String(err);
    console.error(err);
  }
}

main().finally(() => {
  if (isInvestmentAnalysisHash(window.location.hash)) {
    window.setTimeout(() => setupAnalysisHeavyLoaders(), 800);
  }
});
