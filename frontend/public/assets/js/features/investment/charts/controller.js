import { SERIES_COLORS } from '../../../shared/chart_theme.js?v=20261031-gray-v1';
import { trackInvestmentNav } from '../../../investment_startup.js?v=20261031-gray-v1';
import { normalizeInvestmentNavRows } from './legacy_model.js?v=20261031-investment-charts-v1';
import { createInvestmentChartState } from './state.js?v=20261031-investment-charts-v1';
import { RETURN_SECTION_SPECS, readInvestmentNav, readInvestmentReturnSeries } from './requests.js?v=20261031-investment-charts-v1';
import { createInvestmentChartRenderer } from './renderer.js?v=20261031-investment-charts-v1';

const owners = new WeakMap();

export function createInvestmentChartController({ root = document, host = window, presenters, ChartClass = globalThis.Chart }) {
  owners.get(root)?.dispose();
  const state = createInvestmentChartState(host);
  const renderer = createInvestmentChartRenderer({ root, host, state, presenters, ChartClass });
  const sectionSpecs = RETURN_SECTION_SPECS.map((spec, index) => ({ ...spec, color: SERIES_COLORS[index] }));
  let disposed = false;
  let navPromise = null;
  let returnPromise = null;
  let returnGeneration = 0;
  const current = () => !disposed && owners.get(root) === controller;
  const publishCharts = () => {
    if (!current()) return;
    if (renderer.navChart) host.__investmentNavChart = renderer.navChart;
    if (renderer.returnChart) host.__investmentReturnChart = renderer.returnChart;
  };

  function loadNavOnce() {
    if (!current()) return Promise.resolve(null);
    if (!navPromise) {
      navPromise = trackInvestmentNav(readInvestmentNav()
        .then(response => {
          if (!current()) return null;
          state.navRows = normalizeInvestmentNavRows(response.rows || []);
          state.navChart = renderer.renderNav(state.navRows, state.activeWindow, state.isMobile);
          if (!renderer.returnChart) renderer.renderWindowToggle(null, state);
          publishCharts();
          return state.navChart;
        })
        .catch(error => {
          if (!current()) return null;
          navPromise = null;
          console.error('NAV曲线加载失败', error);
          throw error;
        }));
    }
    return navPromise;
  }

  async function renderReturns() {
    if (!current()) return null;
    const el = root.getElementById ? root.getElementById('investmentReturnChart') : root.querySelector('#investmentReturnChart');
    if (!el) return null;
    loadNavOnce().catch(() => null);
    const generation = ++returnGeneration;
    const response = await readInvestmentReturnSeries();
    if (!current() || generation !== returnGeneration) return null;
    const result = renderer.renderReturns({ ...response, sectionSpecs });
    publishCharts();
    return result;
  }

  function loadReturnsOnce() {
    if (!current()) return Promise.resolve(null);
    if (!returnPromise) {
      returnPromise = renderReturns().catch(error => {
        if (!current()) return null;
        returnPromise = null;
        (root.body || root).dataset.investmentReturnChartError = String(error);
        console.error('收益曲线加载失败', error);
        throw error;
      });
    }
    return returnPromise;
  }

  function onPageHide(event) {
    // BFCache retains this document and its state; permanent exits release ownership.
    if (!event.persisted) controller.dispose();
  }

  const controller = {
    state,
    loadNavOnce,
    renderReturns,
    loadReturnsOnce,
    applyWindow(chart, givenState, key, persist = true) {
      if (current() && givenState === state) renderer.applyWindow(chart, state, key, persist);
    },
    get disposed() { return disposed; },
    dispose() {
      if (disposed) return;
      disposed = true;
      returnGeneration += 1;
      host.removeEventListener?.('pagehide', onPageHide);
      renderer.dispose();
      if (owners.get(root) === controller) owners.delete(root);
    },
  };
  owners.set(root, controller);
  host.addEventListener?.('pagehide', onPageHide);
  return controller;
}
