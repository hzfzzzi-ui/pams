import { escapeHtml, formatSignedMoneyFixed2 as signedMoney, toFiniteNumber as num } from '../../utils.js';
import { accountInstitution } from '../../shared/account_institutions.js';
import { categoryIconPath } from './category_icons.js';
import { RECENT_SORT_OPTIONS } from './state.js';
import { renderLedgerDays } from './days.js';
import { currentBusinessMonth } from './transfers.js';

const accountGlyph = '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="8" r="3"/><path d="M5 20v-2a7 7 0 0 1 14 0v2"/></svg>';

function accountAvatar(groupCode) {
  const { logoUrl } = accountInstitution(groupCode);
  return logoUrl ? `<img src="${escapeHtml(logoUrl)}" alt="">` : accountGlyph;
}

export function createLedgerLists({ root, host, state, model, templates, dom, events, sheets, showDetails, renderTransferPair,
  reloadRecent = () => renderRecentRows(), isTransferReview, loadMore }) {
  const { $ } = dom;
  const { rowAmountClass, rowCategoryText, rowTitle, rowDateText, originalCurrencyText,
    accountDisplayName, ledgerAccountIdentity, filteredRecentRows, isInternalTransferRow } = model;
  const { recentSearchSheetHtml, rowDetailHtml, recentSortSheetHtml } = templates;
  let renderedAccountOptions = '';

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
    reloadRecent();
    session.close();
  });
  session.listen(session.body.querySelector('[data-recent-search-clear]'), 'click', () => {
    state.recentSearch = '';
    renderRecentSearchButton();
    reloadRecent();
    session.close();
  });
}

function renderRecentAccountPicker() {
  const toggle = $('recentAccountToggle');
  if (!toggle) return;
  const picker = $('recentAccountPicker');
  const code = state.recentAccountFilter;
  const groupCode = state.recentAccountGroup;
  const groups = state.streamAccountGroups;
  const group = groups.find(item => groupCode ? item.code === groupCode
    : item.accounts.some(account => account.code === code));
  const label = code ? group?.accounts.find(account => account.code === code)?.label || accountDisplayName(code)
    : group?.label || groupCode;
  const title = label ? `筛选账户（${label}）` : '筛选账户';
  toggle.title = title;
  toggle.setAttribute('aria-label', title);
  toggle.querySelector('.ledgerAccountAvatar').innerHTML = accountAvatar(group?.code || groupCode);
  $('recentAccountLabel').textContent = label ? ` · ${label}` : '';
  if (picker.hidden) return;

  const option = ({ label, account = '', institution = '' }) => {
    const active = code === account && groupCode === institution;
    return `<button type="button" class="ledgerAccountOption${account ? ' ledgerAccountOptionChild' : ''}"
      data-account="${escapeHtml(account)}" data-institution="${escapeHtml(institution)}" aria-pressed="${active}">
      ${account ? '' : `<span class="ledgerAccountAvatar" aria-hidden="true">${accountAvatar(institution)}</span>`}
      <span>${escapeHtml(label)}</span>${active ? '<span class="ledgerAccountCheck" aria-hidden="true">✓</span>' : ''}
    </button>`;
  };
  const options = option({ label: '全部账户' }) + groups.map(item =>
    option({ label: item.label, institution: item.code }) +
    item.accounts.map(account => option({ label: account.label, account: account.code })).join('')
  ).join('');
  // Keep the focused control and scroll position when the same choices arrive with another page.
  if (renderedAccountOptions !== options) {
    picker.innerHTML = options;
    renderedAccountOptions = options;
  }
}

function closeRecentAccountPicker({ restoreFocus = false } = {}) {
  const picker = $('recentAccountPicker');
  if (!picker || picker.hidden) return;
  picker.hidden = true;
  const toggle = $('recentAccountToggle');
  toggle.setAttribute('aria-expanded', 'false');
  if (restoreFocus) toggle.focus();
}

function bindRecentAccountPicker() {
  const control = $('recentAccountControl');
  if (!control) return;
  const toggle = $('recentAccountToggle');
  const picker = $('recentAccountPicker');
  events.listen('page', toggle, 'click', () => {
    if (!picker.hidden) {
      closeRecentAccountPicker();
      return;
    }
    picker.hidden = false;
    toggle.setAttribute('aria-expanded', 'true');
    renderRecentAccountPicker();
    (picker.querySelector('[aria-pressed="true"]') || picker.firstElementChild)?.focus();
  });
  events.listen('page', picker, 'click', event => {
    const option = event.target.closest('[data-account]');
    if (!option) return;
    const { account, institution } = option.dataset;
    closeRecentAccountPicker({ restoreFocus: true });
    if (state.recentAccountFilter === account && state.recentAccountGroup === institution) return;
    state.recentAccountFilter = account;
    state.recentAccountGroup = institution;
    renderRecentAccountPicker();
    reloadRecent();
  });
  events.listen('page', root, 'click', event => {
    if (!event.composedPath().includes(control)) closeRecentAccountPicker();
  });
  events.listen('page', control, 'keydown', event => {
    if (event.key === 'Escape' && !picker.hidden) {
      event.preventDefault();
      closeRecentAccountPicker({ restoreFocus: true });
    }
  });
}

function recentPeriodLabel() {
  const period = isTransferReview() ? state.transferPeriod : state.recentPeriod;
  if (period === 'history') return state.transferMonth || '历史月份';
  return period === 'month' ? '本月至今' : '年初至今';
}

function renderRecentPeriodButton() {
  const toggle = $('recentPeriodToggle');
  if (!toggle) return;
  const label = `流水时间范围（${recentPeriodLabel()}）`;
  toggle.title = label;
  toggle.setAttribute('aria-label', label);
  const periodLabel = $('recentPeriodLabel');
  if (periodLabel) periodLabel.textContent = recentPeriodLabel();
}

function closeRecentPeriod({ restoreFocus = false } = {}) {
  const form = $('recentPeriodForm');
  if (!form || form.hidden) return;
  form.hidden = true;
  const toggle = $('recentPeriodToggle');
  toggle.setAttribute('aria-expanded', 'false');
  if (restoreFocus) toggle.focus();
}

function bindRecentPeriod() {
  const control = $('recentPeriodControl');
  if (!control) return;
  const toggle = $('recentPeriodToggle');
  const form = $('recentPeriodForm');
  const selected = form.elements.namedItem('recentPeriod');
  const month = $('transferMonth');
  function updateMonthField() {
    const history = isTransferReview() && selected.value === 'history';
    $('transferMonthPicker').hidden = !history;
    month.required = history;
    month.disabled = !history;
  }
  events.listen('page', toggle, 'click', () => {
    if (!form.hidden) {
      closeRecentPeriod();
      return;
    }
    $('transferHistoryOption').hidden = !isTransferReview();
    selected.value = isTransferReview() ? state.transferPeriod : state.recentPeriod;
    month.value = state.transferMonth;
    month.max = currentBusinessMonth();
    month.setCustomValidity('');
    updateMonthField();
    form.hidden = false;
    toggle.setAttribute('aria-expanded', 'true');
    form.querySelector('input:checked')?.focus();
  });
  events.listen('page', form, 'change', updateMonthField);
  events.listen('page', month, 'input', () => month.setCustomValidity(''));
  events.listen('page', form, 'submit', event => {
    event.preventDefault();
    if (isTransferReview()) {
      if (selected.value === 'history') {
        if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(month.value) || month.value > month.max) {
          month.setCustomValidity('请选择有效的历史月份。');
          month.reportValidity();
          return;
        }
        state.transferMonth = month.value;
      }
      state.transferPeriod = selected.value;
    } else {
      state.recentPeriod = selected.value;
    }
    renderRecentPeriodButton();
    reloadRecent('period');
    closeRecentPeriod({ restoreFocus: true });
  });
  events.listen('page', root, 'click', event => {
    if (!control.contains(event.target)) closeRecentPeriod();
  });
  events.listen('page', control, 'keydown', event => {
    if (event.key === 'Escape' && !form.hidden) {
      event.preventDefault();
      closeRecentPeriod({ restoreFocus: true });
    }
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
    reloadRecent();
    session.close();
  });
}

function renderRows(id, rows) {
  events.clear(`rows:${id}`);
  const el = $(id);
  if (!el) return;
  if (!rows?.length) {
    el.innerHTML = '<div class="ledgerEmpty">暂无流水</div>';
    return;
  }
  const showRowDate = Boolean(state.recentAmountSort);
  const renderRow = (row, index) => {
    if (row.transfer_pair) return renderTransferPair(row.transfer_pair);
    const rawId = row.id || row.transaction_id || `${row.transaction_date || 'row'}-${index}`;
    const detailId = `${id}-detail-${String(rawId).replace(/[^a-zA-Z0-9_-]/g, '_')}`;
    const amount = num(row.amount_cny ?? row.amount);
    const amountClass = rowAmountClass(row, amount);
    const title = rowTitle(row);
    const dateText = rowDateText(row);
    const account = ledgerAccountIdentity(row.account_code);
    const accountName = [account.institution, account.name].filter(Boolean).join(' ');
    const pending = row.subcategory_pending || row.flow_type === 'unclassified';
    const primaryCategory = String(row.category || '').trim();
    const transfer = isInternalTransferRow(row);
    const category = transfer ? '内部划转' : pending ? '待补分类' : ['收入', '消费', '内部划转', ''].includes(primaryCategory)
      ? rowCategoryText(row) : primaryCategory;
    const icon = transfer ? categoryIconPath('借还款/往来') : pending ? categoryIconPath('其他支出')
      : categoryIconPath(row.category, row.subcategory || row.coarse_category,
        row.flow_type === 'income' ? '其他收入' : '其他支出');
    const originalAmount = originalCurrencyText(row);
    const showOriginal = row.amount_cny == null && originalAmount;
    const displayAmount = row.amount_cny == null && row.amount == null ? '—' : showOriginal || signedMoney(amount);
    return `
      <button type="button" class="ledgerListRow" data-detail-target="${escapeHtml(detailId)}" title="点击查看账户、对方和原始摘要">
        <span class="ledgerStreamCategoryIcon" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="${icon}"/></svg></span>
        <span class="ledgerListMain">
          <strong title="${escapeHtml(title)}">${escapeHtml(title)}</strong>
          <small class="ledgerListAccount">${category
            ? `<span class="${pending ? 'ledgerReviewHint' : ''}">${escapeHtml(category)}</span> · ` : ''}${escapeHtml(accountName)}</small>
          ${showRowDate ? `<time class="ledgerListDate" datetime="${escapeHtml(dateText)}">${escapeHtml(dateText)}</time>` : ''}
        </span>
        <span class="ledgerListSide">
          <b class="${displayAmount === '—' ? '' : amountClass}">${escapeHtml(displayAmount)}</b>
          ${originalAmount && !showOriginal ? `<small class="ledgerOriginalAmount">原币 ${escapeHtml(originalAmount)}</small>` : ''}
          ${row.needs_review && !pending ? '<small class="ledgerReviewHint">待确认</small>' : ''}
        </span>
      </button>
      <div id="${escapeHtml(detailId)}" class="ledgerInlineDetail" hidden>
        ${rowDetailHtml(row)}
      </div>
    `;
  };

  if (showRowDate) {
    el.innerHTML = rows.map(renderRow).join('');
  } else {
    el.innerHTML = renderLedgerDays(rows, rowDateText, renderRow, {
      countUnit: rows.every(row => row.transfer_pair) ? '对' : '笔',
      partialLastDay: state.streamNext != null,
    });
  }

  el.querySelectorAll('.ledgerListRow').forEach((rowEl) => {
    const openDetail = () => {
      const detail = $(rowEl.dataset.detailTarget);
      if (!detail) return;
      if (host.matchMedia('(max-width: 760px)').matches) {
        const title = rowEl.querySelector('.ledgerListMain strong')?.innerText || '流水详情';
        showDetails(title, detail.innerHTML);
        return;
      }
      detail.hidden = !detail.hidden;
      rowEl.classList.toggle('open', !detail.hidden);
      rowEl.setAttribute('aria-expanded', String(!detail.hidden));
    };
    events.listen(`rows:${id}`, rowEl, 'click', openDetail);
  });

}

function renderRecentRows() {
  const rows = state.streamRows || filteredRecentRows();
  renderRows('recentRows', rows);
  const element = $('recentRows');
  if (element && state.streamNext != null) {
    element.insertAdjacentHTML('beforeend', '<button type="button" class="btn ledgerPairMore" data-more-stream>展开更多</button>');
    const button = element.querySelector('[data-more-stream]');
    events.listen('rows:recentRows', button, 'click', () => loadMore());
  }
}

  return { renderRecentSearchButton, openRecentSearchSheet, renderRecentAccountPicker, bindRecentAccountPicker, closeRecentAccountPicker, renderRecentPeriodButton,
    bindRecentPeriod, closeRecentPeriod, renderRecentSortButton, openRecentSortSheet, renderRecentRows };
}
