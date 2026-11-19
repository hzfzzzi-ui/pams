import {
  esc, formatCompactCurrency, buildAccountCashRows,
  isInvestmentRow, historicalReferenceRow
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
import { watchPageRead } from '../../../shared/page_read.js';

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
  const cashNote = document.getElementById('holdingCashStatus');
  function showCashStatus(missing = []) {
    if (!cashNote) return;
    cashNote.hidden = !missing.length;
    cashNote.textContent = missing.length ? `${missing.join('、')}现金暂未取得核验数据` : '';
  }
  const initialGroup = context.state?.group || savedView?.group || document.getElementById('holdingGroupFilter')?.value;
  let currentGroup = initialGroup || '全部';
  let releaseGroupFilter = () => {};
  let currentSegment = context.state?.segment || savedView?.segment || '';
  let summary = remembered?.summary;
  const primaryOrder = ['全部', '股票', '基金', '银行理财', '贵金属', '员工持股'];
  const marketOrder = ['A股', '港股', '美股', '韩股'];
  const category = row => row.position_group || row.minor_category || row.major_category || '其他';

  function setupHoldingGroupFilter(allRows, {references = []} = {}) {
    releaseGroupFilter();
    const rows = [...allRows, ...references.map(historicalReferenceRow)];
    const select = document.getElementById('holdingGroupFilter');
    const primary = document.getElementById('mobileHoldingFilterChips');
    const secondary = document.getElementById('holdingSegmentFilter');
    const value = document.getElementById('personalHoldingValue');
    if (value) value.textContent = summary?.market_value_cny == null ? '—' :
      (summary.missing_values ? '≈' : '') + formatCompactCurrency(summary.market_value_cny);
    const groups = [...new Set([...primaryOrder, ...rows.map(category)])];
    if (!groups.includes(currentGroup)) currentGroup = '全部';
    const button = (label, key, selected) =>
      `<button type="button" data-filter="${esc(key)}" aria-pressed="${selected}" class="mobileFilterChip ${selected ? 'active' : ''}">${esc(label)}</button>`;
    function apply() {
      const matches = currentGroup === '全部' ? rows.filter(r => !['负债','现金/活期'].includes(category(r))) :
        rows.filter(r => category(r) === currentGroup);
      const segments = [...new Set(matches.map(r => r.position_segment).filter(Boolean))];
      const order = currentGroup === '基金' ? ['场内','场外'] : marketOrder;
      segments.sort((a,b) => (order.includes(a) ? order.indexOf(a) : order.length) -
        (order.includes(b) ? order.indexOf(b) : order.length) || a.localeCompare(b, 'zh-CN'));
      if (!['股票','基金'].includes(currentGroup) || !segments.includes(currentSegment)) currentSegment = '';
      if (select) select.value = currentGroup;
      if (primary) primary.innerHTML = groups.map(g => button(g,g,g === currentGroup)).join('');
      if (secondary) {
        secondary.hidden = !segments.length || !['股票','基金'].includes(currentGroup);
        secondary.innerHTML = secondary.hidden ? '' : button(currentGroup === '股票' ? '全部市场' : '全部基金','',!currentSegment)
          + segments.map(g => button(g,g,g === currentSegment)).join('');
      }
      renderRows(matches.filter(r => !currentSegment || r.position_segment === currentSegment), {summary});
      const heroGroup = currentGroup === '基金' ? 'A股基金' : currentGroup === '股票' ?
        (currentSegment === 'A股' ? 'A股个股' : currentSegment || '全部') : currentGroup;
      window.dispatchEvent(new CustomEvent('pams:holding-group-change', {detail:{group:heroGroup,sourceRoot:root}}));
    }
    if (select) select.innerHTML = groups.map(g => `<option>${esc(g)}</option>`).join('');
    function chooseGroup(group) {
      currentGroup = group;
      currentSegment = '';
      apply();
    }
    const removers = [
      scope.listen(select,'change',() => chooseGroup(select.value)),
      scope.listen(primary,'click',event => {
        const btn = event.target.closest('[data-filter]');
        if (btn) chooseGroup(btn.dataset.filter);
      }),
      scope.listen(secondary,'click',event => {
        const btn = event.target.closest('[data-filter]');
        if (btn) { currentSegment = btn.dataset.filter; apply(); }
      }),
    ];
    releaseGroupFilter = () => removers.forEach(remove => remove());
    apply();
  }

  async function load() {
    try {
      let sourceRows = [];
      let references = [];
      let supplementFailed = false;
      const [holdings, accountValues] = await readListInputs({
        get: path => scope.get(path, savedView ? {force: true} : undefined),
        loadHoldingView: context.loadHoldingView,
        loadAccountPositions: context.loadAccountPositions,
        onSupplementError: () => { supplementFailed = true; },
        withClosedParam,
        onHoldings(holdings) {
          if (!scope.active) return;
          if (!remembered) document.getElementById('asof').textContent = holdings.as_of || '--';
          sourceRows = (holdings.rows || []).filter(isInvestmentRow);
          summary = holdings.personal_summary;
          references = holdings.historical_references || [];
          if (!remembered && !['现金/活期','负债'].includes(currentGroup)) {
            setupHoldingGroupFilter(sourceRows, {references});
          }
        },
      });
      if (!scope.active) return;

      const cash = buildAccountCashRows(accountValues, sourceRows);
      const rows = [...sourceRows, ...cash.rows];
      setupHoldingGroupFilter(rows, {references});
      showCashStatus(cash.missing);
      document.getElementById('asof').textContent = holdings.as_of || '--';
      if (!supplementFailed) memory?.write('rows', variant, {rows, references, summary, asOf: holdings.as_of, missingCash: cash.missing});
      showRememberedData(refreshNote);
    } catch (error) {
      if (!scope.active) return;
      if (remembered) showRememberedData(refreshNote, 'failed');
      else errorView.show(error);
    }
  }
  const instance = {
    ready: null,
    snapshot: () => ({ group: currentGroup, segment: currentSegment }),
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
    setupHoldingGroupFilter(remembered.rows, {
      references: remembered.references,
    });
    showCashStatus(remembered.missingCash);
    document.getElementById('asof').textContent = remembered.asOf || '--';
    restorePageScroll(window, savedView?.scroll, () => scope.active);
  }
  instance.ready = load();
  watchPageRead(scope, window, load);
  return instance;
}
export function dispose(root) { return mounted.get(root)?.dispose(); }
