// Compatibility-only chart model. Bodies copied unchanged from holdings_categories.js.
// Existing rebasing, NAV summary, event thresholds and attribution remain legacy behavior.
// Do not extend these formulas; future changes require the backend result contract.
import { safeNum } from '../../../utils.js?v=20261031-modules-v1';

const RETURN_WINDOW_OPTIONS = [
  { key: 'ytd', label: '年初至今' },
  { key: '1m', label: '近1月' },
  { key: '1w', label: '近1周' },
];
const RETURN_EVENT_LIMIT = 4;

function normalizeReturnWindowKey(key) {
  return RETURN_WINDOW_OPTIONS.some(item => item.key === key) ? key : 'ytd';
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
  return safeNum(row?.return_rate ?? row?.display_return_rate ?? row?.time_weighted_return_rate);
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

export { RETURN_WINDOW_OPTIONS, normalizeReturnWindowKey, parseReturnSeriesDate, returnWindowStartDate, filterReturnWindowRows, returnRateForChart, rebaseReturnRate, returnWindowLabel, returnEventCapitalBase, returnEventAdjustedAmount, returnEventMeta, buildReturnEvents, pointRadiusForReturnSeries, buildInvestmentReturnWindowData, normalizeInvestmentNavRows, buildInvestmentNavChartData, sectionReturnAmount, sectionPeriodPnl, sectionPeriodReturnRate, buildTopContributionForWindow };
