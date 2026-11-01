import {
  fmtCny, fmtSignedCny, formatNumber, formatPercentRate,
  esc, fmtSignedPctFromRate
} from './summary_legacy_model.js?v=20261102-incomplete-scope-v1';
import {
  createSummaryView
} from './summary_view.js?v=20261102-analysis-unknown-v1';
import {
  applyLegacySummaryPayload
} from './legacy_summary_projection.js?v=20261102-incomplete-scope-v1';
import {
  readSummaryInputs
} from './requests.js?v=20261031-modules-v1';
import {
  createScope, createScopedDocument, createOwnedDetails, createOwnedError
} from './scope.js?v=20261031-modules-v1';
import {
  acquireClosedPositions
} from './closed_positions.js?v=20261031-modules-v1';
import {
  createCategoryChartLoader
} from './category_charts.js?v=20261031-modules-v1';
import {
  CATEGORY_SECTION_IDS, CATEGORY_RETURN_CHART_SPECS, INVESTMENT_PHONE_QUERY
} from './constants.js?v=20261031-modules-v1';
import { createInvestmentChartController } from '../charts/controller.js?v=20261102-analysis-unknown-v1';
import { replaceContentAddress } from '../../../app/routes.js?v=20261031-modules-v1';

const mounted = new WeakMap();
export function mount(root, context) {
  if (mounted.has(root)) return mounted.get(root);
  const window = context.host || root.ownerDocument.defaultView;
  const document = createScopedDocument(root);
  const errorView = createOwnedError(document.getElementById('err'));
  const scope = createScope(window, context.get);
  const closed = acquireClosedPositions(root, window);
  const { showClosedPositions, withClosedParam } = closed;
  const details = createOwnedDetails(context.showDetails, context.closeDetails);
  const view = createSummaryView({document, host: window, showDetails: details.show, scope});
  const { statusBadge, updateMobilePortfolioHero, TopContributionPanel, renderReturnMethodBar } = view;
  const afterInvestmentNav = (task, tab) => context.afterInvestmentNav(() => scope.active ? task() : null, tab);
  const trackInvestmentHoldings = context.trackInvestmentHoldings;
  const Chart = scope.Chart(context.ChartClass || window.Chart);
  const createReturnRateChart = createCategoryChartLoader({document, host: {}, get: scope.get, Chart});
  const loadedCategoryReturnCharts = new Set();
  let investmentCharts = null, investmentChartsDisposed = false, analysisHeavyLoadersStarted = false;
function bindContributionTabs() {
  scope.listen(root, 'click', (event) => {
    const btn = event.target?.closest?.('[data-contribution-tab]');
    if (!btn) return;
    const root = btn.closest('.contributionTabs');
    if (!root) return;
    const key = btn.dataset.contributionTab;
    root.querySelectorAll('[data-contribution-tab]').forEach(el => el.classList.toggle('active', el === btn));
    root.querySelectorAll('[data-contribution-pane]').forEach(el => { el.hidden = el.dataset.contributionPane !== key; });
  });
}

function bindInvestmentPageTabs() {
  const links = Array.from(document.querySelectorAll('[data-invest-tab]'));
  const panels = Array.from(document.querySelectorAll('[data-invest-panel]'));
  if (!links.length || !panels.length) return;

  const media = window.matchMedia(INVESTMENT_PHONE_QUERY);
  let activeTab = 'holdings';

  const tabFromHash = (hash) => {
    const key = String(hash || '').replace(/^#/, '');
    if (
      key === 'return-trend' ||
      key === 'category-details' ||
      key === 'return-attribution' ||
      key === 'trade-attribution-analysis' ||
      key === 'asset-master-panel' ||
      CATEGORY_SECTION_IDS.includes(key)
    ) return 'analysis';
    if (key === 'investment-ledger') return 'transactions';
    if (key === 'portfolio-risk-disclosure' || key === 'lookthrough-exposure') return 'risk';
    if (key === 'portfolio-constraints') return 'allocation';
    return 'holdings';
  };

  const activeCategoryTarget = () => (
    document.querySelector('[data-category-target].active')?.dataset.categoryTarget || CATEGORY_SECTION_IDS[0]
  );

  const syncCategoryVisibility = () => {
    const target = activeCategoryTarget();
    for (const id of CATEGORY_SECTION_IDS) {
      const el = document.getElementById(id);
      if (el) el.hidden = media.matches ? (activeTab !== 'analysis' || id !== target) : id !== target;
    }
  };

  const setActive = (nextTab, options = {}) => {
    activeTab = nextTab || 'holdings';
    document.body.dataset.investTab = activeTab;
    for (const link of links) {
      const active = link.dataset.investTab === activeTab;
      link.classList.toggle('is-active', active);
      if (active) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    }

    for (const panel of panels) {
      if (panel.classList.contains('investmentCategorySection')) continue;
      panel.hidden = media.matches ? panel.dataset.investPanel !== activeTab : false;
    }
    syncCategoryVisibility();

    if (options.updateHash) {
      const href = links.find(link => link.dataset.investTab === activeTab)?.getAttribute('href');
      if (href) replaceContentAddress(href);
    }
  };

  for (const link of links) {
    scope.listen(link, 'click', (event) => {
      if (!media.matches) return;
      event.preventDefault();
      setActive(link.dataset.investTab, { updateHash: true });
    });
  }

  scope.listen(window, 'hashchange', () => {
    const key = String(window.location.hash || '').replace(/^#/, '');
    if (key === 'category-details' || CATEGORY_SECTION_IDS.includes(key)) openCategoryDetailShell();
    setActive(tabFromHash(window.location.hash));
  });
  scope.listen(media, 'change', () => setActive(activeTab));
  scope.listen(root, 'click', (event) => {
    if (event.target?.closest?.('[data-category-target]')) {
      scope.timeout(syncCategoryVisibility, 0);
    }
  });

  setActive(tabFromHash(window.location.hash));
}

function runWhenInvestmentAnalysisVisible(callback, options = {}) {
  const media = window.matchMedia(INVESTMENT_PHONE_QUERY);
  let started = false;
  const delayMs = Number(options.delayMs || 0);
  const start = () => {
    if (started) return;
    started = true;
    scope.timeout(() => callback(), delayMs);
  };
  const maybeStart = () => {
    if (started) return;
    if (!media.matches || document.body.dataset.investTab === 'analysis') start();
  };

  scope.listen(window, 'hashchange', maybeStart);
  scope.listen(media, 'change', maybeStart);
  scope.observe(root, maybeStart, { attributes: true, attributeFilter: ['data-invest-tab'] });
  scope.timeout(maybeStart, media.matches ? 0 : 1200);
}

function canLoadAnalysisDetail() {
  return document.body.dataset.investTab === 'analysis';
}

function isInvestmentAnalysisHash(hash) {
  const key = String(hash || '').replace(/^#/, '');
  return (
    key === 'return-trend' ||
    key === 'return-attribution' ||
    key === 'trade-attribution-analysis' ||
    key === 'asset-master-panel'
  );
}

function loadCategoryReturnChart(spec) {
  if (!scope.active || !spec || loadedCategoryReturnCharts.has(spec.chartId) || !canLoadAnalysisDetail()) return;
  loadedCategoryReturnCharts.add(spec.chartId);
  createReturnRateChart(spec.chartId, spec.url, spec.color, spec.title, 'daily').catch(() => {
    loadedCategoryReturnCharts.delete(spec.chartId);
  });
}

function categoryDetailShell() {
  return document.getElementById('category-detail-shell');
}

function isCategoryDetailOpen() {
  const shell = categoryDetailShell();
  return Boolean(shell && !shell.hidden && shell.open);
}

function openCategoryDetailShell() {
  const shell = categoryDetailShell();
  if (shell && !shell.hidden) shell.open = true;
}

function loadVisibleCategoryReturnChart() {
  if (!canLoadAnalysisDetail() || !isCategoryDetailOpen()) return;
  const spec = CATEGORY_RETURN_CHART_SPECS.find(item => {
    const section = document.getElementById(item.targetId);
    return section && section.hidden === false;
  }) || CATEGORY_RETURN_CHART_SPECS[0];
  loadCategoryReturnChart(spec);
}

function loadAllCategoryReturnCharts() {
  if (!canLoadAnalysisDetail() || !isCategoryDetailOpen()) return;
  CATEGORY_RETURN_CHART_SPECS.forEach(loadCategoryReturnChart);
}

function queueVisibleCategoryReturnChart(delayMs = 0) {
  scope.timeout(loadVisibleCategoryReturnChart, delayMs);
}

function getInvestmentCharts() {
  if (investmentChartsDisposed) return null;
  if (!investmentCharts) {
    investmentCharts = createInvestmentChartController({
      root,
      host: window,
      ChartClass: context.ChartClass || window.Chart,
      presenters: { esc, statusBadge, formatNumber, formatPercentRate, fmtCny, fmtSignedCny, fmtSignedPctFromRate, TopContributionPanel },
    });
  }
  return investmentCharts;
}

function getInvestmentChartState() {
  return getInvestmentCharts()?.state || null;
}

function loadInvestmentNavChartOnce() {
  return getInvestmentCharts()?.loadNavOnce() || Promise.resolve(null);
}

function renderInvestmentReturnChart() {
  return getInvestmentCharts()?.renderReturns() || Promise.resolve(null);
}

function loadInvestmentReturnChartOnce() {
  return getInvestmentCharts()?.loadReturnsOnce() || Promise.resolve(null);
}

function applyInvestmentReturnWindow(chart, state, key, persist = true) {
  getInvestmentCharts()?.applyWindow(chart, state, key, persist);
}

function disposeInvestmentCharts() {
  investmentChartsDisposed = true;
  investmentCharts?.dispose();
}

function setupAnalysisHeavyLoaders() {
  if (!scope.active || analysisHeavyLoadersStarted) return;
  analysisHeavyLoadersStarted = true;
  document.body.dataset.analysisHeavyLoadersStarted = '1';
  const panel = document.querySelector('.returnChartPanel');
  const chartWrap = panel?.querySelector('.chartWrap');
  const toggle = document.getElementById('investmentReturnToggle');
  if (chartWrap) chartWrap.hidden = false;
  if (toggle) toggle.hidden = false;
  loadInvestmentNavChartOnce().catch(() => null);
  afterInvestmentNav(() => loadInvestmentReturnChartOnce()).catch(() => null);
  afterInvestmentNav(() => queueVisibleCategoryReturnChart(900)).catch(() => null);
}

function CategoryDetailTabs() {
  const buttons = Array.from(document.querySelectorAll('[data-category-target]'));
  const shell = categoryDetailShell();
  if (shell?.hidden) return;
  if (shell) {
    scope.listen(shell, 'toggle', () => {
      if (shell.open) queueVisibleCategoryReturnChart(120);
    });
  }
  if (!buttons.length) return;
  const show = (targetId, options = {}) => {
    if (options.openShell) openCategoryDetailShell();
    for (const id of CATEGORY_SECTION_IDS) {
      const el = document.getElementById(id);
      if (el) el.hidden = id !== targetId;
    }
    for (const btn of buttons) {
      btn.classList.toggle('active', btn.dataset.categoryTarget === targetId);
    }
    queueVisibleCategoryReturnChart(150);
  };
  const hashTarget = window.location.hash ? window.location.hash.slice(1) : '';
  const shouldOpenShell = hashTarget === 'category-details' || CATEGORY_SECTION_IDS.includes(hashTarget);
  if (shouldOpenShell) {
    openCategoryDetailShell();
  }
  const initial = CATEGORY_SECTION_IDS.includes(hashTarget) ? hashTarget : CATEGORY_SECTION_IDS.includes(context.state?.category) ? context.state.category : CATEGORY_SECTION_IDS[0];
  if (context.state?.categoryOpen) openCategoryDetailShell();
  show(initial);
  scope.listen(window, 'hashchange', () => {
    const key = window.location.hash ? window.location.hash.slice(1) : '';
    if (key === 'category-details') openCategoryDetailShell();
    if (CATEGORY_SECTION_IDS.includes(key)) show(key, { openShell: true });
  });
  for (const btn of buttons) {
    scope.listen(btn, 'click', () => {
      show(btn.dataset.categoryTarget, { openShell: true });
      const anchor = document.getElementById('category-details');
      if (anchor) anchor.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  }
}

function promoteHoldingsTable() {
  if (window.matchMedia(INVESTMENT_PHONE_QUERY).matches) return;
  const hero = document.getElementById('investment-conclusion');
  const holdings = document.getElementById('holdings-smart-table');
  if (hero && holdings && hero.nextElementSibling !== holdings) {
    hero.after(holdings);
  }
}

  scope.listen(window, 'pams:holding-group-change', event => {
    if (event.detail?.sourceRoot && event.detail.sourceRoot !== root) return;
    updateMobilePortfolioHero(event.detail?.group);
  });
  async function load() {
    try {
      const payloads = await trackInvestmentHoldings(afterInvestmentNav(() => readSummaryInputs({
        get: scope.get, withClosedParam, loadInvestmentSummaryFresh: () => context.loadInvestmentSummaryFresh({signal: scope.signal}),
      }), 'holdings'));
      if (!scope.active) return;
      applyLegacySummaryPayload(payloads, {document, Chart, showClosedPositions, view});
      if (isInvestmentAnalysisHash(window.location.hash)) scope.timeout(() => setupAnalysisHeavyLoaders(), 500);
    } catch (error) {
      if (!scope.active) return;
      errorView.show(error);
      console.error(error);
    } finally {
      if (scope.active && isInvestmentAnalysisHash(window.location.hash)) scope.timeout(() => setupAnalysisHeavyLoaders(), 800);
    }
  }
  const instance = {
    ready: null,
    snapshot: () => ({ category: document.querySelector('[data-category-target].active')?.dataset.categoryTarget,
      categoryOpen: document.getElementById('category-detail-shell')?.open }),
    dispose() {
      if (!scope.active) return savedOnExit;
      savedOnExit = instance.snapshot();
      scope.dispose(); disposeInvestmentCharts(); view.dispose(); details.dispose(); closed.release(); errorView.dispose();
      if (mounted.get(root) === instance) mounted.delete(root);
      return savedOnExit;
    },
    get disposed() { return !scope.active; },
  };
  let savedOnExit;
  mounted.set(root, instance);
  context.bootstrap?.();
  renderReturnMethodBar(); CategoryDetailTabs(); bindContributionTabs(); bindInvestmentPageTabs(); promoteHoldingsTable();
  if (document.body.dataset.investTab === 'analysis') loadInvestmentNavChartOnce().catch(() => null);
  runWhenInvestmentAnalysisVisible(() => setupAnalysisHeavyLoaders());
  instance.ready = load();
  return instance;
}
export function dispose(root) { return mounted.get(root)?.dispose(); }
