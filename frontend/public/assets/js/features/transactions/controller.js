import { createDomView } from '../../shared/dom_view.js?v=20261031-transactions-v1';
import { createState } from './state.js?v=20261031-transactions-v1';
import { createScope } from './scope.js?v=20261031-transactions-v1';
import { createLegacyReview } from './legacy_review.js?v=20261031-transactions-v1';
import { createListView } from './list_view.js?v=20261031-transactions-v1';
import { createAttributionView } from './attribution_view.js?v=20261031-transactions-v1';
import { createEditor } from './editor.js?v=20261031-transactions-v1';
import { createCommands } from './commands.js?v=20261031-transactions-v1';

const mounted = new WeakMap();

export function mount(root, context) {
  if (!root?.querySelector) throw new TypeError('Transactions root is required');
  const current = mounted.get(root);
  if (current) return current;
  const {get, post, bootstrap = () => {}, win = root.ownerDocument?.defaultView} = context;
  const location = context.location || win.location;
  const navigate = context.navigate || (url => win.location.assign(url));
  const restore = context.restoreState;
  const scope = createScope(root, {...context, win});
  const dom = createDomView(root);
  const showError = error => {if (scope.alive) dom.showError(error);};
  const clearError = () => {if (scope.alive) dom.clearError();};
  const state = createState();
  const legacy = createLegacyReview({state, scope});
  state.filters = restore?.filters ? {...restore.filters} : legacy.defaultFilterState();
  state.showAll = Boolean(restore?.showAll);
  const list = createListView({state, scope, legacy});
  const attribution = createAttributionView({state, scope, legacy});
  const editor = createEditor({state, scope, legacy, attribution, get});
  const commands = createCommands({scope, legacy, post, loadOptions, loadLedger});
  const $ = scope.byId;
  const closeBottomSheet = scope.closeSheet;
  const requests = {options: 0, ledger: 0, attribution: 0, campaigns: 0};
  let restoredAttribution = false;
  const {applyIdentityDefaults, defaultFilterState, todayIso, updateTradeReviewCompletion, updateTransitionSection} = legacy;
  const {buildQuery, fillSelects, fillTradeNameOptions, readTradeFilterForm, renderSummary, renderTransactionSummary, setDefaultFilters, toggleTradeFilterPanel} = list;
  const {applyTradeReviewDirectionFilter, closeTradeReviewAssetFloat, renderTradeAttribution: renderAttributionView} = attribution;
  const {openTradeReviewById} = editor;
  const {saveTradeReview} = commands;

async function loadOptions() {
  if (!scope.alive) return;
  const request = ++requests.options;
  const data = await get('/api/investment-transactions/options');
  if (!scope.alive || request !== requests.options) return;
  state.accounts = data.accounts || [];
  state.categories = data.categories || [];
  state.sources = data.sources || [];
  state.assets = data.assets || [];
  state.reviewOptions = data.review_options || {};
  fillSelects();
}

async function loadLedger() {
  if (!scope.alive) return;
  const request = ++requests.ledger;
  clearError();
  const data = await get(`/api/investment-transactions?${buildQuery()}`);
  if (!scope.alive || request !== requests.ledger) return;
  state.rows = data.rows || [];
  state.summary = data.summary || {};
  fillTradeNameOptions(state.rows);
  renderSummary(state.summary);
  renderTransactionSummary(state.summary, state.rows);
}

function requestAttributionSummaryLoad() {
  if (!scope.alive) return Promise.resolve();
  if (state.attributionLoadPromise) return state.attributionLoadPromise;
  state.attributionLoadPromise = loadAttributionSummary().catch(() => {
    if (!scope.alive) return;
    renderTradeAttribution({
      sample_count: 0,
      missing_price_count: 0,
      groups: { buy_reason: [], sell_reason: [] },
      data_quality: { note: '自动归因暂时不可用。' },
    });
  }).finally(() => {
    state.attributionLoadPromise = null;
  });
  return state.attributionLoadPromise;
}

function shouldLoadVisibleTradeAttribution() {
  const panel = $('trade-attribution-analysis');
  if (!panel) return false;
  const hash = String(location.hash || '').replace(/^#/, '');
  return hash === 'trade-attribution-analysis' || panel.hidden === false;
}

function requestVisibleTradeAttributionLoad() {
  if (shouldLoadVisibleTradeAttribution()) {
    requestAttributionSummaryLoad();
  }
}

function requestInitialVisibleTradeAttributionLoad() {
  if (state.attribution) return;
  requestVisibleTradeAttributionLoad();
}

function scheduleInitialTradeAttributionLoadProbe() {
  [0, 300, 1200, 3000].forEach((delay) => {
    scope.delay(requestInitialVisibleTradeAttributionLoad, delay);
  });
}

async function loadAttributionSummary() {
  if (!scope.alive) return;
  const request = ++requests.attribution;
  const params = new URLSearchParams();
  const filters = state.filters || defaultFilterState();
  const start = filters.start || '';
  const end = filters.end || '';
  if (start) params.set('start', start);
  if (end) params.set('end', end);
  params.set('limit', '300');
  const payload = await get(`/api/investment-transactions/attribution/summary?${params.toString()}`, { cache: false });
  if (!scope.alive || request !== requests.attribution) return;
  state.attribution = payload;
  state.campaigns = null;
  renderTradeAttribution(payload);
  await loadCampaignSummary();
  if (scope.alive && request === requests.attribution) renderTradeAttribution(state.attribution);
}

async function loadCampaignSummary() {
  if (!scope.alive) return;
  const request = ++requests.campaigns;
  const params = new URLSearchParams();
  const filters = state.filters || defaultFilterState();
  const start = filters.start || '';
  const end = filters.end || '';
  if (start) params.set('start', start);
  if (end) params.set('end', end);
  if (filters.assetKey) params.set('asset_key', filters.assetKey);
  params.set('limit', '5');
  try {
    const campaigns = await get(`/api/investment-transactions/campaigns?${params.toString()}`, { cache: false });
    if (scope.alive && request === requests.campaigns) state.campaigns = campaigns;
  } catch (err) {
    if (!scope.alive || request !== requests.campaigns) return;
    state.campaigns = {
      summary: { total_campaigns: 0, status_counts: {} },
      campaigns: [],
      data_quality: { note: err?.message || '仓位周期暂时不可用。' },
    };
  }
}

async function createTrade() {
  navigate('account_import.html?kind=trade&mode=manual');
}

function bindEvents() {
  scope.onWindow('pams:load-trade-attribution', () => requestAttributionSummaryLoad());
  scope.onWindow('hashchange', requestVisibleTradeAttributionLoad);
  scope.on('click', async (event) => {
    if (event.target?.closest?.('#btnReload')) {loadLedger().catch(showError); return;}
    if (event.target?.closest?.('#btnReset')) {setDefaultFilters(); loadLedger().catch(showError); return;}
    if (event.target?.closest?.('#btnCreateTrade')) {createTrade(); return;}
    const autoTagButton = event.target?.closest?.('[data-auto-tag]');
    if (autoTagButton) {
      event.preventDefault();
      const form = autoTagButton.closest('#tradeReviewForm');
      const tag = autoTagButton.dataset.autoTag;
      const input = [...(form?.querySelectorAll('input[name="reviewTag"]') || [])].find((item) => item.value === tag);
      if (!form || !input) return;
      const checkedTags = form.querySelectorAll('input[name="reviewTag"]:checked').length;
      if (!input.checked && checkedTags >= 3) {
        if ($('tradeReviewMsg')) $('tradeReviewMsg').textContent = '复盘标签最多选择 3 个。';
        return;
      }
      input.checked = true;
      input.closest('.tradeReviewTag')?.classList.add('checked');
      autoTagButton.classList.add('accepted');
      if ($('tradeReviewMsg')) $('tradeReviewMsg').textContent = '已接受系统建议标签。';
      updateTradeReviewCompletion(form);
      return;
    }
    if (event.target?.id === 'btnToggleAllTrades') {
      state.showAll = !state.showAll;
      renderTransactionSummary(state.summary, state.rows);
      return;
    }
    const patternViewButton = event.target?.closest?.('[data-pattern-view-target]');
    if (patternViewButton) {
      event.preventDefault();
      const panel = patternViewButton.closest('.tradeReviewPatternPanel');
      const target = patternViewButton.dataset.patternViewTarget;
      if (!panel || !target) return;
      panel.querySelectorAll('[data-pattern-view-target]').forEach((button) => {
        const active = button.dataset.patternViewTarget === target;
        button.classList.toggle('active', active);
        button.setAttribute('aria-selected', active ? 'true' : 'false');
      });
      panel.querySelectorAll('[data-pattern-view]').forEach((view) => {
        view.hidden = view.dataset.patternView !== target;
      });
      return;
    }
    const directionFilterToggle = event.target?.closest?.('[data-direction-filter-toggle]');
    if (directionFilterToggle) {
      event.preventDefault();
      const panel = directionFilterToggle.closest('[data-direction-panel]');
      const filters = panel?.querySelector('[data-direction-filter-panel]');
      if (!filters) return;
      filters.hidden = !filters.hidden;
      directionFilterToggle.setAttribute('aria-expanded', filters.hidden ? 'false' : 'true');
      return;
    }
    const directionFilterButton = event.target?.closest?.('[data-direction-filter-value]');
    if (directionFilterButton) {
      event.preventDefault();
      const panel = directionFilterButton.closest('[data-direction-panel]');
      applyTradeReviewDirectionFilter(panel, directionFilterButton.dataset.directionFilterValue || 'all');
      return;
    }
    const assetHeatClose = event.target?.closest?.('[data-asset-heat-close]');
    if (assetHeatClose) {
      event.preventDefault();
      const panel = assetHeatClose.closest('[data-asset-heatmap-panel]');
      closeTradeReviewAssetFloat(panel);
      return;
    }
    const assetHeatButton = event.target?.closest?.('[data-asset-heat-select]');
    if (assetHeatButton) {
      event.preventDefault();
      const panel = assetHeatButton.closest('[data-asset-heatmap-panel]');
      const target = assetHeatButton.dataset.assetHeatSelect;
      if (!panel || !target) return;
      panel.querySelectorAll('[data-asset-heat-select]').forEach((button) => {
        const active = button.dataset.assetHeatSelect === target;
        button.classList.toggle('active', active);
        button.setAttribute('aria-pressed', active ? 'true' : 'false');
      });
      let activeDetail = null;
      panel.querySelectorAll('[data-asset-heat-detail]').forEach((detail) => {
        const active = detail.dataset.assetHeatDetail === target;
        detail.hidden = !active;
        if (active) activeDetail = detail;
      });
      const floatLayer = panel.querySelector('[data-asset-heat-float]');
      if (floatLayer && activeDetail) {
        floatLayer.hidden = false;
        scope.root.classList.add('trade-review-asset-open');
        const floatCard = floatLayer.querySelector('.tradeReviewAssetFloatCard');
        if (floatCard) floatCard.scrollTop = 0;
        floatLayer.querySelector('[data-asset-heat-close]')?.focus?.({ preventScroll: true });
      }
      return;
    }
    const reviewButton = event.target?.closest?.('[data-trade-review]');
    if (reviewButton) {
      try {
        await openTradeReviewById(reviewButton.dataset.tradeReview);
      } catch (err) {
        showError(err);
      }
      return;
    }
    if (event.target?.id === 'btnOpenTradeForm') {
      createTrade();
      return;
    }
    if (event.target?.id === 'btnCloseTradeForm') {
      $('manualTradeEntry').hidden = true;
      return;
    }
    const filterButton = event.target?.closest?.('#btnToggleTradeFilters');
    if (filterButton) {
      event.preventDefault();
      event.stopPropagation();
      toggleTradeFilterPanel();
      return;
    }
    if (event.target?.id === 'btnClearTradeFilters') {
      event.preventDefault();
      setDefaultFilters();
      closeBottomSheet();
      loadLedger().catch(showError);
      return;
    }
    if (event.target?.id === 'btnShowIdentityTransition') {
      event.preventDefault();
      const form = event.target.closest('#tradeReviewForm');
      if (form) {
        updateTransitionSection(form, { forceShow: true });
        updateTradeReviewCompletion(form);
      }
      return;
    }
  });
  scope.on('keydown', (event) => {
    if (event.key !== 'Escape') return;
    const floatLayer = scope.query('[data-asset-heat-float]:not([hidden])');
    if (!floatLayer) return;
    event.preventDefault();
    closeTradeReviewAssetFloat(floatLayer.closest('[data-asset-heatmap-panel]'));
  });
  scope.on('change', (event) => {
    const form = event.target?.closest?.('#tradeReviewForm');
    const choice = event.target?.closest?.('.tradeReviewTag input');
    if (!choice) {
      if (form) {
        updateTransitionSection(form);
        updateTradeReviewCompletion(form);
      }
      return;
    }
    if (choice.type === 'radio') {
      choice.form?.querySelectorAll(`input[name="${choice.name}"]`).forEach((input) => {
        input.closest('.tradeReviewTag')?.classList.toggle('checked', Boolean(input.checked));
      });
      if (['reviewInitialIdentity', 'reviewCurrentIdentity'].includes(choice.name)) {
        applyIdentityDefaults(choice.form, { forceRule: true, forceLimit: true, forcePeriod: choice.name === 'reviewInitialIdentity' });
      } else {
        updateTransitionSection(choice.form);
        updateTradeReviewCompletion(choice.form);
      }
      return;
    }
    if (choice.name === 'reviewTag' && choice.checked) {
      const checkedTags = choice.form?.querySelectorAll('input[name="reviewTag"]:checked').length || 0;
      if (checkedTags > 3) {
        choice.checked = false;
        if ($('tradeReviewMsg')) $('tradeReviewMsg').textContent = '复盘标签最多选择 3 个。';
      }
    }
    choice.closest('.tradeReviewTag')?.classList.toggle('checked', Boolean(choice.checked));
    updateTradeReviewCompletion(choice.form);
  });
  scope.on('input', (event) => {
    const form = event.target?.closest?.('#tradeReviewForm');
    if (!form) return;
    if (['reviewMaxPositionPct', 'reviewSellCondition', 'reviewNextReviewDate', 'reviewAddCondition'].includes(event.target.id)) {
      event.target.dataset.autoGenerated = '0';
      updateTransitionSection(form);
    }
    updateTradeReviewCompletion(form);
  });
  scope.on('submit', (event) => {
    if (event.target?.id === 'tradeFilterForm') {
      event.preventDefault();
      state.filters = readTradeFilterForm(event.target);
      state.showAll = false;
      closeBottomSheet();
      loadLedger().catch(showError);
      return;
    }
    if (event.target?.id !== 'tradeReviewForm') return;
    event.preventDefault();
    saveTradeReview(event.target).catch(showError);
  });
}

  function renderTradeAttribution(payload) {
    renderAttributionView(payload);
    if (restore && !restoredAttribution) {
      for (const item of restore.details || []) {
        const details = $(item.id);
        if (details?.tagName === 'DETAILS') details.open = item.open;
      }
      restoredAttribution = true;
    }
  }
  function captureUiState() {
    return {
      filters: {...state.filters}, showAll: state.showAll,
      manualHidden: $('manualTradeEntry')?.hidden,
      inputs: [...root.querySelectorAll('#manualTradeEntry input[id], #manualTradeEntry select[id], #manualTradeEntry textarea[id]')].map(input => ({id: input.id, value: input.value, checked: input.checked})),
      details: [...root.querySelectorAll('#trade-attribution-analysis details[id]')].map(details => ({id: details.id, open: details.open})),
      filterDraft: scope.query('#tradeFilterForm') ? readTradeFilterForm(scope.query('#tradeFilterForm')) : null,
    };
  }
  function restoreInputs() {
    if (!restore) return;
    for (const item of restore.inputs || []) {
      const input = $(item.id);
      if (!input || !root.querySelector('#manualTradeEntry')?.contains(input)) continue;
      input.value = item.value;
      if (typeof item.checked === 'boolean') input.checked = item.checked;
    }
    if ($('manualTradeEntry') && typeof restore.manualHidden === 'boolean') $('manualTradeEntry').hidden = restore.manualHidden;
  }
  async function init() {
    bootstrap();
    bindEvents();
    if (!restore) setDefaultFilters();
    scheduleInitialTradeAttributionLoadProbe();
    await loadOptions();
    if (!scope.alive) return;
    if (!restore) setDefaultFilters();
    else restoreInputs();
    await loadLedger();
    if (!scope.alive) return;
    if (restore?.filterDraft) {
      list.setTradeFilterPanelOpen(true, {focus: false});
      const fields = {filterSheetStart: 'start', filterSheetEnd: 'end', filterSheetSide: 'side', filterSheetAsset: 'assetKey', filterSheetSearch: 'search'};
      for (const [id, field] of Object.entries(fields)) if ($(id)) $(id).value = restore.filterDraft[field] || '';
    }
    requestInitialVisibleTradeAttributionLoad();
  }
  function dispose() {
    if (!scope.alive) return;
    closeTradeReviewAssetFloat();
    scope.root.classList.remove('trade-review-asset-open');
    scope.dispose();
    if (mounted.get(root) === controller) mounted.delete(root);
  }
  const controller = {ready: null, captureUiState, dispose, get disposed() {return !scope.alive;}};
  mounted.set(root, controller);
  scope.onWindow('pagehide', dispose, {once: true});
  controller.ready = init().catch(showError);
  return controller;
}

export function dispose(root) {mounted.get(root)?.dispose();}
