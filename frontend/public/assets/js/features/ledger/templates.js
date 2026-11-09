import { accountLabel } from '../../api.js';
import { escapeHtml, formatSignedMoneyFixed2 as signedMoney, toFiniteNumber as num } from '../../utils.js';
import { RECENT_SORT_OPTIONS } from './state.js';
import { LEDGER_EDIT_TYPES, LEDGER_EDIT_DEFAULTS } from './legacy_model.js';

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

function ledgerEditSheetHtml(row) {
  const amount = num(row.amount_cny ?? row.amount);
  const displayAmount = (row.amount_cny == null && originalCurrencyText(row)) || signedMoney(amount);
  const account = accountLabel(row.account_code) || row.account_code || '-';
  const cashflowType = ledgerEditType(row);
  const defaultEdit = LEDGER_EDIT_DEFAULTS[cashflowType] || {};
  const hint = row.flow_type === 'unclassified' ? row.classification_hint : null;
  const rawCategory = String(hint?.category || row.category || '').trim();
  const rawSubcategory = String(hint?.subcategory || row.subcategory || '').trim();
  const category = rawCategory && rawCategory !== '未分类' ? rawCategory : (defaultEdit.category || '');
  const subcategory = rawSubcategory || defaultEdit.subcategory || '';
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
        <label>
          <span>粗分类</span>
          <select class="formInput" data-edit-field="category">
            ${optionHtml(ledgerCategoryOptions(cashflowType, category), category)}
          </select>
        </label>
        <label>
          <span>细分类</span>
          <select class="formInput" data-edit-field="subcategory">
            ${optionHtml(ledgerSubcategoryOptions(cashflowType, category, subcategory), subcategory)}
          </select>
        </label>
        <label>
          <span>对方账户标识</span>
          <input class="formInput" data-edit-field="counter_account_code" value="${escapeHtml(row.counter_account_code || '')}" placeholder="内部转账 / 入金可填" />
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

  return { recentSearchSheetHtml, transferRouteHtml, rowDetailHtml, optionHtml, ledgerEditSheetHtml, recentSortSheetHtml };
}
