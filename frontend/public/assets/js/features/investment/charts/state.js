import { normalizeReturnWindowKey, buildReturnEvents, buildInvestmentReturnWindowData, returnRateForChart } from './legacy_model.js';

const RETURN_WINDOW_STORAGE_KEY = 'pams.investmentReturn.window';
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

export function createInvestmentChartState(host) {
  return {
    navRows: [],
    activeWindow: readReturnWindowPreference(),
    isMobile: host.matchMedia?.('(min-width: 0px)')?.matches,
    navChart: null,
  };
}

export function initializeReturnState(state, resp, sectionResponses, sectionSpecs) {
  const rows = resp.rows || [];
  const sectionRowsByKey = new Map(sectionSpecs.map((spec, index) => [spec.key, sectionResponses[index]?.rows || []]));
  const sectionEstimates = new Map();
  sectionSpecs.forEach((spec, index) => {
    const estimate = sectionResponses[index]?.estimated_series;
    if (estimate?.rows?.length && !sectionRowsByKey.get(spec.key).some(row => returnRateForChart(row) != null)) {
      sectionEstimates.set(spec.key, estimate);
    }
  });
  Object.assign(state, {
    rows,
    totalReturnReason: resp.reason,
    totalReturnScope: resp.return_scope,
    sectionRowsByKey,
    sectionEstimates,
    sectionZeroBefore: new Map(sectionSpecs.map((spec, index) =>
      [spec.key, sectionEstimates.get(spec.key)?.chart_zero_before ?? sectionResponses[index]?.chart_zero_before])),
    sectionGapDates: new Map(sectionSpecs.map((spec, index) =>
      [spec.key, (sectionEstimates.get(spec.key) ?? sectionResponses[index])?.chart_gap_dates])),
    sectionReasons: new Map(sectionSpecs.map((spec, index) => [spec.key, sectionResponses[index]?.reason])),
    sectionClosedTradeReturns: new Map(sectionSpecs.map((spec, index) =>
      [spec.key, sectionResponses[index]?.closed_trade_returns])),
    sectionSpecs,
    events: buildReturnEvents(rows),
    visibleRows: [],
    visibleEvents: [],
    topContribution: null,
  });
  return updateVisibleReturnState(state);
}

export function updateVisibleReturnState(state) {
  const windowData = buildInvestmentReturnWindowData(
    state.rows,
    state.sectionRowsByKey,
    state.sectionSpecs,
    state.activeWindow,
    state.isMobile,
    state.events,
    state.sectionEstimates,
    state.sectionGapDates,
    state.sectionZeroBefore,
  );
  state.visibleRows = windowData.rows;
  state.visibleEvents = windowData.visibleEvents;
  state.topContribution = windowData.topContribution;
  return windowData;
}

export function setReturnWindow(state, nextWindowKey, persist = true) {
  state.activeWindow = normalizeReturnWindowKey(nextWindowKey);
  if (persist) writeReturnWindowPreference(state.activeWindow);
}
