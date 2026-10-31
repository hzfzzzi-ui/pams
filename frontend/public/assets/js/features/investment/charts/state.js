import { normalizeReturnWindowKey, buildReturnEvents, buildInvestmentReturnWindowData } from './legacy_model.js?v=20261031-investment-charts-v1';

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
  Object.assign(state, {
    rows,
    sectionRowsByKey,
    sectionSpecs,
    events: buildReturnEvents(rows),
    visibleRows: [],
    visibleEvents: [],
    topContribution: null,
    pnlMap: new Map(),
    valueMap: new Map(),
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
  );
  state.visibleRows = windowData.rows;
  state.visibleEvents = windowData.visibleEvents;
  state.topContribution = windowData.topContribution;
  state.pnlMap = windowData.pnlMap;
  state.valueMap = windowData.valueMap;
  return windowData;
}

export function setReturnWindow(state, nextWindowKey, persist = true) {
  state.activeWindow = normalizeReturnWindowKey(nextWindowKey);
  if (persist) writeReturnWindowPreference(state.activeWindow);
}
