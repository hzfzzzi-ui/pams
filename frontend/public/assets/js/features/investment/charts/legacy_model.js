// Keep legacy investment-return formulas here. Personal NAV comparisons come from the API.
import { safeNum } from '../../../utils.js';

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

function filterReturnWindowRows(rows, windowKey, start = returnWindowStartDate(rows, windowKey)) {
  if (!start) return rows || [];
  return (rows || []).filter(row => {
    const date = parseReturnSeriesDate(row.date);
    return date && date >= start;
  });
}

function returnRateForChart(row) {
  const value = row?.return_rate ?? row?.display_return_rate ?? row?.time_weighted_return_rate;
  return value == null || value === '' || !Number.isFinite(Number(value)) ? null : Number(value);
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
    if (row.daily_return_rate == null || row.total_pnl == null || prev.total_pnl == null) continue;
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

function buildInvestmentReturnWindowData(rows, sectionRowsByKey, sectionSpecs, windowKey, isMobile,
  events = [], sectionEstimates = new Map(), sectionGapDates = new Map(),
  sectionZeroBefore = new Map()) {
  const dateRows = [...rows, ...sectionSpecs.flatMap(spec => sectionEstimates.get(spec.key)?.rows || sectionRowsByKey.get(spec.key) || [])];
  for (const gaps of sectionGapDates.values()) {
    if (gaps) dateRows.push(...Object.keys(gaps).map(date => ({ date })));
  }
  const start = returnWindowStartDate(dateRows, windowKey);
  const windowRows = filterReturnWindowRows(rows, windowKey, start);
  const dates = new Set(filterReturnWindowRows(dateRows, windowKey, start).map(row => row.date));
  if (start) {
    const lastZeroEnd = [...sectionZeroBefore.values()].filter(Boolean).sort().at(-1);
    const day = new Date(start);
    let date = day.toISOString().slice(0, 10);
    dates.add(date);
    while (lastZeroEnd && date < lastZeroEnd) {
      dates.add(date);
      day.setUTCDate(day.getUTCDate() + 1);
      date = day.toISOString().slice(0, 10);
    }
  }
  const labels = [...dates].sort();
  const totalByDate = new Map(windowRows.map(row => [row.date, row]));
  const totalBaseRate = windowRows.map(returnRateForChart).find(value => value !== null) ?? null;
  const totalData = labels.map(date => {
    const rate = returnRateForChart(totalByDate.get(date));
    return rate === null || totalBaseRate === null ? null : rebaseReturnRate(rate, totalBaseRate) * 100;
  });
  const labelSet = new Set(labels);
  const visibleEvents = (events || [])
    .filter(event => labelSet.has(event.date))
    .sort((a, b) => b.score - a.score)
    .slice(0, RETURN_EVENT_LIMIT);
  const eventDates = new Set(visibleEvents.map(event => event.date));
  const eventData = labels.map((date, index) => eventDates.has(date) ? totalData[index] : null);
  const sectionDataByKey = new Map();
  for (const spec of sectionSpecs) {
    const estimate = sectionEstimates.get(spec.key);
    const sectionRows = estimate?.rows || sectionRowsByKey.get(spec.key) || [];
    const rateByDate = new Map(sectionRows.map(row => [row.date, returnRateForChart(row)]));
    const usable = sectionRows.filter(row => rateByDate.get(row.date) != null);
    const loneHoldingReturn = usable.length === 1
      && usable[0].return_rate_basis === 'current_holding_pnl_over_cost';
    let sectionBaseRate = null;
    const zeroBefore = sectionZeroBefore.get(spec.key);
    const sectionData = labels.map(date => {
      if (!rateByDate.has(date)) {
        if (zeroBefore && date < zeroBefore) {
          sectionBaseRate = 0;
          return 0;
        }
        return null;
      }
      const rate = rateByDate.get(date);
      if (rate === null || !Number.isFinite(rate)) return null;
      if (estimate) return rate * 100;
      if (loneHoldingReturn) return windowKey === 'ytd' ? rate * 100 : null;
      if (sectionBaseRate == null) sectionBaseRate = rate;
      return rebaseReturnRate(rate, sectionBaseRate) * 100;
    });
    const pointRadius = pointRadiusForReturnSeries(sectionData.filter(value => value != null).length, isMobile);
    const gaps = sectionGapDates.get(spec.key);
    const bridgeIndices = new Set();
    if (gaps) {
      let previous = -1;
      sectionData.forEach((value, index) => {
        if (value === null) return;
        if (previous >= 0 && index > previous + 1
            && labels.slice(previous + 1, index).every(date => gaps[date] === 'closed')) {
          for (let gap = previous + 1; gap < index; gap += 1) {
            // These are line coordinates, not return observations.
            sectionData[gap] = sectionData[previous] + (value - sectionData[previous]) * (gap - previous) / (index - previous);
            bridgeIndices.add(gap);
          }
        }
        previous = index;
      });
    }
    sectionDataByKey.set(spec.key, { data: sectionData, pointRadius, bridgeIndices });
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
    topContribution: buildTopContributionForWindow(sectionRowsByKey, sectionSpecs, windowKey, rows, sectionEstimates, labels),
  };
}

function normalizeInvestmentNavRows(rows) {
  return (rows || [])
    .map(row => {
      const date = row.date || row.asOfDate || row.snapshot_date;
      const fullValue = row.netWorth ?? row.net_worth ?? row.market_value;
      const partial = fullValue == null && row.availableNetWorth != null && row.available_accounts?.length;
      const rawValue = partial ? row.availableNetWorth : fullValue;
      const marketValue = rawValue == null || rawValue === '' ? null : Number(rawValue);
      if (!date) return null;
      return {
        ...row,
        date,
        market_value: Number.isFinite(marketValue) ? marketValue : null,
        // A legacy response has no evidence that adjacent amounts use the same basis.
        comparison_scope: (partial ? 'partial:' : 'complete:') + (row.comparison_scope || 'unknown:' + date),
        available_account_count: partial ? row.available_accounts.length : null,
        assets: row.assets == null ? null : safeNum(row.assets),
        liabilities: row.liabilities == null ? null : safeNum(row.liabilities),
      };
    })
    .filter(Boolean)
    .sort((a, b) => String(a.date).localeCompare(String(b.date)));
}

function buildInvestmentNavChartData(navRows, period, isMobile) {
  const rows = period ? navRows.filter(row => row.date >= period.start && row.date <= period.end) : [];
  const labels = rows.map(row => row.date);
  const values = rows.map(row => row.market_value);
  const scopes = rows.map(row => row.comparison_scope);
  const latestRow = rows.at(-1);
  return {
    labels,
    values,
    scopes,
    latest: latestRow && latestRow.date === period.end ? latestRow.market_value : null,
    period,
    change: period?.status === 'ok' ? period.change : null,
    changeRate: period?.status === 'ok' ? period.change_rate : null,
    fill: Boolean(scopes[0]?.startsWith('complete:')) &&
      rows.every((row, i) => values[i] != null && scopes[i] === scopes[0]),
    pointRadius: pointRadiusForReturnSeries(values.length, isMobile),
  };
}

function sectionReturnAmount(row) {
  if (!row) return 0;
  if (row.total_pnl != null && Number.isFinite(Number(row.total_pnl))) return safeNum(row.total_pnl);
  if (row.position_pnl != null && Number.isFinite(Number(row.position_pnl))) return safeNum(row.position_pnl);
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

function buildTopContributionForWindow(sectionRowsByKey, sectionSpecs, windowKey, totalRows, sectionEstimates = new Map(), labels = []) {
  const totalWindowRows = filterReturnWindowRows(totalRows, windowKey);
  const partial = totalWindowRows.length < 2 || totalWindowRows.some(row => row.total_pnl == null || row.market_value == null);
  const dates = partial && labels.length ? labels : totalWindowRows.map(row => row.date);
  const startDate = dates[0];
  const endDate = dates[dates.length - 1];
  const seriesTotalPnl = partial
    ? null
    : safeNum(totalWindowRows[totalWindowRows.length - 1].total_pnl) - safeNum(totalWindowRows[0].total_pnl);
  const latestTotalValue = safeNum(totalWindowRows[totalWindowRows.length - 1]?.market_value);
  const rows = sectionSpecs.map((spec) => {
    const estimate = partial ? sectionEstimates.get(spec.key) : null;
    const periodRows = (estimate?.rows || sectionRowsByKey.get(spec.key) || []).filter((row) => {
      const date = String(row.date || '');
      return (!startDate || date >= startDate) && (!endDate || date <= endDate);
    });
    if (periodRows.length < 2 || periodRows.some(row => row.total_pnl == null && row.position_pnl == null
      && (row.market_value == null || row.cumulative_capital == null))) return null;
    if (partial && (dates.length !== periodRows.length || periodRows.some((row, index) => row.date !== dates[index]))) return null;
    if (estimate && periodRows.some(row => row.total_pnl == null || !Number.isFinite(Number(row.total_pnl)))) return null;
    const pnl = sectionPeriodPnl(periodRows);
    const latestValue = safeNum(periodRows[periodRows.length - 1]?.market_value);
    return {
      key: spec.key,
      name: spec.label,
      assetClass: '资产桶',
      account: estimate?.label || returnWindowLabel(windowKey),
      pnl,
      returnPct: partial ? null : sectionPeriodReturnRate(periodRows, pnl),
      weight: partial ? null : latestTotalValue > 0 ? latestValue / latestTotalValue : 0,
    };
  }).filter(item => item && (partial || Math.abs(item.pnl) > 0.005 || Math.abs(item.weight) > 0.0001));
  const sectionPnl = rows.reduce((sum, item) => sum + safeNum(item.pnl), 0);
  const totalPnl = partial ? (rows.length ? sectionPnl : null) : seriesTotalPnl;
  const residualPnl = safeNum(totalPnl) - sectionPnl;
  const residualThreshold = Math.max(1, Math.abs(safeNum(totalPnl)) * 0.002);
  const unattributed = !partial && Math.abs(residualPnl) > residualThreshold
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
    partial,
    unavailable: partial && !rows.length,
    totalPnl,
    winners: rows.filter(item => item.pnl > 0.005).sort((a, b) => b.pnl - a.pnl),
    losers: rows.filter(item => item.pnl < -0.005).sort((a, b) => a.pnl - b.pnl),
    unattributed,
  };
}

export { RETURN_WINDOW_OPTIONS, normalizeReturnWindowKey, parseReturnSeriesDate, returnWindowStartDate, filterReturnWindowRows, returnRateForChart, rebaseReturnRate, returnWindowLabel, returnEventCapitalBase, returnEventAdjustedAmount, returnEventMeta, buildReturnEvents, pointRadiusForReturnSeries, buildInvestmentReturnWindowData, normalizeInvestmentNavRows, buildInvestmentNavChartData, sectionReturnAmount, sectionPeriodPnl, sectionPeriodReturnRate, buildTopContributionForWindow };
