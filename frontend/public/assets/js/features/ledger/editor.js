import { isReadCancelled } from '../../shared/read_scope.js';
import { LEDGER_EDIT_DEFAULTS } from './legacy_model.js';
import { updateLedgerRow } from './requests.js';

const MERCHANT_RULES_KEY = 'pams.ledger.merchantRules.v1';

function merchantRuleKey(row) {
  const raw = String(row?.counterparty || row?.merchant || row?.summary || row?.description || '').trim();
  const cleaned = raw.replace(/\d{4,}/g, '').replace(/\s+/g, ' ').trim().slice(0, 18);
  return cleaned.length >= 2 ? cleaned.toLowerCase() : '';
}

function loadMerchantRules() {
  try { return JSON.parse(localStorage.getItem(MERCHANT_RULES_KEY) || '{}') || {}; } catch { return {}; }
}

function saveMerchantRule(row, rule) {
  const key = merchantRuleKey(row);
  if (!key || !rule?.category) return;
  try {
    const map = loadMerchantRules();
    map[key] = { ...rule, updatedAt: Date.now() };
    const entries = Object.entries(map).sort((a, b) => (b[1]?.updatedAt || 0) - (a[1]?.updatedAt || 0)).slice(0, 120);
    localStorage.setItem(MERCHANT_RULES_KEY, JSON.stringify(Object.fromEntries(entries)));
  } catch { /* ignore storage quota errors */ }
}

export function createLedgerEditor({ model, templates, sheets, dom, current, canEdit = () => true, reload }) {
  const { isEditableLedgerRow, findLedgerRowById, ledgerCategoryOptions, ledgerSubcategoryOptions, ledgerEditSubmitLabel, ledgerEditType } = model;
  const { optionHtml, expenseChoicesHtml, ledgerEditSheetHtml } = templates;

function updateLedgerCategoryOptions(form, cashflowType, preferredValue) {
  const category = form.querySelector('[data-edit-field="category"]');
  if (!category) return '';
  const currentValue = String((preferredValue ?? category.value) || '').trim();
  const options = ledgerCategoryOptions(cashflowType, currentValue);
  const isExpense = cashflowType === 'operating_expense';
  category.innerHTML = (isExpense ? '<option value=""></option>' : '') + optionHtml(options, currentValue);
  category.value = options.includes(currentValue) ? currentValue : (isExpense ? '' : options[0] || '');
  return category.value;
}

function updateLedgerSubcategoryOptions(form, cashflowType, preferredValue = '') {
  const categoryValue = form.querySelector('[data-edit-field="category"]')?.value || '';
  const subcategory = form.querySelector('[data-edit-field="subcategory"]');
  if (!subcategory) return;
  const options = ledgerSubcategoryOptions(cashflowType, categoryValue);
  const isExpense = cashflowType === 'operating_expense';
  const nextValue = options.includes(preferredValue) ? preferredValue : (isExpense ? '' : options[0] || '');
  subcategory.innerHTML = (isExpense ? '<option value=""></option>' : '') + optionHtml(options, nextValue);
  subcategory.value = nextValue;
}

function applyLedgerEditDefaults(form, cashflowType) {
  const defaults = LEDGER_EDIT_DEFAULTS[cashflowType] || {};
  if (cashflowType === 'operating_expense') {
    updateLedgerCategoryOptions(form, cashflowType, '');
    updateLedgerSubcategoryOptions(form, cashflowType);
    return;
  }
  updateLedgerCategoryOptions(form, cashflowType, defaults.category);
  updateLedgerSubcategoryOptions(form, cashflowType, defaults.subcategory);
}

function bindLedgerEditSheet(row, session) {
  const form = session.body.querySelector('[data-ledger-edit-form]');
  if (!form) return;
  const field = name => form.querySelector(`[data-edit-field="${name}"]`);
  const fieldValue = name => field(name)?.value?.trim() || '';
  const isExpense = () => fieldValue('flow_group') === 'operating_expense';
  const isTransfer = () => fieldValue('flow_group') === 'internal_transfer';
  const classificationType = () => isTransfer() ? fieldValue('transfer_type') : fieldValue('flow_group');
  const initialType = ledgerEditType(row);
  const initialCategory = fieldValue('category');
  const initialSubcategory = fieldValue('subcategory');
  const counterAccount = field('counter_account_code');
  const expensePicker = form.querySelector('[data-ledger-expense-picker]');
  const submitBtn = form.querySelector('button[type="submit"]');
  let pending = false;

  function syncSubmit() {
    if (submitBtn) submitBtn.disabled = pending || (isExpense() && (!fieldValue('category') || !fieldValue('subcategory')));
  }

  function showExpenseSubcategories() {
    const category = fieldValue('category');
    const details = expensePicker.querySelector('[data-expense-details]');
    details.hidden = !category;
    details.querySelector('.ledgerExpenseLegend').textContent = category ? `${category} · 细分类` : '';
    const options = category
      ? ledgerSubcategoryOptions('operating_expense', category, fieldValue('subcategory'))
      : [];
    details.querySelector('[data-expense-options="subcategory"]').innerHTML =
      expenseChoicesHtml(options, fieldValue('subcategory'), 'subcategory');
    return details;
  }

  function syncClassificationFields() {
    const expense = isExpense();
    const transfer = isTransfer();
    const principal = classificationType().startsWith('personal_loan_');
    form.querySelector('[data-ledger-income-category]').hidden = expense || transfer || principal;
    form.querySelector('[data-ledger-standard-classification]').hidden = expense || principal;
    form.querySelector('[data-ledger-transfer-type]').hidden = !transfer;
    const canEditCounterAccount = transfer && !row.classification_status;
    counterAccount.disabled = !canEditCounterAccount;
    counterAccount.closest('label').hidden = !canEditCounterAccount;
    if (expensePicker) {
      expensePicker.hidden = !expense;
      if (expense) {
        const options = ledgerCategoryOptions('operating_expense', fieldValue('category'));
        expensePicker.querySelector('[data-expense-options="category"]').innerHTML =
          expenseChoicesHtml(options, fieldValue('category'), 'category');
        showExpenseSubcategories();
      }
    }
    syncSubmit();
  }

  session.listen(form.querySelector('[data-sheet-close]'), 'click', event => {
    event.preventDefault();
    session.close();
  });
  session.listen(field('flow_group'), 'change', () => {
    field('transfer_type').value = 'internal_transfer';
    applyLedgerEditDefaults(form, classificationType());
    syncClassificationFields();
  });
  session.listen(field('transfer_type'), 'change', () => applyLedgerEditDefaults(form, classificationType()));
  session.listen(field('category'), 'change', () => updateLedgerSubcategoryOptions(form, classificationType()));
  if (expensePicker) session.listen(expensePicker, 'click', event => {
    const button = event.target.closest('[data-expense-choice]');
    if (!button || button.getAttribute('aria-pressed') === 'true') return;
    const name = button.dataset.expenseChoice;
    field(name).value = button.dataset.value;
    for (const choice of button.parentElement.children) {
      choice.setAttribute('aria-pressed', String(choice === button));
    }
    if (name === 'category') {
      updateLedgerSubcategoryOptions(form, classificationType());
      showExpenseSubcategories().scrollIntoView({ block: 'nearest' });
    }
    syncSubmit();
  });
  syncClassificationFields();

  // Phase 3.2: Merchant rule memory 1-click suggestion chip
  const rememberedKey = merchantRuleKey(row);
  const rememberedRule = rememberedKey ? loadMerchantRules()[rememberedKey] : null;
  if (rememberedRule && rememberedRule.category && (row.needs_review || !initialCategory || initialCategory !== rememberedRule.category)) {
    const hint = document.createElement('div');
    hint.className = 'ledgerRuleMemoryHint';
    hint.style.cssText = 'display:flex;align-items:center;justify-content:space-between;gap:8px;padding:8px 10px;margin-bottom:10px;border-radius:10px;background:rgba(56,189,248,0.1);border:1px solid rgba(56,189,248,0.28);font-size:12px;';
    hint.innerHTML = `<span style="color:#e0f2fe">规则记忆：<b>${rememberedRule.category}${rememberedRule.subcategory ? ' · ' + rememberedRule.subcategory : ''}</b></span><button type="button" class="btn" style="padding:4px 10px;font-size:11px" data-apply-merchant-rule>一键套用</button>`;
    form.prepend(hint);
    session.listen(hint.querySelector('[data-apply-merchant-rule]'), 'click', () => {
      const targetGroup = rememberedRule.cashflow_type === 'operating_income'
        ? 'operating_income'
        : rememberedRule.cashflow_type === 'operating_expense' || rememberedRule.cashflow_type === 'fee'
          ? 'operating_expense'
          : 'internal_transfer';
      if (field('flow_group')) field('flow_group').value = targetGroup;
      if (targetGroup === 'internal_transfer' && field('transfer_type')) {
        field('transfer_type').value = rememberedRule.cashflow_type || 'internal_transfer';
      }
      updateLedgerCategoryOptions(form, classificationType(), rememberedRule.category);
      updateLedgerSubcategoryOptions(form, classificationType(), rememberedRule.subcategory || '');
      syncClassificationFields();
      hint.remove();
    });
  }

  session.listen(form, 'submit', async event => {
    event.preventDefault();
    if (submitBtn?.disabled || pending || !current() || !session.active()) return;
    pending = true;
    if (submitBtn) {
      submitBtn.disabled = true;
      submitBtn.textContent = '保存中...';
    }
    let saved = false;
    try {
      const category = fieldValue('category');
      const subcategory = fieldValue('subcategory');
      const selectedFee = category === '金融费用' && subcategory === '银行费用';
      const retainedFee = initialType === 'fee' && category === initialCategory && subcategory === initialSubcategory;
      const cashflowType = isExpense() && row.classification_status && (selectedFee || retainedFee)
        ? 'fee' : classificationType();
      await updateLedgerRow(row, {
        cashflow_type: cashflowType,
        category,
        subcategory,
        ...(!counterAccount.disabled && { counter_account_code: fieldValue('counter_account_code') }),
        counterparty: fieldValue('counterparty'),
      });
      saveMerchantRule(row, { cashflow_type: cashflowType, category, subcategory });
      saved = true;
      if (!current() || !session.active()) return;
      session.close();
      await reload();
    } catch (err) {
      if (!current() || isReadCancelled(err) || (!saved && !session.active())) return;
      dom.showError(err);
      if (session.active()) {
        pending = false;
        if (submitBtn) {
          syncSubmit();
          submitBtn.textContent = ledgerEditSubmitLabel(row);
        }
      }
    }
  });
}

function openLedgerEdit(row) {
  if (!current() || !canEdit() || !row || !isEditableLedgerRow(row)) return;
  const session = sheets.open(row.needs_review ? '确认流水' : '修改流水', ledgerEditSheetHtml(row));
  if (session) bindLedgerEditSheet(row, session);
}

function onEditClick(event, scope) {
  const btn = event.target?.closest?.('[data-ledger-edit]');
  if (!btn || !scope.contains(btn)) return;
  event.preventDefault();
  event.stopPropagation();
  openLedgerEdit(findLedgerRowById(btn.dataset.ledgerEdit));
}

  return { openLedgerEdit, onEditClick };
}
