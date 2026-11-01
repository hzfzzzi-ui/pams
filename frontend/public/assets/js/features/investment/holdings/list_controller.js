import {
  esc, holdingGroupLabel, holdingGroupRank, buildAccountCashRows,
  isInvestmentRow
} from './list_legacy_model.js?v=20261031-modules-v1';
import {
  createListView
} from './list_view.js?v=20261031-modules-v1';
import {
  readListInputs
} from './requests.js?v=20261031-modules-v1';
import {
  createScope, createScopedDocument, createOwnedDetails, createOwnedError
} from './scope.js?v=20261031-modules-v1';
import {
  acquireClosedPositions
} from './closed_positions.js?v=20261031-modules-v1';
import { renderTradeDerivedPositions } from './trade_derived_positions.js?v=20261101-trade-positions-v1';

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
  const initialGroup = context.state?.group || document.getElementById('holdingGroupFilter')?.value;
  let currentGroup = initialGroup || '全部';
function setupHoldingGroupFilter(allRows) {
  const sel = document.getElementById('holdingGroupFilter');
  const chips = document.getElementById('mobileHoldingFilterChips');
  if (!sel) {
    renderRows(allRows, { totalRows: allRows, showGroupRows: true });
    return;
  }
  const groups = Array.from(new Set(allRows.map(holdingGroupLabel)))
    .sort((a, b) => holdingGroupRank(a) - holdingGroupRank(b) || a.localeCompare(b, 'zh-CN'));
  sel.innerHTML = '';
  const allLabel = '全部';
  let selectedGroup = initialGroup || allLabel;
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
    renderRows(rows, { totalRows: allRows, showGroupRows: false });
    renderChips();
    window.dispatchEvent(new CustomEvent('pams:holding-group-change', { detail: { group: selectedGroup, sourceRoot: root } }));
  };
  scope.listen(sel, 'change', () => {
    selectedGroup = sel.value || allLabel;
    apply();
  });
  apply();
}

  async function load() {
    try {
      const [holdings, accountOverview, fxData, ibkrPerf] = await readListInputs({get: scope.get, withClosedParam});
      if (!scope.active) return;
  document.getElementById('asof').textContent = holdings.as_of || '--';

  const fxRates = { CNY: 1 };
  for (const r of fxData?.rates || []) {
    fxRates[r.quote_currency] = Number(r.rate);
  }

  const sourceRows = (holdings.rows || [])
    .filter(isInvestmentRow)
    .filter((row) => !(row.major_category === '现金' && row.account && row.account !== 'tonghuashun'));
  const accountCashRows = buildAccountCashRows(accountOverview, fxRates, ibkrPerf);
  const allRows = [...sourceRows, ...accountCashRows];

  setupHoldingGroupFilter(allRows);
  // A reconstructed trade campaign is not a valuation snapshot. Keep it out
  // of portfolio totals and avoid duplicating an existing account snapshot.
  const section = document.getElementById('trade-derived-positions');
  const list = document.getElementById('trade-derived-position-list');
  if (section && list) { section.hidden = true; list.textContent = ''; }
  const hasSecuritiesSnapshot = (holdings.rows || []).some(row =>
    (row.account || row.account_code) === 'tonghuashun'
    && !['现金', '现金/活期'].includes(row.major_category || row.type));
  if (section && list && !hasSecuritiesSnapshot) {
    try {
      const campaigns = await scope.get('/api/investment-transactions/campaigns?account_code=tonghuashun&status=open&limit=500');
      if (scope.active) renderTradeDerivedPositions(section, list, campaigns);
    } catch (error) {
      if (scope.active && error?.name !== 'AbortError') {
        section.hidden = false;
        list.textContent = '交易推算暂不可用，请稍后重试。';
      }
    }
  }
    } catch (error) {
      if (!scope.active) return;
      errorView.show(error);
    }
  }
  const instance = {
    ready: null,
    snapshot: () => ({ group: currentGroup }),
    dispose() {
      if (!scope.active) return savedOnExit;
      savedOnExit = instance.snapshot();
      scope.dispose(); view.dispose(); details.dispose(); closed.release(); errorView.dispose();
      if (mounted.get(root) === instance) mounted.delete(root);
      return savedOnExit;
    },
    get disposed() { return !scope.active; },
  };
  let savedOnExit;
  mounted.set(root, instance);
  context.bootstrap?.();
  instance.ready = load();
  return instance;
}
export function dispose(root) { return mounted.get(root)?.dispose(); }
