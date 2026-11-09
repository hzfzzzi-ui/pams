import { createReadScope, isReadCancelled } from '../../shared/read_scope.js';
import { replaceContentAddress } from '../../app/routes.js';
import { createBottomSheet } from '../../components/bottom_sheet.js';
import { setActiveNav } from '../../ui.js';
import { createLedgerState, restoreLedgerViewState, LEDGER_PANEL_HASH, LEDGER_HASH_PANEL, RETIRED_ACCOUNT_HASHES } from './state.js';
import { createLedgerLegacyModel } from './legacy_model.js';
import { createLedgerTemplates } from './templates.js';
import { createLedgerDom, createLedgerEvents, createLedgerSheets } from './runtime.js';
import { createLedgerOverview } from './overview.js';
import { createLedgerLists } from './lists.js';
import { createLedgerEditor } from './editor.js';
import { createLedgerTransfers } from './transfers.js';
import { readLedgerWorkbench } from './requests.js';

const owners = new WeakMap();

export function createLedgerController({ root = document, host = window, sheetService, ChartClass = host.Chart, viewState } = {}) {
  owners.get(root)?.dispose();
  const reads = createReadScope({AbortController: host?.AbortController});
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
  const { $, clearError } = dom;
  const showError = error => {if (current() && !isReadCancelled(error)) dom.showError(error);};
  const events = createLedgerEvents(current);
  const sheets = createLedgerSheets(sheetService || createBottomSheet(root.ownerDocument || root), events, current);
  const overview = createLedgerOverview({ root, host, state, model, dom, ChartClass });
  const { renderOverviewPeriodButtons, renderLedgerMobileHero, renderOverviewCategoryInsight, renderRules, renderTrendChart } = overview;
  const transfers = createLedgerTransfers({ root, state, model, dom, events, reads, current, reloadWorkbench: loadWorkbench });
  const editor = createLedgerEditor({ model, templates, sheets, dom, current, reload: loadWorkbench });
  const listView = createLedgerLists({ root, host, state, model, templates, dom, events, sheets, showDetails });
  const { renderRecentSearchButton, openRecentSearchSheet, renderRecentAccountScope, renderRecentPeriodButton,
    bindRecentPeriod, closeRecentPeriod, renderRecentSortButton, openRecentSortSheet, renderRecentRows } = listView;

function showDetails(title, html) {
  const session = sheets.open(title, html);
  if (session) session.listen(session.body, 'click', event => editor.onEditClick(event, session.body));
}

async function loadWorkbench() {
  if (!current()) return null;
  const generation = ++readGeneration;
  clearError();
  try {
    const { data, accounts } = await reads.run('workbench', signal => readLedgerWorkbench({signal}));
    if (!current() || generation !== readGeneration) return null;
    state.data = data;
    state.accounts = accounts;
    renderAll(data);
    if (state.activePanel === 'transfer') transfers.render();
    return data;
  } catch (error) {
    if (isReadCancelled(error)) throw error;
    if (!current() || generation !== readGeneration) return null;
    throw error;
  }
}

function renderAll(data) {
  renderOverviewPeriodButtons();
  renderLedgerMobileHero(data);
  renderOverviewCategoryInsight(data);
  renderRules(data.classification_rules || []);
  renderTrendChart(data);
  renderRecentSearchButton();
  renderRecentPeriodButton();
  renderRecentSortButton();
  renderRecentAccountScope();
  renderRecentRows();
}

function selectTrendMode(mode) {
  state.trendMode = mode;
  $('trendMonthly')?.setAttribute('aria-pressed', String(mode === 'monthly'));
  $('trendWeekly')?.setAttribute('aria-pressed', String(mode === 'weekly'));
}

function bindEvents() {
  transfers.bind();
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
    selectTrendMode('monthly');
    if (state.data) renderTrendChart(state.data);
  });
  events.listen('page', $('trendWeekly'), 'click', () => {
    selectTrendMode('weekly');
    if (state.data) renderTrendChart(state.data);
  });
  root.querySelectorAll('[data-ledger-flow-filter]').forEach((btn) => {
    events.listen('page', btn, 'click', () => {
      state.recentFlowFilter = btn.dataset.ledgerFlowFilter || '';
      root.querySelectorAll('[data-ledger-flow-filter]').forEach((item) => {
        const active = item === btn;
        item.classList.toggle('active', active);
        item.setAttribute('aria-pressed', String(active));
      });
      renderRecentRows();
    });
  });
  bindRecentPeriod();
  const recentSearchToggle = $('recentSearchToggle');
  events.listen('page', recentSearchToggle, 'click', () => {
    openRecentSearchSheet();
  });
  events.listen('page', $('recentSortToggle'), 'click', () => {
    openRecentSortSheet();
  });
  events.listen('page', root, 'click', event => editor.onEditClick(event, root));

  renderRecentSearchButton();
  renderRecentPeriodButton();
  renderRecentSortButton();
}

function setLedgerPanel(panel = 'overview') {
  state.activePanel = LEDGER_PANEL_HASH[panel] ? panel : 'overview';
  if (state.activePanel !== 'recent') closeRecentPeriod();
  (root.body || root).dataset.ledgerTab = state.activePanel;
  root.querySelectorAll('[data-ledger-panel]').forEach((group) => {
    group.hidden = group.dataset.ledgerPanel !== state.activePanel;
  });
  root.querySelectorAll('.ledgerAnchorNav a').forEach((link) => {
    const active = link.dataset.ledgerTab === state.activePanel;
    link.classList.toggle('is-active', active);
    if (active) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });
  if (state.activePanel === 'transfer') transfers.load();
}

function bindLedgerAnchors() {
  root.querySelectorAll('a[data-ledger-tab]').forEach((link) => {
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
  function selectPanelFromHash() {
    if (RETIRED_ACCOUNT_HASHES.has(host.location.hash)) {
      replaceContentAddress(`${host.location.pathname}${host.location.search}${LEDGER_PANEL_HASH.overview}`);
    }
    setLedgerPanel(LEDGER_HASH_PANEL[host.location.hash] || 'overview');
  }
  events.listen('page', host, 'hashchange', selectPanelFromHash);
  selectPanelFromHash();
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
        const active = (btn.dataset.ledgerFlowFilter || '') === state.recentFlowFilter;
        btn.classList.toggle('active', active);
        btn.setAttribute('aria-pressed', String(active));
      });
      selectTrendMode(state.trendMode);
      events.listen('page', host, 'pagehide', () => controller.dispose());
      initialLoad = loadWorkbench().catch(error => { if (current()) showError(error); });
      return initialLoad;
    },
    get disposed() { return disposed; },
    dispose() {
      if (disposed) return;
      disposed = true;
      closeRecentPeriod();
      reads.dispose();
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
