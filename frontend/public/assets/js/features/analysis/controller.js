import { ANALYSIS_PANEL_KEYS } from './constants.js?v=20261031-analysis-modules-v1';
import { createAnalysisState, safeStorageSet, analysisPanelFromHash } from './state.js?v=20261031-analysis-modules-v1';
import { createAnalysisLegacyModel } from './legacy_model.js?v=20261031-analysis-modules-v1';
import { createAnalysisCharts } from './charts.js?v=20261031-analysis-modules-v1';
import { createAnalysisTemplates } from './templates.js?v=20261031-analysis-modules-v1';
import { createAnalysisView } from './view.js?v=20261031-analysis-modules-v1';
import { createAnalysisRequests } from './requests.js?v=20261031-analysis-modules-v1';
import { createBottomSheet } from '../../components/bottom_sheet.js?v=20261031-ui-v1';
import { afterInvestmentNav } from '../../investment_startup.js?v=20261031-gray-v1';

const owners = new WeakMap();

export function createAnalysisController({ root = document, host = window, viewState = null, requests = createAnalysisRequests(), afterNav = afterInvestmentNav } = {}) {
  owners.get(root)?.dispose();
  const doc = root.ownerDocument || root;
  const state = createAnalysisState(host, viewState);
  const model = createAnalysisLegacyModel({ state });
  const charts = createAnalysisCharts({ root, state, model });
  const templates = createAnalysisTemplates({ state, model });
  const view = createAnalysisView({ root, state, model, charts, templates });
  const sheet = createBottomSheet(doc);
  const { esc, setReturnAttributionHoldingDailySummary, currentRowsForClick, displayName, returnAttributionHeatmapRows, returnAttributionRows } = model;
  const { rowDetailHtml, returnAttributionDetailHtml } = templates;
  const { renderAnalysisPanelSwitch, renderReturnAttribution, renderReturnAttributionList, renderLookthrough, renderAssetMaster } = view;
  const { setActiveChartSegment } = charts;
  const getElementById = id => root.getElementById ? root.getElementById(id) : root.querySelector('[id="' + id + '"]');
  const removers = [];
  let initialized = false;
  let disposed = false;
  let observer = null;
  let lookthroughLoadPromise = null;
  let returnAttributionLoadPromise = null;
  let returnAttributionHoldingDailyLoadPromise = null;

  function current() { return !disposed && owners.get(root) === controller; }
  function listen(target, type, callback, options) {
    target.addEventListener(type, callback, options);
    removers.push(() => target.removeEventListener(type, callback, options));
  }
  function isAnalysisPanelVisible() {
    if (doc.body?.dataset.investTab === 'analysis') return true;
    const panel = root.querySelector?.('[data-invest-panel="analysis"]');
    return Boolean(panel && panel.hidden === false);
  }
  function isRiskPanelVisible() {
    if (doc.body?.dataset.investTab === 'risk') return true;
    const panel = root.querySelector?.('[data-invest-panel="risk"]');
    return Boolean(panel && panel.hidden === false);
  }
  function attributionVisible() { return current() && isAnalysisPanelVisible() && state.activeAnalysisPanel === 'return_attribution'; }
  function renderVisibleExposure() {
    if (!current()) return;
    if (isRiskPanelVisible()) renderLookthrough();
    if (isAnalysisPanelVisible() && state.activeAnalysisPanel === 'asset_master') renderAssetMaster();
  }

  function loadReturnAttributionHoldingDailySummary() {
    if (!current()) return Promise.resolve(null);
    if (state.returnAttributionHoldingDailySummary) return Promise.resolve(state.returnAttributionHoldingDailySummary);
    if (returnAttributionHoldingDailyLoadPromise) return returnAttributionHoldingDailyLoadPromise;
    returnAttributionHoldingDailyLoadPromise = requests.holdingDaily()
      .then(data => {
        if (!current()) return null;
        setReturnAttributionHoldingDailySummary(data);
        return data;
      })
      .catch(() => {
        if (current()) setReturnAttributionHoldingDailySummary(null);
        return null;
      })
      .finally(() => { returnAttributionHoldingDailyLoadPromise = null; });
    return returnAttributionHoldingDailyLoadPromise;
  }

  function loadLookthroughData() {
    if (!current() || !getElementById('lookthrough-exposure')) return Promise.resolve();
    if (state.lookthroughData) {
      renderVisibleExposure();
      return Promise.resolve(state.lookthroughData);
    }
    if (lookthroughLoadPromise) return lookthroughLoadPromise;
    lookthroughLoadPromise = requests.lookthrough()
      .then(data => {
        if (!current()) return null;
        state.lookthroughData = data;
        renderVisibleExposure();
        return data;
      })
      .catch(error => {
        if (current() && (isRiskPanelVisible() || (isAnalysisPanelVisible() && state.activeAnalysisPanel === 'asset_master'))) {
          const el = getElementById('lookthroughList');
          if (el) el.innerHTML = `<div class="lookthroughEmpty">风险暴露接口暂不可用：${esc(error?.message || error)}</div>`;
        }
        throw error;
      })
      .finally(() => { lookthroughLoadPromise = null; });
    return lookthroughLoadPromise;
  }

  function loadReturnAttributionData() {
    if (!current() || !getElementById('return-attribution')) return Promise.resolve();
    if (state.returnAttributionData) {
      if (attributionVisible()) renderReturnAttribution();
      if (!state.returnAttributionHoldingDailySummary) {
        return loadReturnAttributionHoldingDailySummary().then(() => {
          if (!current()) return null;
          if (attributionVisible()) renderReturnAttribution();
          return state.returnAttributionData;
        });
      }
      return Promise.resolve(state.returnAttributionData);
    }
    if (returnAttributionLoadPromise) return returnAttributionLoadPromise;
    returnAttributionLoadPromise = Promise.all([requests.attribution(), loadReturnAttributionHoldingDailySummary()])
      .then(([data]) => {
        if (!current()) return null;
        state.returnAttributionData = data;
        if (attributionVisible()) renderReturnAttribution();
        return data;
      })
      .catch(error => {
        if (attributionVisible()) {
          const el = getElementById('returnAttributionList');
          if (el) el.innerHTML = `<div class="lookthroughEmpty">收益归因接口暂不可用：${esc(error?.message || error)}</div>`;
        }
        throw error;
      })
      .finally(() => { returnAttributionLoadPromise = null; });
    return returnAttributionLoadPromise;
  }

  function loadActiveAnalysisPanel() {
    if (!current()) return Promise.resolve();
    if (state.activeAnalysisPanel === 'return_attribution') return loadReturnAttributionData().catch(() => {});
    if (state.activeAnalysisPanel === 'trade_attribution') {
      host.dispatchEvent(new host.CustomEvent('pams:load-trade-attribution'));
      return Promise.resolve();
    }
    return loadLookthroughData().catch(() => {});
  }

  function scheduleVisibleAnalysisLoad() {
    if (!current() || !getElementById('return-attribution')) return;
    const hashPanel = analysisPanelFromHash(host.location?.hash);
    if (hashPanel && hashPanel !== state.activeAnalysisPanel) {
      state.activeAnalysisPanel = hashPanel;
      safeStorageSet(host, 'pams.analysis.panel', state.activeAnalysisPanel);
    }
    renderAnalysisPanelSwitch();
    if (isAnalysisPanelVisible()) {
      if (state.activeAnalysisPanel === 'trade_attribution') return loadActiveAnalysisPanel();
      // Keep NAV ordering; a later trade selection must not dispatch the event twice.
      afterNav(() => {
        if (current() && isAnalysisPanelVisible() && state.activeAnalysisPanel !== 'trade_attribution') return loadActiveAnalysisPanel();
      }).catch(() => {});
    }
  }
  function scheduleRiskExposureLoad() {
    if (!current() || !getElementById('lookthrough-exposure')) return;
    if (isRiskPanelVisible()) loadLookthroughData().catch(() => {});
  }
  function scheduleVisiblePanelLoads() {
    scheduleVisibleAnalysisLoad();
    scheduleRiskExposureLoad();
  }

  function bindEvents() {
    const syncChartSelection = (event) => {
      if (!current() || (root.contains && !root.contains(event.target))) return;
      const target = event.target?.closest?.('[data-lookthrough-segment], [data-lookthrough-segment-list]');
      if (!target) return;
      const segmentIndex = target.dataset.lookthroughSegment ?? target.dataset.lookthroughSegmentList;
      if (segmentIndex !== undefined) setActiveChartSegment(segmentIndex, target);
    };

    listen(root, 'pointerover', syncChartSelection);
    listen(root, 'touchstart', syncChartSelection, { passive: true });

    listen(root, 'click', (event) => {
      if (!current() || (root.contains && !root.contains(event.target))) return;
      const panelSwitch = event.target?.closest?.('[data-analysis-panel-switch]');
      if (panelSwitch) {
        state.activeAnalysisPanel = panelSwitch.dataset.analysisPanelSwitch || 'return_attribution';
        if (!ANALYSIS_PANEL_KEYS.has(state.activeAnalysisPanel)) state.activeAnalysisPanel = 'return_attribution';
        safeStorageSet(host, 'pams.analysis.panel', state.activeAnalysisPanel);
        renderAnalysisPanelSwitch();
        loadActiveAnalysisPanel();
        return;
      }

      const returnTab = event.target?.closest?.('[data-return-attr-dim]');
      if (returnTab) {
        state.activeReturnAttributionDimension = returnTab.dataset.returnAttrDim;
        state.returnAttributionShowAll = false;
        safeStorageSet(host, 'pams.returnAttribution.dimension', state.activeReturnAttributionDimension);
        renderReturnAttribution();
        return;
      }

      const returnMore = event.target?.closest?.('[data-return-attr-more]');
      if (returnMore) {
        state.returnAttributionShowAll = !state.returnAttributionShowAll;
        renderReturnAttributionList();
        return;
      }

      const tab = event.target?.closest?.('[data-lookthrough-dim]');
      if (tab) {
        state.activeDimension = tab.dataset.lookthroughDim;
        safeStorageSet(host, 'pams.lookthrough.dimension', state.activeDimension);
        renderLookthrough();
        return;
      }

      const family = event.target?.closest?.('[data-theme-family]');
      if (family) {
        state.activeThemeFamily = family.dataset.themeFamily || '全部';
        renderLookthrough();
        return;
      }

      const sort = event.target?.closest?.('[data-underlying-sort]');
      if (sort) {
        state.underlyingSort = sort.dataset.underlyingSort || 'value';
        renderLookthrough();
        return;
      }

      const chartTarget = event.target?.closest?.('[data-lookthrough-segment], [data-lookthrough-segment-list]');
      if (chartTarget) {
        const segmentIndex = chartTarget.dataset.lookthroughSegment ?? chartTarget.dataset.lookthroughSegmentList;
        if (segmentIndex !== undefined) setActiveChartSegment(segmentIndex, chartTarget);
      }

      const rowButton = event.target?.closest?.('[data-lookthrough-row], [data-underlying-row]');
      if (rowButton && state.lookthroughData) {
        const index = Number(rowButton.dataset.lookthroughRow ?? rowButton.dataset.underlyingRow);
        const row = currentRowsForClick()[index];
        if (row) sheet.open(`穿透：${displayName(row)}`, rowDetailHtml(row));
      }

      const returnAssetHeatButton = event.target?.closest?.('[data-return-asset-heat-row]');
      if (returnAssetHeatButton && state.returnAttributionData) {
        const index = Number(returnAssetHeatButton.dataset.returnAssetHeatRow);
        const row = returnAttributionHeatmapRows()[index];
        if (row) sheet.open(`收益归因：${row.display_name || row.name}`, returnAttributionDetailHtml(row, { useHoldingDaily: true }));
      }

      const returnRowButton = event.target?.closest?.('[data-return-attr-row]');
      if (returnRowButton && state.returnAttributionData) {
        const index = Number(returnRowButton.dataset.returnAttrRow);
        const row = returnAttributionRows()[index];
        if (row) sheet.open(`收益归因：${row.display_name || row.name}`, returnAttributionDetailHtml(row));
      }
    });
  }

  const controller = {
    state, view, charts,
    get disposed() { return disposed; },
    scheduleVisibleAnalysisLoad, scheduleVisiblePanelLoads, loadActiveAnalysisPanel,
    loadLookthroughData, loadReturnAttributionData,
    init() {
      if (initialized || !current()) return controller;
      initialized = true;
      bindEvents();
      scheduleVisiblePanelLoads();
      listen(host, 'hashchange', scheduleVisiblePanelLoads);
      if (typeof host.MutationObserver === 'function' && doc.body) {
        observer = new host.MutationObserver(scheduleVisiblePanelLoads);
        observer.observe(doc.body, { attributes: true, attributeFilter: ['data-invest-tab'] });
      }
      return controller;
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      observer?.disconnect();
      observer = null;
      for (const remove of removers.splice(0)) remove();
      sheet.dispose({ restoreFocus: false });
      charts.dispose();
      if (owners.get(root) === controller) owners.delete(root);
    },
  };
  owners.set(root, controller);
  return controller;
}
