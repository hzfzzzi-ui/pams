import { createReadScope, isReadCancelled } from '../../../shared/read_scope.js';
import { SERIES_COLORS } from '../../../shared/chart_theme.js';
import { trackInvestmentNav } from '../../../investment_startup.js';
import { normalizeInvestmentNavRows, returnRateForChart } from './legacy_model.js';
import { createInvestmentChartState } from './state.js';
import { RETURN_SECTION_SPECS, readInvestmentAnalysisView, readInvestmentNav, readInvestmentReturnSeries } from './requests.js';
import { createInvestmentChartRenderer } from './renderer.js';

const owners = new WeakMap();
const MARKET_RETURN_COLORS = new Map([
  ['stocks', '#ff6269'],
  ['a_fund', '#54cdb2'],
  ['precious_metal', '#f4c34f'],
  ['wealth', '#a78bfa'],
]);

export function createInvestmentChartController({ root = document, host = window, presenters, ChartClass = globalThis.Chart, loadChartClass = () => ChartClass, onNavUpdate }) {
  owners.get(root)?.dispose();
  const reads = createReadScope({AbortController: host?.AbortController});
  const state = createInvestmentChartState(host);
  function publishWindow(labels) {
    if (!labels?.length) return;
    const detail = { windowKey: state.activeWindow, startDate: labels[0], endDate: labels.at(-1) };
    host.__investmentReturnWindow = detail;
    host.dispatchEvent?.(new host.CustomEvent('pams:return-window-change', { detail }));
  }
  const renderer = createInvestmentChartRenderer({ root, host, state, presenters, ChartClass, onWindowChange: publishWindow });
  const sectionSpecs = RETURN_SECTION_SPECS.map((spec, index) => ({
    ...spec,
    color: MARKET_RETURN_COLORS.get(spec.key) ?? SERIES_COLORS[index],
  }));
  let disposed = false;
  let navPromise = null;
  let returnPromise = null;
  let analysisPromise = null;
  let analysisStale = false;
  let returnGeneration = 0;
  let contributionObserver = null;
  const sectionReads = new Map();
  const current = () => !disposed && owners.get(root) === controller;
  const projectionStatus = root.getElementById?.('investmentAnalysisProjectionStatus')
    ?? root.querySelector?.('#investmentAnalysisProjectionStatus');
  function readAnalysis() {
    if (!projectionStatus) return null;
    analysisPromise ??= reads.run('analysis', signal => readInvestmentAnalysisView({signal})).then(response => {
      if (current() && response?.projection) {
        analysisStale = response.projection.stale === true;
        projectionStatus.hidden = true;
        projectionStatus.textContent = '';
      }
      return response?.data ?? null;
    });
    return analysisPromise;
  }
  const publishCharts = () => {
    if (!current()) return;
    if (renderer.navChart) host.__investmentNavChart = renderer.navChart;
    if (renderer.returnChart) host.__investmentReturnChart = renderer.returnChart;
  };

  function showNav(rows, comparisons) {
    state.navRows = rows;
    state.navComparisons = comparisons ?? {};
    state.navChart = renderer.renderNav();
    publishCharts();
    onNavUpdate?.(rows.at(-1));
  }

  async function readNav() {
    const [analysis, chartClass] = await Promise.all([readAnalysis(), loadChartClass()]);
    if (!current()) return null;
    renderer.setChartClass(chartClass);
    const cached = analysis?.nav;
    const cachedRows = normalizeInvestmentNavRows(cached?.rows || []);
    if (cachedRows.length) {
      showNav(cachedRows, cached.period_comparisons);
      const periods = cached.period_comparisons;
      if (!analysisStale && ['ytd', '1m', '1w'].every(key => periods?.[key]?.comparable_accounts)) {
        return state.navChart;
      }
    }
    const live = await reads.run('nav', signal => readInvestmentNav({signal}));
    if (!current()) return null;
    const rows = normalizeInvestmentNavRows(live?.rows || []);
    const fresh = rows.length > 0 && (!cachedRows.length || rows.at(-1).date >= cachedRows.at(-1).date);
    if (fresh || !cachedRows.length) {
      showNav(rows, live?.period_comparisons);
      if (projectionStatus) {
        projectionStatus.textContent = '';
        projectionStatus.hidden = true;
      }
    } else if (projectionStatus) {
      projectionStatus.textContent = '暂未读到更新的净资产数据，显示已保存的记录';
      projectionStatus.hidden = false;
    }
    if (!fresh) navPromise = null; // Reopening the overview can retry a failed or older direct read.
    return state.navChart;
  }

  function loadNavOnce() {
    if (!current()) return Promise.resolve(null);
    if (!navPromise) {
      navPromise = trackInvestmentNav(readNav())
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

    const generation = ++returnGeneration;
    contributionObserver?.disconnect();
    for (const key of sectionReads.keys()) reads.cancel(key);
    try {
      const view = readAnalysis();
      const legacyReturns = () => reads.run('returns', signal => readInvestmentReturnSeries({signal}));
      const [resp, analysis, chartClass] = await Promise.all([
        view?.then(data => data?.total ?? legacyReturns()) ?? legacyReturns(),
        view,
        loadChartClass(),
      ]);
      if (!current() || generation !== returnGeneration) return null;
      renderer.setChartClass(chartClass);
      sectionReads.clear();
      const sectionResponses = sectionSpecs.map(({key}) => analysis?.sections?.[key]);
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
      const contribution = root.getElementById?.('profitContributionPanel') || root.querySelector?.('#profitContributionPanel');
      state.contributionPending = Boolean(contribution && sectionResponses.some(response => !response)
        && typeof host.IntersectionObserver === 'function');
      const result = renderer.renderReturns({ resp, sectionResponses, sectionSpecs, loadSection });
      publishCharts();
      publishWindow(renderer.returnChart?.data.labels);
      if (state.contributionPending) {
        const observer = new host.IntersectionObserver(async entries => {
          if (!entries.some(entry => entry.isIntersecting)) return;
          observer.disconnect();
          for (const {key} of sectionSpecs) {
            if (!current() || generation !== returnGeneration) return;
            await loadSection(key).catch(() => false);
          }
          if (current() && generation === returnGeneration) {
            state.contributionPending = false;
            renderer.renderContribution(state.topContribution);
          }
        });
        contributionObserver = observer;
        observer.observe(contribution);
      }
      // Load the default section if the portfolio series is unavailable.
      if (!(resp.rows || []).some(row => returnRateForChart(row) != null)) {
        await loadSection('stocks');
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
      delete host.__investmentReturnWindow;
      if (owners.get(root) === controller) owners.delete(root);
    },
  };
  owners.set(root, controller);
  renderer.renderWindowToggles();
  host.addEventListener?.('pagehide', onPageHide);
  return controller;
}
