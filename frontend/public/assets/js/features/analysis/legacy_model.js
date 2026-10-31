// Existing display bindings and legacy calculations are preserved; API business semantics remain upstream.
import { TYPE_LABELS, EXPOSURE_COLORS } from './constants.js?v=20261031-analysis-modules-v1';

export function createAnalysisLegacyModel({ state }) {

function esc(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function n(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function returnAttributionDailyKey(value) {
  return String(value ?? '')
    .trim()
    .toUpperCase()
    .replace(/\s+/g, ' ');
}

function returnAttributionAddDailyKey(map, prefix, value, amount) {
  const key = returnAttributionDailyKey(value);
  if (!key) return;
  const fullKey = `${prefix}:${key}`;
  map.set(fullKey, n(map.get(fullKey)) + amount);
}

function returnAttributionNameWithoutSymbol(name, symbol) {
  const rawName = String(name || '').trim();
  const rawSymbol = String(symbol || '').trim();
  if (!rawName || !rawSymbol) return rawName;
  if (rawName.toUpperCase().startsWith(`${rawSymbol.toUpperCase()} - `)) {
    return rawName.slice(rawSymbol.length + 3).trim();
  }
  return rawName;
}

function buildReturnAttributionHoldingDailyMap(summary) {
  const map = new Map();
  (summary?.children || []).forEach((row) => {
    const daily = n(row?.daily_pnl);
    returnAttributionAddDailyKey(map, 'symbol', row?.symbol, daily);
    returnAttributionAddDailyKey(map, 'name', row?.name, daily);
    returnAttributionAddDailyKey(map, 'name', returnAttributionNameWithoutSymbol(row?.name, row?.symbol), daily);
  });
  return map;
}

function setReturnAttributionHoldingDailySummary(summary) {
  state.returnAttributionHoldingDailySummary = summary || null;
  state.returnAttributionHoldingDailyMap = buildReturnAttributionHoldingDailyMap(summary);
}

function returnAttributionLookupHoldingDaily(row) {
  const keys = [
    ['symbol', row?.symbol],
    ['symbol', row?.source_symbol],
    ['name', row?.display_name],
    ['name', row?.name],
    ['name', row?.source_asset_name],
    ['name', returnAttributionNameWithoutSymbol(row?.display_name || row?.name, row?.symbol || row?.source_symbol)],
  ];
  for (const [prefix, value] of keys) {
    const key = returnAttributionDailyKey(value);
    if (key && state.returnAttributionHoldingDailyMap.has(`${prefix}:${key}`)) {
      return n(state.returnAttributionHoldingDailyMap.get(`${prefix}:${key}`));
    }
  }
  return 0;
}

function fmtMoney(value) {
  const amount = n(value);
  return `${amount < 0 ? '-' : ''}¥${Math.abs(amount).toLocaleString('zh-CN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

function fmtPct(value) {
  return `${(n(value) * 100).toFixed(1)}%`;
}

function fmtSignedMoney(value) {
  const amount = n(value);
  return `${amount < 0 ? '-' : '+'}¥${Math.abs(amount).toLocaleString('zh-CN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

function fmtCompactSignedMoney(value) {
  const amount = n(value);
  if (!amount) return '¥0';
  const sign = amount < 0 ? '-' : '+';
  const abs = Math.abs(amount);
  const compact = (next, digits = 0) => next.toLocaleString('zh-CN', {
    minimumFractionDigits: 0,
    maximumFractionDigits: digits,
  });
  if (abs >= 100000000) return `${sign}¥${compact(abs / 100000000, 1)}亿`;
  if (abs >= 10000) return `${sign}¥${compact(abs / 10000, abs >= 100000 ? 1 : 2)}万`;
  if (abs >= 1000) return `${sign}¥${compact(abs, 0)}`;
  if (abs >= 100) return `${sign}¥${compact(abs, 0)}`;
  return `${sign}¥${compact(abs, abs >= 10 ? 1 : 2)}`;
}

function calibratedReturnAttributionData() {
  return state.returnAttributionData;
}

function confidenceClass(confidence) {
  const key = String(confidence || '').toUpperCase();
  if (key === 'HIGH') return 'ok';
  if (key === 'MEDIUM') return 'warn';
  return 'pending';
}

function confidenceLabel(confidence) {
  const key = String(confidence || '').toUpperCase();
  return { HIGH: '高', MEDIUM: '中', LOW: '低' }[key] || '低';
}

function qualityClass(status) {
  const key = String(status || '').toUpperCase();
  if (key === 'FULL') return 'ok';
  if (key === 'INDEX_PROXY' || key === 'TOP10_ONLY') return 'data';
  if (key === 'PRODUCT_TAG_ONLY' || key === 'STALE') return 'warn';
  return 'pending';
}

function rowValue(row) {
  return n(row?.value_cny ?? row?.total_exposure_cny ?? row?.effectiveMarketValue);
}

function rowWeight(row) {
  return n(row?.weight ?? row?.total_weight ?? row?.effectiveWeightInPortfolio);
}

function exposureRows(dimension) {
  const rows = state.lookthroughData?.exposures?.[dimension];
  return Array.isArray(rows) ? rows : [];
}

function exposureName(row) {
  return String(row?.display_name ?? row?.name ?? '');
}

function portfolioTotalValue() {
  const explicitTotal = n(state.lookthroughData?.total_market_value_cny);
  if (explicitTotal > 0) return explicitTotal;
  return exposureRows('asset_type').reduce((sum, row) => sum + rowValue(row), 0);
}

function displayName(row) {
  if (!row) return '';
  if (row.display_name) return row.display_name;
  return row.name;
}

function chartSegments(rows, maxItems = 6) {
  const visible = rows.slice(0, maxItems).map((row, index) => ({
    row,
    index,
    label: displayName(row),
    weight: Math.max(rowWeight(row), 0),
    value: rowValue(row),
    color: EXPOSURE_COLORS[index % EXPOSURE_COLORS.length],
  }));
  const otherValue = rows.slice(maxItems).reduce((sum, row) => sum + rowValue(row), 0);
  const otherWeight = Math.max(1 - visible.reduce((sum, item) => sum + item.weight, 0), 0);
  if (otherValue > 0.005 || otherWeight > 0.005) {
    visible.push({
      row: null,
      index: -1,
      label: '其他',
      weight: otherWeight,
      value: otherValue,
      color: EXPOSURE_COLORS[EXPOSURE_COLORS.length - 1],
    });
  }
  return visible.filter(item => item.weight > 0 || item.value > 0);
}

function chartVisibleLimit(rows) {
  if (state.activeDimension === 'market') return rows.length;
  if (state.activeDimension === 'industry') return 8;
  return 6;
}

function sortUnderlyingRows(rows) {
  const sorted = [...rows];
  if (state.underlyingSort === 'industry') {
    sorted.sort((a, b) => String(a.industry_bucket || '').localeCompare(String(b.industry_bucket || ''), 'zh-CN') || rowValue(b) - rowValue(a));
  } else if (state.underlyingSort === 'theme') {
    sorted.sort((a, b) => String((a.theme_names || [])[0] || '').localeCompare(String((b.theme_names || [])[0] || ''), 'zh-CN') || rowValue(b) - rowValue(a));
  } else if (state.underlyingSort === 'source') {
    sorted.sort((a, b) => String(a.source || '').localeCompare(String(b.source || ''), 'zh-CN') || rowValue(b) - rowValue(a));
  } else if (state.underlyingSort === 'weight') {
    sorted.sort((a, b) => rowWeight(b) - rowWeight(a));
  } else {
    sorted.sort((a, b) => rowValue(b) - rowValue(a));
  }
  return sorted;
}

function splitQualityIssues(items = []) {
  const residual = [];
  const review = [];
  items.forEach(item => {
    const uncoveredValue = n(item.uncovered_value_cny);
    const status = String(item.quality_status || '').toUpperCase();
    const coverage = Math.max(Math.min(n(item.coverage_ratio), 1), 0);
    const hasResidual = uncoveredValue > 0.005 || status === 'MISSING' || status === 'STALE' || (status === 'PRODUCT_TAG_ONLY' && coverage < 1);
    if (hasResidual) residual.push(item);
    else review.push(item);
  });
  return { residual, review };
}

function sourceBreakdownText(sourceBreakdown = {}) {
  const entries = Object.entries(sourceBreakdown || {});
  if (!entries.length) return '暂无';
  return entries
    .sort((a, b) => n(b[1]) - n(a[1]))
    .map(([key, value]) => `${TYPE_LABELS[key] || key} ${fmtMoney(value)}`)
    .join(' / ');
}

function returnAttributionRows() {
  return calibratedReturnAttributionData()?.dimensions?.[state.activeReturnAttributionDimension] || [];
}

function returnAttributionRowsForDimension(dimension) {
  return calibratedReturnAttributionData()?.dimensions?.[dimension] || [];
}

function returnAttributionHeatmapRows() {
  return returnAttributionRowsForDimension('holding')
    .filter(row => row && (Math.abs(n(row.pnl_cny ?? row.total_pnl_cny ?? row.value_cny)) > 0.01 || n(row.market_value_cny) > 0))
    .slice()
    .sort((a, b) => Math.abs(n(b.pnl_cny ?? b.total_pnl_cny ?? b.value_cny)) - Math.abs(n(a.pnl_cny ?? a.total_pnl_cny ?? a.value_cny)))
    .slice(0, 60);
}

function returnAttributionAssetPnl(row) {
  return n(row?.pnl_cny ?? row?.total_pnl_cny ?? row?.value_cny);
}

function returnAttributionAssetWeight(row) {
  const pnl = Math.abs(returnAttributionAssetPnl(row));
  if (pnl > 0.01) return pnl;
  return Math.max(Math.abs(n(row?.market_value_cny)) * 0.015, 1);
}

function returnAttributionAssetLabel(row) {
  const symbol = String(row?.symbol || '').trim();
  const name = String(row?.display_name || row?.name || symbol || '').trim();
  if (!name) return symbol || '--';
  if (symbol && name.toUpperCase().startsWith(`${symbol.toUpperCase()} - `)) {
    return name.slice(symbol.length + 3).trim() || name;
  }
  return name;
}

function returnAttributionAssetMeta(row) {
  const symbol = String(row?.symbol || '').trim();
  const name = String(row?.display_name || row?.name || '').trim();
  if (symbol && name && name !== symbol) return symbol;
  return symbol || '';
}

function isClosedAttributionRow(row) {
  return row?.source === '已清仓/零市值';
}

function dailyFxValueText(value, status = 'confirmed') {
  if (value == null || !['number', 'string'].includes(typeof value)
    || String(value).trim() === '' || !Number.isFinite(Number(value))) return '待核准';
  const suffix = status === 'confirmed' ? '' : status === 'partial' ? '（部分，待核准）' : '（待核准）';
  return `${fmtSignedMoney(value)}${suffix}`;
}

function returnAttributionFxDisplay(row) {
  const display = row?.fx_display;
  if (display?.show === false || (!display && row?.currency === 'CNY')) return null;
  return {
    label: display?.label || '证券汇兑估算（人民币）',
    value: display?.show === true && display.status === 'estimated'
      && display.scope === 'instrument_only_not_account_net_fx'
      && display.date_status === 'not_independently_verified' ? display.value_cny : null,
  };
}

function currentRowsForClick() {
  const rows = state.lookthroughData?.exposures?.[state.activeDimension] || [];
  if (state.activeDimension === 'theme' && state.activeThemeFamily !== '全部') {
    return rows.filter(row => row.family === state.activeThemeFamily);
  }
  if (state.activeDimension === 'underlying_exposure') {
    return sortUnderlyingRows(rows).slice(0, 20);
  }
  return rows;
}

  return { esc, n, returnAttributionDailyKey, returnAttributionAddDailyKey, returnAttributionNameWithoutSymbol, buildReturnAttributionHoldingDailyMap, setReturnAttributionHoldingDailySummary, returnAttributionLookupHoldingDaily, fmtMoney, fmtPct, fmtSignedMoney, fmtCompactSignedMoney, calibratedReturnAttributionData, confidenceClass, confidenceLabel, qualityClass, rowValue, rowWeight, exposureRows, exposureName, portfolioTotalValue, displayName, chartSegments, chartVisibleLimit, sortUnderlyingRows, splitQualityIssues, sourceBreakdownText, returnAttributionRows, returnAttributionRowsForDimension, returnAttributionHeatmapRows, returnAttributionAssetPnl, returnAttributionAssetWeight, returnAttributionAssetLabel, returnAttributionAssetMeta, isClosedAttributionRow, dailyFxValueText, returnAttributionFxDisplay, currentRowsForClick };
}
