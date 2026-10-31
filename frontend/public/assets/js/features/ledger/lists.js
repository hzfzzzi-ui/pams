import { accountLabel } from '../../api.js?v=20261031-modules-v1';
import { escapeHtml, formatCurrency, formatMoneyAxis as axisMoney, formatMoneyFixed2 as money, formatPercent, formatSignedMoneyFixed2 as signedMoney, toFiniteNumber as num } from '../../utils.js?v=20261031-modules-v1';
import { LEDGER_PANEL_HASH, RECENT_SORT_OPTIONS } from './state.js?v=20261031-modules-v1';

export function createLedgerLists({ root, host, state, model, templates, dom, events, sheets, replaceContentAddress, setLedgerPanel, showDetails }) {
  const { $ } = dom;
  const { flowClass, isEditableLedgerRow, ledgerEditActionLabel, rowTagLabel, rowAmountClass, rowTitle, rowMetaText, rowDateText, rowSearchText, originalCurrencyText, accountDisplayName, buildAccountFlowGroups, filteredRecentRows } = model;
  const { recentSearchSheetHtml, rowDetailHtml, renderAccountGroup, recentSortSheetHtml } = templates;

function renderRecentSearchButton() {
  const btn = $('recentSearchToggle');
  if (!btn) return;
  const hasSearch = Boolean(state.recentSearch);
  btn.classList.toggle('active', hasSearch);
  btn.setAttribute('aria-pressed', String(hasSearch));
  btn.title = hasSearch ? `搜索：${state.recentSearch}` : '搜索流水';
}

function openRecentSearchSheet() {
  const session = sheets.open('搜索流水', recentSearchSheetHtml());
  if (!session) return;
  const form = session.body.querySelector('[data-recent-search-form]');
  const input = session.body.querySelector('[data-recent-search-input]');
  input?.focus();
  session.listen(form, 'submit', event => {
    event.preventDefault();
    state.recentSearch = String(input?.value || '').trim();
    renderRecentSearchButton();
    renderRecentRows();
    session.close();
  });
  session.listen(session.body.querySelector('[data-recent-search-clear]'), 'click', () => {
    state.recentSearch = '';
    renderRecentSearchButton();
    renderRecentRows();
    session.close();
  });
}

function renderRecentAccountScope() {
  events.clear('account-scope');
  const el = $('recentAccountScope');
  if (!el) return;
  if (!state.recentAccountFilter) {
    el.hidden = true;
    el.innerHTML = '';
    return;
  }
  el.hidden = false;
  const periodLabel = state.recentPeriod === 'ytd' ? '年初至今' : '本月';
  el.innerHTML = `
    <span>账户筛选 <strong>${escapeHtml(accountDisplayName(state.recentAccountFilter))}</strong><small> · ${escapeHtml(periodLabel)}</small></span>
    <button type="button" data-clear-account-filter>清除</button>
  `;
  events.listen('account-scope', el.querySelector('[data-clear-account-filter]'), 'click', () => {
    state.recentAccountFilter = '';
    renderRecentAccountScope();
    renderRows('recentRows', filteredRecentRows(), 120);
  });
}

function openRecentForAccount(accountCode) {
  state.recentAccountFilter = accountCode || '';
  state.recentFlowFilter = '';
  state.recentPeriod = 'ytd';
  renderRecentPeriodButtons();
  root.querySelectorAll('[data-ledger-flow-filter]').forEach((item) => {
    item.classList.toggle('active', (item.dataset.ledgerFlowFilter || '') === '');
  });
  renderRecentAccountScope();
  renderRows('recentRows', filteredRecentRows(), 120);
  replaceContentAddress(`${host.location.pathname}${host.location.search}${LEDGER_PANEL_HASH.recent}`);
  setLedgerPanel('recent');
  host.scrollTo({ top: 0, behavior: 'smooth' });
}

function renderRecentPeriodButtons() {
  root.querySelectorAll('[data-recent-period]').forEach((btn) => {
    btn.classList.toggle('active', (btn.dataset.recentPeriod || 'month') === state.recentPeriod);
  });
}

function renderRecentSortButton() {
  const btn = $('recentSortToggle');
  if (!btn) return;
  const hasSort = Boolean(state.recentAmountSort);
  const label = RECENT_SORT_OPTIONS.find((item) => item.value === state.recentAmountSort)?.label || '流水排序';
  btn.classList.toggle('active', hasSort);
  btn.setAttribute('aria-pressed', String(hasSort));
  btn.title = label;
}

function openRecentSortSheet() {
  const session = sheets.open('流水排序', recentSortSheetHtml());
  if (!session) return;
  session.listen(session.body, 'click', event => {
    const option = event.target?.closest?.('[data-recent-sort-option]');
    if (!option || !session.body.contains(option)) return;
    state.recentAmountSort = option.dataset.recentSortOption || '';
    renderRecentSortButton();
    renderRecentRows();
    session.close();
  });
}

function renderAccountFlowSummary(rows) {
  events.clear('accounts');
  const el = $('accountFlowRows');
  if (!el) return;
  const hint = $('accountFlowHint');
  if (hint) {
    hint.textContent = '账户余额优先展示；现金流变化统一按年初至今计算。';
  }
  if (!rows?.length) {
    el.innerHTML = '<div class="ledgerEmpty">年初至今暂无账户流水</div>';
    return;
  }
  el.innerHTML = buildAccountFlowGroups(rows).map(renderAccountGroup).join('');
  el.querySelectorAll('[data-account-flow-filter]').forEach((btn) => {
    events.listen('accounts', btn, 'click', () => openRecentForAccount(btn.dataset.accountFlowFilter || ''));
  });
  el.querySelectorAll('[data-account-group-toggle]').forEach((btn) => {
    events.listen('accounts', btn, 'click', () => {
      const groupCode = btn.dataset.accountGroupToggle || '';
      if (state.expandedAccountGroups.has(groupCode)) {
        state.expandedAccountGroups.delete(groupCode);
      } else {
        state.expandedAccountGroups.add(groupCode);
      }
      renderAccountFlowSummary(rows);
    });
  });
}

function renderRows(id, rows, limit = 80) {
  events.clear(`rows:${id}`);
  const el = $(id);
  if (!el) return;
  if (!rows?.length) {
    el.innerHTML = '<div class="ledgerEmpty">暂无流水</div>';
    return;
  }
  const displayRows = rows.slice(0, limit);
  el.innerHTML = displayRows.map((row, index) => {
    const rawId = row.id || row.transaction_id || `${row.transaction_date || 'row'}-${index}`;
    const detailId = `${id}-detail-${String(rawId).replace(/[^a-zA-Z0-9_-]/g, '_')}`;
    const amount = num(row.amount_cny ?? row.amount);
    const amountClass = rowAmountClass(row, amount);
    const title = rowTitle(row);
    const meta = rowMetaText(row);
    const dateText = rowDateText(row);
    const originalAmount = originalCurrencyText(row);
    const reviewAction = row.needs_review && isEditableLedgerRow(row)
      ? `<button type="button" class="ledgerInlineEditBtn" data-ledger-edit="${escapeHtml(String(row.id || ''))}">${escapeHtml(ledgerEditActionLabel(row))}</button>`
      : '';
    return `
      <div class="ledgerListRow" role="button" tabindex="0" data-detail-target="${escapeHtml(detailId)}" title="点击查看账户、对方和原始摘要">
        <span class="ledgerListMain">
          <strong title="${escapeHtml(title)}">${escapeHtml(title)}</strong>
          <small class="ledgerListAccount" title="${escapeHtml(rowSearchText(row))}">${escapeHtml(meta)}</small>
          <small class="ledgerListDate">${escapeHtml(dateText)}</small>
        </span>
        <span class="ledgerListSide">
          <b class="${amountClass}">${signedMoney(amount)}</b>
          ${originalAmount ? `<small class="ledgerOriginalAmount">原币 ${escapeHtml(originalAmount)}</small>` : ''}
          <small class="ledgerListMeta">
            <span class="flowTag ${flowClass(row.flow_type)}">${escapeHtml(rowTagLabel(row))}</span>
            ${row.needs_review ? '<span class="ledgerReviewHint">待确认支出</span>' : ''}
          </small>
          ${reviewAction}
        </span>
      </div>
      <div id="${escapeHtml(detailId)}" class="ledgerInlineDetail" hidden>
        ${rowDetailHtml(row)}
      </div>
    `;
  }).join('');

  el.querySelectorAll('.ledgerListRow').forEach((rowEl) => {
    const openDetail = (event) => {
      if (event?.target?.closest?.('[data-ledger-edit]')) return;
      const detail = $(rowEl.dataset.detailTarget);
      if (!detail) return;
      if (host.matchMedia('(max-width: 760px)').matches) {
        const title = rowEl.querySelector('.ledgerListMain strong')?.innerText || '流水详情';
        showDetails(title, detail.innerHTML);
        return;
      }
      detail.hidden = !detail.hidden;
      rowEl.classList.toggle('open', !detail.hidden);
    };
    events.listen(`rows:${id}`, rowEl, 'click', openDetail);
    events.listen(`rows:${id}`, rowEl, 'keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        openDetail(event);
      }
    });
  });

}

function renderRecentRows() {
  const rows = filteredRecentRows();
  renderRows('recentRows', rows, rows.length);
}

  return { renderRecentSearchButton, openRecentSearchSheet, renderRecentAccountScope, openRecentForAccount, renderRecentPeriodButtons, renderRecentSortButton, openRecentSortSheet, renderAccountFlowSummary, renderRows, renderRecentRows };
}
