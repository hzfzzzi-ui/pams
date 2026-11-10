import { createReadScope, isReadCancelled } from '../../../shared/read_scope.js';
import { SERIES_COLORS } from '../../../shared/chart_theme.js';
import { trackInvestmentNav } from '../../../investment_startup.js';
import { normalizeInvestmentNavRows, returnRateForChart } from './legacy_model.js';
import { createInvestmentChartState } from './state.js';
import { RETURN_SECTION_SPECS, readInvestmentNav, readInvestmentReturnSeries } from './requests.js';
import { createInvestmentChartRenderer } from './renderer.js';

const owners = new WeakMap();
const MARKET_RETURN_COLORS = new Map([
  ['a_stock', '#ff6269'],
  ['us_stock', '#69aaff'],
]);

export function createInvestmentChartController({ root = document, host = window, presenters, ChartClass = globalThis.Chart, loadChartClass = () => ChartClass }) {
  owners.get(root)?.dispose();
  const reads = createReadScope({AbortController: host?.AbortController});
  const state = createInvestmentChartState(host);
  const renderer = createInvestmentChartRenderer({ root, host, state, presenters, ChartClass });
  const sectionSpecs = RETURN_SECTION_SPECS.map((spec, index) => ({
    ...spec,
    color: MARKET_RETURN_COLORS.get(spec.key) ?? SERIES_COLORS[index],
  }));
  let disposed = false;
  let navPromise = null;
  let returnPromise = null;
  let returnGeneration = 0;
  let contributionObserver = null;
  const sectionReads = new Map();
  const current = () => !disposed && owners.get(root) === controller;
  const publishCharts = () => {
    if (!current()) return;
    if (renderer.navChart) host.__investmentNavChart = renderer.navChart;
    if (renderer.returnChart) host.__investmentReturnChart = renderer.returnChart;
  };

  function loadNavOnce() {
    if (!current()) return Promise.resolve(null);
    if (!navPromise) {
      const navRead = trackInvestmentNav(reads.run('nav', signal => readInvestmentNav({signal})));
      navPromise = Promise.all([navRead, loadChartClass()])
        .then(([response, chartClass]) => {
          if (!current()) return null;
          renderer.setChartClass(chartClass);
          state.navRows = normalizeInvestmentNavRows(response.rows || []);
          state.navChart = renderer.renderNav(state.navRows, state.activeWindow, state.isMobile);
          if (!renderer.returnChart) renderer.renderWindowToggle(null, state);
          publishCharts();
          return state.navChart;
        })
        .catch(error => {
          if (!current() || isReadCancelled(error)) return null;
          navPromise = null;
          console.error('NAV曲线加载失败', error);
          throw error;
        });
    }
    return navPromise;
  }

  async function renderReturns() {
    if (!current()) return null;
    const el = root.getElementById ? root.getElementById('investmentReturnChart') : root.querySelector('#investmentReturnChart');
    if (!el) return null;
    loadNavOnce().catch(() => null);
    const generation = ++returnGeneration;
    contributionObserver?.disconnect();
    for (const key of sectionReads.keys()) reads.cancel(key);
    try {
      const [resp, chartClass] = await Promise.all([
        reads.run('returns', signal => readInvestmentReturnSeries({signal})),
        loadChartClass(),
      ]);
      if (!current() || generation !== returnGeneration) return null;
      renderer.setChartClass(chartClass);
      sectionReads.clear();
      const sectionResponses = [];
      function loadSection(key) {
        const index = sectionSpecs.findIndex(spec => spec.key === key);
        if (index < 0) return Promise.resolve(false);
        if (sectionResponses[index]) return Promise.resolve(true);
        if (!sectionReads.has(key)) {
          const promise = reads.run(key, signal => readInvestmentReturnSeries({signal, section: sectionSpecs[index]}))
            .then(data => {
              if (!current() || generation !== returnGeneration) return false;
              sectionResponses[index] = data;
              renderer.updateReturnSeries({ resp, sectionResponses, sectionSpecs });
              return true;
            })
            .catch(error => {
              if (isReadCancelled(error)) return false;
              throw error;
            })
            .finally(() => {
              if (sectionReads.get(key) === promise) sectionReads.delete(key);
            });
          sectionReads.set(key, promise);
        }
        return sectionReads.get(key);
      }
      const result = renderer.renderReturns({ resp, sectionResponses, sectionSpecs, loadSection });
      publishCharts();
      const contribution = root.getElementById?.('profitContributionPanel') || root.querySelector?.('#profitContributionPanel');
      if (contribution && typeof host.IntersectionObserver === 'function') {
        const observer = new host.IntersectionObserver(async entries => {
          if (!entries.some(entry => entry.isIntersecting)) return;
          observer.disconnect();
          for (const {key} of sectionSpecs) {
            if (!current() || generation !== returnGeneration) break;
            await loadSection(key).catch(() => false);
          }
        });
        contributionObserver = observer;
        observer.observe(contribution);
      }
      // Preserve the established first available A-share curve for partial totals.
      if (!(resp.rows || []).some(row => returnRateForChart(row) != null)) {
        await loadSection('a_stock');
      }
      return result;
    } catch (error) {
      if (isReadCancelled(error)) return null;
      throw error;
    }
  }

  function loadReturnsOnce() {
    if (!current()) return Promise.resolve(null);
    if (!returnPromise) {
      returnPromise = renderReturns().catch(error => {
        if (!current() || isReadCancelled(error)) return null;
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
      contributionObserver?.disconnect();
      reads.dispose();
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
