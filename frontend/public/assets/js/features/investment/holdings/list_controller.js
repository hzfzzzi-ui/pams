import {
  esc, holdingGroupLabel, holdingGroupRank, buildAccountCashRows,
  isInvestmentRow
} from './list_legacy_model.js';
import {
  createListView
} from './list_view.js';
import {
  readListInputs
} from './requests.js';
import {
  createScope, createScopedDocument, createOwnedDetails, createOwnedError
} from './scope.js';
import {
  acquireClosedPositions
} from './closed_positions.js';

import { pageMemory, pageScroller, restorePageScroll, showRememberedData } from '../../../app/page_memory.js';

const mounted = new WeakMap();
export function mount(root, context) {
  if (mounted.has(root)) return mounted.get(root);
  const window = context.host || root.ownerDocument.defaultView;
  const scope = createScope(window, context.get);
  const document = createScopedDocument(root);
  const errorView = createOwnedError(document.getElementById('err'));
  const closed = acquireClosedPositions(root, window);
  const { withClosedParam } = closed;
  const details = createOwnedDetails(context.showDetails, context.closeDetails);
  const view = createListView({document, host: window, showDetails: details.show, scope});
  const { renderRows } = view;
  const memory = pageMemory(window);
  const variant = window.location.search;
  const isHoldings = () => ['', '#holdings-smart-table'].includes(window.location.hash);
  const remembered = isHoldings() && memory?.read('rows', variant);
  const savedView = memory?.read('view', variant);
  const refreshNote = document.getElementById('holdingViewRefresh');
  const initialGroup = context.state?.group || savedView?.group || document.getElementById('holdingGroupFilter')?.value;
  let currentGroup = initialGroup || '全部';
  let releaseGroupFilter = () => {};
function setupHoldingGroupFilter(allRows, { cashPending = false } = {}) {
  releaseGroupFilter();
  const sel = document.getElementById('holdingGroupFilter');
  const chips = document.getElementById('mobileHoldingFilterChips');
  if (!sel) {
    renderRows(allRows, { totalRows: allRows, showGroupRows: true, cashPending });
    return;
  }
  const groups = Array.from(new Set(allRows.map(holdingGroupLabel)))
    .sort((a, b) => holdingGroupRank(a) - holdingGroupRank(b) || a.localeCompare(b, 'zh-CN'));
  sel.innerHTML = '';
  const allLabel = '全部';
  let selectedGroup = currentGroup;
  const allOpt = document.createElement('option');
  allOpt.value = allLabel;
  allOpt.textContent = allLabel;
  sel.appendChild(allOpt);
  for (const group of groups) {
    const opt = document.createElement('option');
    opt.value = group;
    opt.textContent = group;
    sel.appendChild(opt);
  }
  if (selectedGroup !== allLabel && !groups.includes(selectedGroup)) selectedGroup = allLabel;
  sel.value = selectedGroup;
  let chipListeners = [];
  const renderChips = () => {
    for (const remove of chipListeners) remove();
    chipListeners = [];
    if (!chips) return;
    chips.innerHTML = [allLabel, ...groups].map((label) => `
      <button class="mobileFilterChip ${label === selectedGroup ? 'active' : ''}" type="button" data-group="${esc(label)}">${esc(label)}</button>
    `).join('');
    chips.querySelectorAll('[data-group]').forEach((btn) => {
      chipListeners.push(scope.listen(btn, 'click', () => {
        selectedGroup = btn.dataset.group || allLabel;
        if (selectedGroup !== allLabel && !groups.includes(selectedGroup)) selectedGroup = allLabel;
        sel.value = selectedGroup;
        apply();
      }));
    });
  };
  const apply = () => {
    const selected = selectedGroup === allLabel ? allLabel : sel.value;
    selectedGroup = selected || allLabel;
    currentGroup = selectedGroup;
    const rows = selectedGroup === allLabel ? allRows : allRows.filter((row) => holdingGroupLabel(row) === selectedGroup);
    renderRows(rows, { totalRows: allRows, showGroupRows: false, cashPending });
    renderChips();
    window.dispatchEvent(new CustomEvent('pams:holding-group-change', { detail: { group: selectedGroup, sourceRoot: root } }));
  };
  const removeSelect = scope.listen(sel, 'change', () => {
    selectedGroup = sel.value || allLabel;
    apply();
  });
  releaseGroupFilter = () => {
    removeSelect();
    for (const remove of chipListeners) remove();
  };
  apply();
}

  async function load() {
    try {
      let sourceRows = [];
      let supplementFailed = false;
      const [holdings, accountOverview, fxData, ibkrPerf] = await readListInputs({
        get: path => scope.get(path, savedView ? {force: true} : undefined),
        onSupplementError: () => { supplementFailed = true; },
        withClosedParam,
        onHoldings(holdings) {
          if (!scope.active) return;
          if (!remembered) document.getElementById('asof').textContent = holdings.as_of || '--';
          sourceRows = (holdings.rows || [])
            .filter(isInvestmentRow)
            .filter(row => !(row.major_category === '现金' && row.account && row.account !== 'tonghuashun'));
          if (!remembered && sourceRows.length && (currentGroup === '全部' || sourceRows.some(row => holdingGroupLabel(row) === currentGroup))) {
            setupHoldingGroupFilter(sourceRows, {cashPending: true});
          }
        },
      });
      if (!scope.active) return;

  const fxRates = { CNY: 1 };
  for (const r of fxData?.rates || []) {
    fxRates[r.quote_currency] = Number(r.rate);
  }

      if (remembered && supplementFailed) {
        showRememberedData(refreshNote, 'failed');
        return;
      }
      const accountCashRows = buildAccountCashRows(accountOverview, fxRates, ibkrPerf);
      const rows = [...sourceRows, ...accountCashRows];
      setupHoldingGroupFilter(rows);
      document.getElementById('asof').textContent = holdings.as_of || '--';
      if (!supplementFailed) memory?.write('rows', variant, {rows, asOf: holdings.as_of});
      showRememberedData(refreshNote);
    } catch (error) {
      if (!scope.active) return;
      if (remembered) showRememberedData(refreshNote, 'failed');
      else errorView.show(error);
    }
  }
  const instance = {
    ready: null,
    snapshot: () => ({ group: currentGroup }),
    dispose() {
      if (!scope.active) return savedOnExit;
      savedOnExit = instance.snapshot();
      if (isHoldings()) memory?.write('view', variant, {...savedOnExit, scroll: pageScroller(window)?.scrollTop || 0});
      scope.dispose(); view.dispose(); details.dispose(); closed.release(); errorView.dispose();
      if (mounted.get(root) === instance) mounted.delete(root);
      return savedOnExit;
    },
    get disposed() { return !scope.active; },
  };
  let savedOnExit;
  mounted.set(root, instance);
  context.bootstrap?.();
  if (remembered) {
    showRememberedData(refreshNote, 'refreshing');
    setupHoldingGroupFilter(remembered.rows);
    document.getElementById('asof').textContent = remembered.asOf || '--';
    restorePageScroll(window, savedView?.scroll, () => scope.active);
  }
  instance.ready = load();
  return instance;
}
export function dispose(root) { return mounted.get(root)?.dispose(); }
