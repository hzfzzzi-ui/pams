import { accountLabel } from '../../api.js';
import { escapeHtml, formatSignedMoneyFixed2 as signedMoney, toFiniteNumber as num } from '../../utils.js';
import { RECENT_SORT_OPTIONS } from './state.js';
import { categoryIconPath } from './category_icons.js';
import { LEDGER_EDIT_GROUPS, LEDGER_EDIT_TYPES, LEDGER_EDIT_DEFAULTS, ledgerEditGroup } from './legacy_model.js';


export function createLedgerTemplates({ state, model }) {
  const { isEditableLedgerRow, ledgerEditType, ledgerEditCategory, ledgerEditTypeLabel, ledgerEditActionLabel, ledgerEditSubmitLabel, rowTitle, originalCurrencyText, transferEndpointName, transferRoute, ledgerCategoryOptions, ledgerSubcategoryOptions } = model;

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
  const editButton = !state.streamIsRemembered && isEditableLedgerRow(row)
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
      <div><strong>日期</strong><span>${escapeHtml(row.transaction_date || '—')}</span></div>
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
    const icon = field === 'category' ? categoryIconPath(value) : '';
    return `
      <button type="button" data-expense-choice="${field}" data-value="${escapeHtml(value)}"
        aria-pressed="${value === selectedValue}">
        ${icon ? `<span class="ledgerExpenseIcon" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="${icon}"/></svg></span>` : ''}
        <span>${escapeHtml(value)}</span>
      </button>
    `;
  }).join('');
}

function ledgerEditSheetHtml(row) {
  const amount = num(row.amount_cny ?? row.amount);
  const displayAmount = (row.amount_cny == null && originalCurrencyText(row)) || signedMoney(amount);
  const account = accountLabel(row.account_code) || row.account_code || '-';
  const currentType = ledgerEditType(row);
  const group = ledgerEditGroup(currentType, amount);
  const cashflowType = currentType || group;
  const isExpense = group === 'operating_expense';
  const isTransfer = group === 'internal_transfer';
  const categoryType = isExpense ? group : cashflowType;
  const showCounterAccount = isTransfer && !row.classification_status;
  const defaultEdit = LEDGER_EDIT_DEFAULTS[cashflowType] || {};
  const hint = (row.flow_type === 'unclassified' || row.subcategory_pending) && row.classification_hint?.cashflow_type === cashflowType
    ? row.classification_hint : null;
  const rawSubcategory = String(hint?.subcategory || row.subcategory || '').trim();
  const rawCategory = ledgerEditCategory(categoryType, String(hint?.category || row.category || '').trim(), rawSubcategory);
  const category = rawCategory && rawCategory !== '未分类' ? rawCategory : (isExpense && cashflowType !== 'fee' ? '' : defaultEdit.category || '');
  const subcategory = isExpense && cashflowType !== 'fee' ? (category ? rawSubcategory : '') : rawSubcategory || defaultEdit.subcategory || '';
  return `
    <form class="ledgerConfirmSheet ledgerEditSheet" data-ledger-edit-form="${escapeHtml(String(row.id || ''))}">
      ${hint ? `<p class="ledgerConfirmLead">词典建议，匹配到「${escapeHtml(hint.matched_keyword)}」。</p>` : ''}
      <div class="ledgerEditSummary">
        <div class="ledgerConfirmLead">${row.needs_review ? '请核对后保存分类。' : '保存后更新流水分类和收支统计。'}</div>
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
      </div>
      <div class="ledgerEditGrid">
        <label class="ledgerEditType">
          <span>流水分类</span>
          <select class="formInput" data-edit-field="flow_group" required>
            ${LEDGER_EDIT_GROUPS.map(([value, label]) => `
              <option value="${escapeHtml(value)}" ${value === group ? 'selected' : ''}>${escapeHtml(label)}</option>
            `).join('')}
          </select>
        </label>
        <label data-ledger-transfer-type ${isTransfer ? '' : 'hidden'}>
          <span>转账类型</span>
          <select class="formInput" data-edit-field="transfer_type">
            ${LEDGER_EDIT_TYPES.filter(([value, , redesignedOnly]) => ledgerEditGroup(value) === 'internal_transfer'
              && (!redesignedOnly || row.classification_status || value === cashflowType)).map(([value, label]) => `
              <option value="${escapeHtml(value)}" ${value === (isTransfer ? cashflowType : 'internal_transfer') ? 'selected' : ''}>${escapeHtml(label)}</option>
            `).join('')}
          </select>
        </label>
        <label data-ledger-income-category ${group === 'operating_income' ? '' : 'hidden'}>
          <span>收入分类</span>
          <select class="formInput" data-edit-field="category">
            ${isExpense ? '<option value=""></option>' : ''}
            ${optionHtml(ledgerCategoryOptions(categoryType, category), category)}
          </select>
        </label>
        <label data-ledger-standard-classification ${isExpense ? 'hidden' : ''}>
          <span>细分类</span>
          <select class="formInput" data-edit-field="subcategory">
            ${isExpense ? '<option value=""></option>' : ''}
            ${optionHtml(ledgerSubcategoryOptions(categoryType, category, subcategory), subcategory)}
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
        <label data-ledger-counter-account ${showCounterAccount ? '' : 'hidden'}>
          <span>对方账户标识</span>
          <input class="formInput" data-edit-field="counter_account_code" value="${escapeHtml(row.counter_account_code || '')}" placeholder="内部转账 / 入金可填" ${showCounterAccount ? '' : 'disabled'} />
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
