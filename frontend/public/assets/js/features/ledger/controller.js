import { createReadScope, isReadCancelled } from '../../shared/read_scope.js';
import { pageMemory, pageScroller, restorePageScroll, showRememberedData } from '../../app/page_memory.js';
import { replaceContentAddress } from '../../app/routes.js';
import { createBottomSheet } from '../../components/bottom_sheet.js';
import { setActiveNav } from '../../ui.js';
import { loadChart } from '../../shared/load_chart.js';
import { createLedgerState, restoreLedgerViewState, snapshotLedgerViewState, LEDGER_PANEL_HASH, LEDGER_HASH_PANEL, RETIRED_ACCOUNT_HASHES } from './state.js';
import { createLedgerLegacyModel } from './legacy_model.js';
import { createLedgerTemplates } from './templates.js';
import { createLedgerDom, createLedgerEvents, createLedgerSheets } from './runtime.js';
import { createLedgerOverview } from './overview.js';
import { createLedgerLists } from './lists.js';
import { createLedgerEditor } from './editor.js';
import { createLedgerTransfers } from './transfers.js';
import { ledgerStreamQuery, readLedgerStream, readLedgerWorkbench } from './requests.js';

const owners = new WeakMap();

export function createLedgerController({ root = document, host = window, sheetService, ChartClass = host.Chart, viewState } = {}) {
  owners.get(root)?.dispose();
  const reads = createReadScope({AbortController: host?.AbortController});
  let disposed = false;
  let initialized = false;
  let initialLoad = null;
  let readGeneration = 0;
  let streamGeneration = 0;
  let streamLoading = false;
  let chartsLoading = false;
  const current = () => !disposed && owners.get(root) === controller;
  const state = createLedgerState();
  const memory = pageMemory(host);
  const variant = host.location?.search || '';
  const savedView = memory?.read('view', variant);
  restoreLedgerViewState(state, viewState || savedView?.state);
  const model = createLedgerLegacyModel(state);
  const templates = createLedgerTemplates({ state, model });
  const dom = createLedgerDom(root);
  const { $, clearError } = dom;
  const refreshNote = $('ledgerViewRefresh');
  const showRefresh = status => showRememberedData(refreshNote, status);
  const restoreScroll = () => {
    const panel = LEDGER_HASH_PANEL[host.location.hash] || 'overview';
    if (savedView?.state.activePanel === panel && savedView.scroll) {
      restorePageScroll(host, savedView.scroll, current);
    }
  };
  const showError = error => {if (current() && !isReadCancelled(error)) dom.showError(error);};
  const events = createLedgerEvents(current);
  const sheets = createLedgerSheets(sheetService || createBottomSheet(root.ownerDocument || root), events, current);
  const overview = createLedgerOverview({ root, host, state, model, dom, ChartClass });
  const { renderOverviewPeriodButtons, renderLedgerMobileHero, renderTransferSummary, renderOverviewCategoryInsight, renderRules, renderTrendChart } = overview;
  const transfers = createLedgerTransfers({
    state, model, dom, events, reads, current, memory, variant, showRefresh, reloadWorkbench: loadWorkbench,
    reloadStream: () => loadStream().catch(showError),
    onReviewChange: () => {
      closeRecentPeriod();
      renderRecentPeriodButton();
    },
  });
  const editor = createLedgerEditor({
    model, templates, sheets, dom, current,
    canEdit: () => !state.streamIsRemembered, reload: loadWorkbench,
  });
  const listView = createLedgerLists({ root, host, state, model, templates, dom, events, sheets, showDetails,
    renderTransferPair: transfers.renderPair,
    isTransferReview: transfers.isOpen,
    reloadRecent: change => transfers.isOpen()
      ? change === 'period' ? transfers.load({replay: true}) : transfers.applyFilters()
      : loadStream().catch(showError),
    loadMore: () => loadStream(state.streamNext).catch(showError),
  });
  const { renderRecentSearchButton, openRecentSearchSheet, renderRecentAccountPicker, bindRecentAccountPicker, closeRecentAccountPicker, renderRecentPeriodButton,
    bindRecentPeriod, closeRecentPeriod, renderRecentSortButton, openRecentSortSheet, renderRecentRows } = listView;

function showDetails(title, html) {
  const session = sheets.open(title, html);
  if (session) session.listen(session.body, 'click', event => editor.onEditClick(event, session.body));
}

async function loadWorkbench({replay = false, restore = false} = {}) {
  if (!current()) return null;
  if (state.activePanel === 'recent') return loadStream(0, {replay, restore});
  const generation = ++readGeneration;
  clearError();
  const remembered = replay && memory?.read('overview', variant);
  if (remembered) {
    state.data = remembered;
    showRefresh('refreshing');
    renderAll(remembered);
    if (restore) restoreScroll();
  }
  if (!ChartClass && !chartsLoading) {
    chartsLoading = true;
    loadChart(host).then(chartClass => {
      chartsLoading = false;
      if (!current() || !chartClass) return;
      ChartClass = chartClass;
      overview.setChartClass(chartClass);
      if (state.activePanel === 'overview' && state.data) {
        renderOverviewCategoryInsight(state.data);
        renderTrendChart(state.data);
      }
    });
  }
  try {
    const data = await reads.run('workbench', signal => readLedgerWorkbench({signal}));
    if (!current() || generation !== readGeneration) return null;
    state.data = data;
    renderAll(data);
    memory?.write('overview', variant, data);
    showRefresh();
    return data;
  } catch (error) {
    if (isReadCancelled(error)) throw error;
    if (!current() || generation !== readGeneration) return null;
    if (remembered) {
      showRefresh('failed');
      return null;
    }
    throw error;
  }
}

function setStreamAccountGroups(groups) {
  if (!groups) return;
  const firstCatalog = !state.streamAccountGroups.length && groups.length;
  state.streamAccountGroups = groups;
  if (firstCatalog) transfers.applyFilters({resetLimit: false});
}

async function loadStream(offset = 0, {replay = false, restore = false} = {}) {
  if (!current() || state.activePanel !== 'recent' || offset == null) return null;
  if (offset > 0 && streamLoading) return null;
  const generation = ++streamGeneration;
  const query = variant + '|' + ledgerStreamQuery(state);
  const remembered = offset === 0 && replay && memory?.read('stream', query);
  if (offset === 0) {
    showRefresh(remembered ? 'refreshing' : '');
    state.streamIsRemembered = Boolean(remembered);
    state.streamRows = remembered?.rows || [];
    setStreamAccountGroups(remembered?.account_groups);
    renderRecentAccountPicker();
    state.streamNext = null;
    renderRecentRows();
    if (remembered && restore) restoreScroll();
  }
  streamLoading = true;
  try {
    const result = await reads.run('stream', signal => readLedgerStream(state, {offset, signal}));
    if (!current() || generation !== streamGeneration || state.activePanel !== 'recent') return null;
    if (offset === 0) {
      state.streamIsRemembered = false;
      state.streamRows = [];
      setStreamAccountGroups(result.account_groups);
      renderRecentAccountPicker();
    }
    const existing = new Set(state.streamRows.map(row => String(row.id)));
    state.streamRows.push(...result.rows.filter(row => !existing.has(String(row.id))));
    state.streamNext = result.next_offset;
    renderRecentRows();
    if (offset === 0) {
      memory?.write('stream', query, {rows: state.streamRows, account_groups: state.streamAccountGroups});
      showRefresh();
    }
    return result;
  } catch (error) {
    if (!isReadCancelled(error) && current() && generation === streamGeneration && remembered) {
      showRefresh('failed');
      return null;
    }
    throw error;
  } finally {
    if (generation === streamGeneration) streamLoading = false;
  }
}

function renderAll(data) {
  renderOverviewPeriodButtons();
  renderLedgerMobileHero(data);
  overview.renderLoanSummary(data);
  renderTransferSummary(data);
  renderOverviewCategoryInsight(data);
  renderRules(data.classification_rules || []);
  renderTrendChart(data);
  renderRecentSearchButton();
  renderRecentPeriodButton();
  renderRecentSortButton();
  renderRecentRows();
}

function selectTrendMode(mode) {
  state.trendMode = mode;
  $('trendMonthly')?.setAttribute('aria-pressed', String(mode === 'monthly'));
  $('trendWeekly')?.setAttribute('aria-pressed', String(mode === 'weekly'));
}

function selectRecentFlow(flow) {
  state.recentFlowFilter = flow;
  root.querySelectorAll('[data-ledger-flow-filter]').forEach(button => {
    const active = (button.dataset.ledgerFlowFilter || '') === flow;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', String(active));
  });
  $('transferReviewActions').hidden = flow !== 'internal_transfer';
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
  const categoryRank = $('overviewCategoryRank');
  if (categoryRank) events.listen('page', categoryRank, 'click', event => {
    const button = event.target.closest('[data-overview-category]');
    if (button && state.data) overview.toggleOverviewCategory(state.data, button.dataset.overviewCategory);
  });
  const categoryMonths = $('overviewCategoryMonths');
  if (categoryMonths) events.listen('page', categoryMonths, 'click', event => {
    const button = event.target.closest('[data-category-month]');
    if (!button) return;
    const pressed = button.getAttribute('aria-pressed') === 'true';
    categoryMonths.querySelectorAll('[data-category-month]').forEach(month => {
      month.setAttribute('aria-pressed', String(month === button && !pressed));
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
      transfers.selectReview();
      selectRecentFlow(btn.dataset.ledgerFlowFilter || '');
      if (host.location.hash === '#transfers') {
        replaceContentAddress(`${host.location.pathname}${host.location.search}${LEDGER_PANEL_HASH.recent}`);
      }
      loadStream().catch(showError);
    });
  });
  bindRecentPeriod();
  const loanJump = $('overviewLoanJump');
  if (loanJump) events.listen('page', loanJump, 'click', () => {
    state.recentPeriod = state.overviewPeriod;
    selectRecentFlow('personal_loan');
    replaceContentAddress(`${host.location.pathname}${host.location.search}${LEDGER_PANEL_HASH.recent}`);
    setLedgerPanel('recent');
    renderRecentPeriodButton();
    host.scrollTo({ top: 0, behavior: 'smooth' });
  });
  bindRecentAccountPicker();
  renderRecentAccountPicker();
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

function setLedgerPanel(panel = 'overview', { openTransfers = false } = {}) {
  const previous = state.activePanel;
  const previousFlow = state.recentFlowFilter;
  if (openTransfers) selectRecentFlow('internal_transfer');
  state.activePanel = LEDGER_PANEL_HASH[panel] ? panel : 'overview';
  if (state.activePanel !== 'recent') {
    closeRecentPeriod();
    closeRecentAccountPicker();
  }
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
  if (state.activePanel !== previous) {
    showRefresh();
    if (previous === 'recent') reads.cancel('stream');
    if (previous === 'overview') reads.cancel('workbench');
  }
  transfers.selectReview(openTransfers ? 'confirmed' : '');
  if (initialLoad && (state.activePanel !== previous || state.recentFlowFilter !== previousFlow)) {
    loadWorkbench({replay:true}).catch(showError);
  }
}

function bindLedgerAnchors() {
  root.querySelectorAll('a[data-ledger-tab]').forEach((link) => {
    events.listen('page', link, 'click', (event) => {
      event.preventDefault();
      const nextHash = link.hash || LEDGER_PANEL_HASH.overview;
      if (host.location.hash !== nextHash) {
        replaceContentAddress(`${host.location.pathname}${host.location.search}${nextHash}`);
      }
      setLedgerPanel(link.dataset.ledgerTab);
      host.scrollTo({ top: 0, behavior: 'smooth' });
    });
  });
  function selectPanelFromHash() {
    if (RETIRED_ACCOUNT_HASHES.has(host.location.hash)) {
      replaceContentAddress(`${host.location.pathname}${host.location.search}${LEDGER_PANEL_HASH.overview}`);
    }
    setLedgerPanel(LEDGER_HASH_PANEL[host.location.hash] || 'overview', { openTransfers: host.location.hash === '#transfers' });
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
      selectRecentFlow(state.recentFlowFilter);
      bindEvents();
      selectTrendMode(state.trendMode);
      events.listen('page', host, 'pagehide', () => controller.dispose());
      initialLoad = loadWorkbench({replay:true, restore:true}).catch(error => { if (current()) showError(error); });
      return initialLoad;
    },
    get disposed() { return disposed; },
    dispose() {
      if (disposed) return;
      memory?.write('view', variant, {
        state: snapshotLedgerViewState(state),
        hash: LEDGER_PANEL_HASH[state.activePanel],
        scroll: pageScroller(host)?.scrollTop || 0,
      });
      disposed = true;
      closeRecentPeriod();
      closeRecentAccountPicker();
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
