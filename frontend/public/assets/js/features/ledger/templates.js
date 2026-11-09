import { accountLabel } from '../../api.js';
import { escapeHtml, formatSignedMoneyFixed2 as signedMoney, toFiniteNumber as num } from '../../utils.js';
import { RECENT_SORT_OPTIONS } from './state.js';
import { LEDGER_EDIT_TYPES, LEDGER_EDIT_DEFAULTS } from './legacy_model.js';

const EXPENSE_CATEGORY_ICONS = {
  餐饮: 'M4 3v5a3 3 0 0 0 6 0V3M7 3v18M18 3c-2 2-3 5-3 9h4M19 3v18',
  交通出行: 'M5 16v3m14-3v3M4 11l2-5h12l2 5v5H4zm0 0h16M7 13.5h1m8 0h1',
  居住: 'm3 10 9-7 9 7M5 9v12h14V9M10 21v-7h4v7',
  通讯网络: 'M3 9a14 14 0 0 1 18 0M6 12a9 9 0 0 1 12 0m-9 3a4.5 4.5 0 0 1 6 0m-3 4h.01',
  工具订阅: 'M4 6h16M4 12h16M4 18h16M8 4v4m8 2v4m-6 2v4',
  购物: 'M5 8h14l2 12H3zm4 0V6a3 3 0 0 1 6 0v2',
  医疗健康: 'M5 4h14a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1zm7 4v8m-4-4h8',
  娱乐休闲: 'M5 5h14v14H5zM10 9l5 3-5 3z',
  学习成长: 'M12 6c-3-2-6-2-9-1v14c3-1 6-1 9 1 3-2 6-2 9-1V5c-3-1-6-1-9 1zm0 0v14',
  人情往来: 'M3 9h18v4H3zm2 4v7h14v-7M12 9v11m0-11C7 9 6 4 9 4c2 0 3 5 3 5s1-5 3-5c3 0 2 5-3 5Z',
  '借还款/往来': 'M3 8h17m-4-4 4 4-4 4m5 4H4m4-4-4 4 4 4',
  其他支出: 'M5 12h.01m7 0h.01m7 0h.01',
  待确认支出: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zm-2.5 6a2.6 2.6 0 0 1 5 1c0 2-2.5 2-2.5 4m0 3h.01',
};

export function createLedgerTemplates({ state, model }) {
  const { isEditableLedgerRow, ledgerEditType, ledgerEditTypeLabel, ledgerEditActionLabel, ledgerEditSubmitLabel, rowTitle, originalCurrencyText, transferEndpointName, transferRoute, ledgerCategoryOptions, ledgerSubcategoryOptions } = model;

function recentSearchSheetHtml() {
  return `
    <form class="ledgerSearchSheet" data-recent-search-form>
      <input class="formInput" type="search" data-recent-search-input placeholder="搜索摘要 / 商户 / 粗分类" value="${escapeHtml(state.recentSearch)}" />
      <div class="ledgerSearchSheetActions">
        <button type="button" class="ledgerActionBtn ghost" data-recent-search-clear>清除</button>
        <button type="submit" class="ledgerActionBtn confirm">搜索</button>
      </div>
    </form>
  `;
}

function transferRouteHtml(row) {
  const route = transferRoute(row);
  if (!route) return '';
  return `
    <div class="transferRoute rowDetailFull">
      <div class="transferRouteHeader">${escapeHtml(route.label)}</div>
      <div class="transferRouteNode">
        <em>转出账户</em>
        <strong>${escapeHtml(transferEndpointName(route.from))}</strong>
      </div>
      <div class="transferRouteArrow" aria-hidden="true">→</div>
      <div class="transferRouteNode is-target">
        <em>转入账户</em>
        <strong>${escapeHtml(transferEndpointName(route.to))}</strong>
      </div>
    </div>
  `;
}

function rowDetailHtml(row) {
  const originalAmount = originalCurrencyText(row);
  const editButton = isEditableLedgerRow(row)
    ? `
      <div class="ledgerDetailActions rowDetailFull">
        <button type="button" class="ledgerActionBtn confirm" data-ledger-edit="${escapeHtml(String(row.id || ''))}">${escapeHtml(ledgerEditActionLabel(row))}</button>
      </div>
    `
    : '';
  return `
    <div class="rowDetailGrid">
      ${transferRouteHtml(row)}
      ${editButton}
      <div><strong>账户</strong><span>${escapeHtml(accountLabel(row.account_code) || row.account_code || '-')}</span></div>
      <div><strong>对方账户</strong><span>${escapeHtml(row.counter_account_code || '不适用')}</span></div>
      <div><strong>对方</strong><span>${escapeHtml(row.counterparty || '不适用')}</span></div>
      <div><strong>原始类型</strong><span>${escapeHtml(row.flow_type || '-')}</span></div>
      ${originalAmount ? `<div><strong>原币金额</strong><span>${escapeHtml(originalAmount)}</span></div>` : ''}
      <div><strong>粗分类</strong><span>${escapeHtml(row.coarse_category || '-')}</span></div>
      <div><strong>摘要</strong><span>${escapeHtml(row.summary || '-')}</span></div>
      <div><strong>说明</strong><span>${escapeHtml(row.description || '不适用')}</span></div>
      <div><strong>状态</strong><span>${row.needs_review ? '待确认' : '已归类'}</span></div>
    </div>
  `;
}

function optionHtml(options, selectedValue) {
  const selectedText = String(selectedValue || '').trim();
  return options.map((value) => `
    <option value="${escapeHtml(value)}" ${value === selectedText ? 'selected' : ''}>${escapeHtml(value)}</option>
  `).join('');
}

function expenseChoicesHtml(options, selectedValue, field) {
  return options.map(value => {
    const icon = field === 'category' ? EXPENSE_CATEGORY_ICONS[value] || EXPENSE_CATEGORY_ICONS.其他支出 : '';
    return `
      <button type="button" data-expense-choice="${field}" data-value="${escapeHtml(value)}"
        aria-pressed="${value === selectedValue}">
        ${icon ? `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${icon}"/></svg>` : ''}
        <span>${escapeHtml(value)}</span>
      </button>
    `;
  }).join('');
}

function ledgerEditSheetHtml(row) {
  const amount = num(row.amount_cny ?? row.amount);
  const displayAmount = (row.amount_cny == null && originalCurrencyText(row)) || signedMoney(amount);
  const account = accountLabel(row.account_code) || row.account_code || '-';
  const cashflowType = ledgerEditType(row);
  const isExpense = cashflowType === 'operating_expense';
  const defaultEdit = LEDGER_EDIT_DEFAULTS[cashflowType] || {};
  const hint = row.flow_type === 'unclassified' ? row.classification_hint : null;
  const rawCategory = String(hint?.category || row.category || '').trim();
  const rawSubcategory = String(hint?.subcategory || row.subcategory || '').trim();
  const category = rawCategory && rawCategory !== '未分类' ? rawCategory : (isExpense ? '' : defaultEdit.category || '');
  const subcategory = isExpense ? (category ? rawSubcategory : '') : rawSubcategory || defaultEdit.subcategory || '';
  return `
    <form class="ledgerConfirmSheet ledgerEditSheet" data-ledger-edit-form="${escapeHtml(String(row.id || ''))}">
      <div class="ledgerConfirmLead">${row.needs_review ? '请核对后保存分类。' : '保存后更新流水分类和收支统计。'}</div>
      ${hint ? `<p class="ledgerConfirmLead">词典建议，匹配到「${escapeHtml(hint.matched_keyword)}」。</p>` : ''}
      <div class="ledgerConfirmTitle">${escapeHtml(rowTitle(row))}</div>
      <div class="ledgerConfirmAmount ${amount >= 0 ? 'up' : 'down'}">${escapeHtml(displayAmount)}</div>
      <div class="ledgerConfirmMeta">
        <div>
          <span>日期 / 账户</span>
          <strong>${escapeHtml(row.transaction_date || '--')} / ${escapeHtml(account)}</strong>
        </div>
        <div>
          <span>当前口径</span>
          <strong>${escapeHtml(row.flow_type === 'unclassified' ? '待分类' : ledgerEditTypeLabel(cashflowType))} / ${escapeHtml(row.coarse_category || '-')}</strong>
        </div>
        <div>
          <span>对方</span>
          <strong>${escapeHtml(row.counterparty || row.summary || '未提供')}</strong>
        </div>
        <div>
          <span>说明</span>
          <strong>${escapeHtml(row.description || row.summary || '无')}</strong>
        </div>
      </div>
      <div class="ledgerEditGrid">
        <label>
          <span>流水口径</span>
          <select class="formInput" data-edit-field="cashflow_type" required>
            ${cashflowType ? '' : '<option value="" selected disabled>请选择流水口径</option>'}
            ${LEDGER_EDIT_TYPES.filter(([value, , redesignedOnly]) =>
              !redesignedOnly || row.classification_status || value === cashflowType).map(([value, label]) => `
              <option value="${escapeHtml(value)}" ${value === cashflowType ? 'selected' : ''}>${escapeHtml(label)}</option>
            `).join('')}
          </select>
        </label>
        <label data-ledger-standard-classification ${isExpense ? 'hidden' : ''}>
          <span>粗分类</span>
          <select class="formInput" data-edit-field="category">
            ${isExpense ? '<option value=""></option>' : ''}
            ${optionHtml(ledgerCategoryOptions(cashflowType, category), category)}
          </select>
        </label>
        <label data-ledger-standard-classification ${isExpense ? 'hidden' : ''}>
          <span>细分类</span>
          <select class="formInput" data-edit-field="subcategory">
            ${isExpense ? '<option value=""></option>' : ''}
            ${optionHtml(ledgerSubcategoryOptions(cashflowType, category, subcategory), subcategory)}
          </select>
        </label>
        <div class="ledgerExpensePicker ledgerEditFull" data-ledger-expense-picker ${isExpense ? '' : 'hidden'}>
          <div class="ledgerExpenseLegend" id="ledger-expense-category-label">消费分类</div>
          <div class="ledgerExpenseCategories" role="group" aria-labelledby="ledger-expense-category-label" data-expense-options="category"></div>
          <div class="ledgerExpenseDetails" data-expense-details hidden>
            <div class="ledgerExpenseLegend" id="ledger-expense-subcategory-label"></div>
            <div class="ledgerExpenseSubcategories" role="group" aria-labelledby="ledger-expense-subcategory-label" data-expense-options="subcategory"></div>
          </div>
        </div>
        <label data-ledger-counter-account ${isExpense ? 'hidden' : ''}>
          <span>对方账户标识</span>
          <input class="formInput" data-edit-field="counter_account_code" value="${escapeHtml(row.counter_account_code || '')}" placeholder="内部转账 / 入金可填" ${isExpense ? 'disabled' : ''} />
        </label>
        <label class="ledgerEditFull">
          <span>对方 / 商户</span>
          <input class="formInput" data-edit-field="counterparty" value="${escapeHtml(row.counterparty || '')}" />
        </label>
      </div>
      <div class="ledgerConfirmActions">
        <button type="button" class="ledgerActionBtn ghost" data-sheet-close>取消</button>
        <button type="submit" class="ledgerActionBtn confirm">${escapeHtml(ledgerEditSubmitLabel(row))}</button>
      </div>
    </form>
  `;
}

function recentSortSheetHtml() {
  return `
    <div class="ledgerOptionSheet">
      ${RECENT_SORT_OPTIONS.map((option) => `
        <button type="button" class="ledgerOptionButton ${option.value === state.recentAmountSort ? 'active' : ''}" data-recent-sort-option="${escapeHtml(option.value)}">
          <span>
            <strong>${escapeHtml(option.label)}</strong>
            <small>${escapeHtml(option.hint)}</small>
          </span>
          ${option.value === state.recentAmountSort ? '<em>✓</em>' : ''}
        </button>
      `).join('')}
    </div>
  `;
}

  return { recentSearchSheetHtml, transferRouteHtml, rowDetailHtml, optionHtml, expenseChoicesHtml, ledgerEditSheetHtml, recentSortSheetHtml };
}
