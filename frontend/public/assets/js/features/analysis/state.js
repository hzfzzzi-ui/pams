import { DIMENSION_KEYS, RETURN_ATTRIBUTION_KEYS, ANALYSIS_PANEL_KEYS } from './constants.js?v=20261031-analysis-modules-v1';

export function safeStorageGet(host, key) {
  try { return host.localStorage?.getItem(key) ?? null; } catch { return null; }
}

export function safeStorageSet(host, key, value) {
  try { host.localStorage?.setItem(key, value); } catch { /* Privacy mode must not block rendering. */ }
}

export function analysisPanelFromHash(hash = '') {
  const key = String(hash || '').replace(/^#/, '');
  if (key === 'asset-master-panel' || key === 'asset-master') return 'asset_master';
  if (key === 'return-attribution') return 'return_attribution';
  if (key === 'trade-attribution-analysis') return 'trade_attribution';
  if (key === 'return-trend') return 'return_attribution';
  return null;
}

const VIEW_FIELDS = ['activeDimension', 'activeThemeFamily', 'underlyingSort', 'activeReturnAttributionDimension', 'returnAttributionShowAll', 'activeAnalysisPanel'];

export function snapshotAnalysisViewState(state) {
  return Object.fromEntries(VIEW_FIELDS.map(key => [key, state[key]]));
}

export function createAnalysisState(host, viewState) {
  const state = {
    activeDimension: safeStorageGet(host, 'pams.lookthrough.dimension') || 'asset_type',
    activeThemeFamily: '全部', underlyingSort: 'value', lookthroughData: null,
    activeReturnAttributionDimension: safeStorageGet(host, 'pams.returnAttribution.dimension') || 'holding',
    returnAttributionShowAll: false, returnAttributionData: null,
    activeAnalysisPanel: analysisPanelFromHash(host.location?.hash) || safeStorageGet(host, 'pams.analysis.panel') || 'return_attribution',
    returnAttributionHoldingDailySummary: null, returnAttributionHoldingDailyMap: new Map(),
  };
  if (viewState) for (const key of VIEW_FIELDS) if (key in viewState) state[key] = viewState[key];
  if (state.activeDimension === 'underlying_asset') state.activeDimension = 'underlying_exposure';
  if (!DIMENSION_KEYS.has(state.activeDimension)) state.activeDimension = 'asset_type';
  if (!RETURN_ATTRIBUTION_KEYS.has(state.activeReturnAttributionDimension)) state.activeReturnAttributionDimension = 'holding';
  if (!ANALYSIS_PANEL_KEYS.has(state.activeAnalysisPanel)) state.activeAnalysisPanel = 'return_attribution';
  return state;
}
