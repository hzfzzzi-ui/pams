import { replaceContentAddress } from '../../app/routes.js?v=20261031-ui-v1';
import { createBottomSheet } from '../../components/bottom_sheet.js?v=20261031-ui-v1';
import { setActiveNav } from '../../ui.js?v=20261031-ui-v1';
import { createLedgerState, restoreLedgerViewState, LEDGER_PANEL_HASH, LEDGER_HASH_PANEL } from './state.js?v=20261031-ledger-modules-v1';
import { createLedgerLegacyModel } from './legacy_model.js?v=20261031-ledger-modules-v1';
import { createLedgerTemplates } from './templates.js?v=20261031-ledger-modules-v1';
import { createLedgerDom, createLedgerEvents, createLedgerSheets } from './runtime.js?v=20261031-ledger-modules-v1';
import { createLedgerOverview } from './overview.js?v=20261031-ledger-modules-v1';
import { createLedgerLists } from './lists.js?v=20261031-ledger-modules-v1';
import { createLedgerEditor } from './editor.js?v=20261031-ledger-modules-v1';
import { readLedgerWorkbench } from './requests.js?v=20261031-ledger-modules-v1';

const owners = new WeakMap();

export function createLedgerController({ root = document, host = window, sheetService, ChartClass = host.Chart, viewState } = {}) {
  owners.get(root)?.dispose();
  let disposed = false;
  let initialized = false;
  let initialLoad = null;
  let readGeneration = 0;
  const current = () => !disposed && owners.get(root) === controller;
  const state = createLedgerState();
  restoreLedgerViewState(state, viewState);
  const model = createLedgerLegacyModel(state);
  const templates = createLedgerTemplates({ state, model });
  const dom = createLedgerDom(root);
  const { $, showError, clearError } = dom;
  const { currentAccountSummary, currentOverviewTransferSummary } = model;
  const events = createLedgerEvents(current);
  const sheets = createLedgerSheets(sheetService || createBottomSheet(root.ownerDocument || root), events, current);
  const overview = createLedgerOverview({ root, host, state, model, dom, ChartClass });
  const { renderOverviewPeriodButtons, renderOverviewFlowButtons, renderKpis, renderLedgerMobileHero, renderCategoryBars, renderOverviewCategoryPieFallback, renderOverviewCategoryPie, renderOverviewCategoryInsight, renderCashflowBridge, renderTransferSummary, renderRules, renderTrendChart } = overview;
  const editor = createLedgerEditor({ model, templates, sheets, dom, current, reload: loadWorkbench });
  const listView = createLedgerLists({ root, host, state, model, templates, dom, events, sheets, replaceContentAddress, setLedgerPanel, showDetails });
  const { renderRecentSearchButton, openRecentSearchSheet, renderRecentAccountScope, openRecentForAccount, renderRecentPeriodButtons, renderRecentSortButton, openRecentSortSheet, renderAccountFlowSummary, renderRows, renderRecentRows } = listView;

function showDetails(title, html) {
  const session = sheets.open(title, html);
  if (session) session.listen(session.body, 'click', event => editor.onEditClick(event, session.body));
}

async function loadWorkbench() {
  if (!current()) return null;
  const generation = ++readGeneration;
  clearError();
  try {
    const { data, accounts } = await readLedgerWorkbench();
    if (!current() || generation !== readGeneration) return null;
    state.data = data;
    state.accounts = accounts;
    renderAll(data);
    return data;
  } catch (error) {
    if (!current() || generation !== readGeneration) return null;
    throw error;
  }
}

function renderAll(data) {
  renderOverviewPeriodButtons();
  renderLedgerMobileHero(data);
  renderKpis(data);
  renderAccountFlowSummary(currentAccountSummary(data));
  renderOverviewCategoryInsight(data);
  renderTransferSummary(currentOverviewTransferSummary(data));
  renderRules(data.classification_rules || []);
  renderTrendChart(data);
  renderCashflowBridge(data);
  renderRecentSearchButton();
  renderRecentPeriodButtons();
  renderRecentSortButton();
  renderRecentAccountScope();
  renderRecentRows();
}

function bindEvents() {
  bindLedgerAnchors();
  events.listen('page', $('btnReloadLedger'), 'click', () => loadWorkbench().catch(showError));
  root.querySelectorAll('[data-overview-period]').forEach((btn) => {
    events.listen('page', btn, 'click', () => {
      state.overviewPeriod = btn.dataset.overviewPeriod || 'ytd';
      if (state.data) renderAll(state.data);
    });
  });
  root.querySelectorAll('[data-overview-flow]').forEach((btn) => {
    events.listen('page', btn, 'click', () => {
      state.overviewFlow = btn.dataset.overviewFlow || 'income';
      if (state.data) renderOverviewCategoryInsight(state.data);
    });
  });
  events.listen('page', $('trendMonthly'), 'click', () => {
    state.trendMode = 'monthly';
    $('trendMonthly')?.classList.add('active');
    $('trendWeekly')?.classList.remove('active');
    if (state.data) renderTrendChart(state.data);
  });
  events.listen('page', $('trendWeekly'), 'click', () => {
    state.trendMode = 'weekly';
    $('trendWeekly')?.classList.add('active');
    $('trendMonthly')?.classList.remove('active');
    if (state.data) renderTrendChart(state.data);
  });
  root.querySelectorAll('[data-ledger-flow-filter]').forEach((btn) => {
    events.listen('page', btn, 'click', () => {
      state.recentFlowFilter = btn.dataset.ledgerFlowFilter || '';
      root.querySelectorAll('[data-ledger-flow-filter]').forEach((item) => {
        item.classList.toggle('active', item === btn);
      });
      renderRecentRows();
    });
  });
  root.querySelectorAll('[data-recent-period]').forEach((btn) => {
    events.listen('page', btn, 'click', () => {
      state.recentPeriod = btn.dataset.recentPeriod || 'month';
      renderRecentPeriodButtons();
      renderRecentAccountScope();
      renderRecentRows();
    });
  });
  const recentSearchToggle = $('recentSearchToggle');
  events.listen('page', recentSearchToggle, 'click', () => {
    openRecentSearchSheet();
  });
  events.listen('page', $('recentSortToggle'), 'click', () => {
    openRecentSortSheet();
  });
  events.listen('page', root, 'click', event => editor.onEditClick(event, root));

  renderRecentSearchButton();
  renderRecentSortButton();
}

function setLedgerPanel(panel = 'overview') {
  state.activePanel = LEDGER_PANEL_HASH[panel] ? panel : 'overview';
  (root.body || root).dataset.ledgerTab = state.activePanel;
  root.querySelectorAll('[data-ledger-panel]').forEach((group) => {
    group.hidden = group.dataset.ledgerPanel !== state.activePanel;
  });
  root.querySelectorAll('.ledgerAnchorNav a').forEach((link) => {
    link.classList.toggle('is-active', link.dataset.ledgerTab === state.activePanel);
  });
}

function bindLedgerAnchors() {
  root.querySelectorAll('.ledgerAnchorNav a').forEach((link) => {
    events.listen('page', link, 'click', (event) => {
      event.preventDefault();
      const panel = link.dataset.ledgerTab || 'overview';
      const nextHash = LEDGER_PANEL_HASH[panel] || LEDGER_PANEL_HASH.overview;
      if (host.location.hash !== nextHash) {
        replaceContentAddress(`${host.location.pathname}${host.location.search}${nextHash}`);
      }
      setLedgerPanel(panel);
      host.scrollTo({ top: 0, behavior: 'smooth' });
    });
  });
  events.listen('page', host, 'hashchange', () => {
    setLedgerPanel(LEDGER_HASH_PANEL[host.location.hash] || 'overview');
  });
  setLedgerPanel(LEDGER_HASH_PANEL[host.location.hash] || 'overview');
}

  const controller = {
    state,
    loadWorkbench,
    init() {
      if (!current()) return Promise.resolve(null);
      if (initialized) return initialLoad;
      initialized = true;
      setActiveNav();
      bindEvents();
      root.querySelectorAll('[data-ledger-flow-filter]').forEach(btn => {
        btn.classList.toggle('active', (btn.dataset.ledgerFlowFilter || '') === state.recentFlowFilter);
      });
      $('trendMonthly')?.classList.toggle('active', state.trendMode === 'monthly');
      $('trendWeekly')?.classList.toggle('active', state.trendMode === 'weekly');
      events.listen('page', host, 'pagehide', () => controller.dispose());
      initialLoad = loadWorkbench().catch(error => { if (current()) showError(error); });
      return initialLoad;
    },
    get disposed() { return disposed; },
    dispose() {
      if (disposed) return;
      disposed = true;
      readGeneration += 1;
      events.dispose();
      sheets.dispose();
      overview.dispose();
      if (owners.get(root) === controller) owners.delete(root);
    },
  };
  owners.set(root, controller);
  return controller;
}
