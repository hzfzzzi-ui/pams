import { isReadCancelled } from '../../shared/read_scope.js';
import { LEDGER_EDIT_DEFAULTS } from './legacy_model.js';
import { updateLedgerRow } from './requests.js';

export function createLedgerEditor({ model, templates, sheets, dom, current, reload }) {
  const { isEditableLedgerRow, findLedgerRowById, ledgerCategoryOptions, ledgerSubcategoryOptions, ledgerEditSubmitLabel } = model;
  const { optionHtml, expenseChoicesHtml, ledgerEditSheetHtml } = templates;

function updateLedgerCategoryOptions(form, preferredValue) {
  const cashflowType = form.querySelector('[data-edit-field="cashflow_type"]')?.value || '';
  const category = form.querySelector('[data-edit-field="category"]');
  if (!category) return '';
  const currentValue = String((preferredValue ?? category.value) || '').trim();
  const options = ledgerCategoryOptions(cashflowType, currentValue);
  const isExpense = cashflowType === 'operating_expense';
  category.innerHTML = (isExpense ? '<option value=""></option>' : '') + optionHtml(options, currentValue);
  category.value = options.includes(currentValue) ? currentValue : (isExpense ? '' : options[0] || '');
  return category.value;
}

function updateLedgerSubcategoryOptions(form, preferredValue = '') {
  const cashflowType = form.querySelector('[data-edit-field="cashflow_type"]')?.value || '';
  const categoryValue = form.querySelector('[data-edit-field="category"]')?.value || '';
  const subcategory = form.querySelector('[data-edit-field="subcategory"]');
  if (!subcategory) return;
  const options = ledgerSubcategoryOptions(cashflowType, categoryValue);
  const isExpense = cashflowType === 'operating_expense';
  const nextValue = options.includes(preferredValue) ? preferredValue : (isExpense ? '' : options[0] || '');
  subcategory.innerHTML = (isExpense ? '<option value=""></option>' : '') + optionHtml(options, nextValue);
  subcategory.value = nextValue;
}

function applyLedgerEditDefaults(form) {
  const cashflowType = form.querySelector('[data-edit-field="cashflow_type"]')?.value || '';
  const defaults = LEDGER_EDIT_DEFAULTS[cashflowType] || {};
  if (cashflowType === 'operating_expense') {
    updateLedgerCategoryOptions(form, '');
    updateLedgerSubcategoryOptions(form);
    return;
  }
  updateLedgerCategoryOptions(form, defaults.category || undefined);
  updateLedgerSubcategoryOptions(form, defaults.subcategory);
}

function bindLedgerEditSheet(row, session) {
  const form = session.body.querySelector('[data-ledger-edit-form]');
  if (!form) return;
  const field = name => form.querySelector(`[data-edit-field="${name}"]`);
  const fieldValue = name => field(name)?.value?.trim() || '';
  const isExpense = () => fieldValue('cashflow_type') === 'operating_expense';
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
    if (expensePicker) {
      expensePicker.hidden = !expense;
      for (const label of form.querySelectorAll('[data-ledger-standard-classification], [data-ledger-counter-account]')) {
        label.hidden = expense;
      }
      field('counter_account_code').disabled = expense;
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
  session.listen(field('cashflow_type'), 'change', () => {
    applyLedgerEditDefaults(form);
    syncClassificationFields();
  });
  session.listen(field('category'), 'change', () => updateLedgerSubcategoryOptions(form));
  if (expensePicker) session.listen(expensePicker, 'click', event => {
    const button = event.target.closest('[data-expense-choice]');
    if (!button || button.getAttribute('aria-pressed') === 'true') return;
    const name = button.dataset.expenseChoice;
    field(name).value = button.dataset.value;
    for (const choice of button.parentElement.children) {
      choice.setAttribute('aria-pressed', String(choice === button));
    }
    if (name === 'category') {
      updateLedgerSubcategoryOptions(form);
      showExpenseSubcategories().scrollIntoView({ block: 'nearest' });
    }
    syncSubmit();
  });
  syncClassificationFields();
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
      await updateLedgerRow(row, {
        cashflow_type: fieldValue('cashflow_type'),
        category: fieldValue('category'),
        subcategory: fieldValue('subcategory'),
        ...(!isExpense() && { counter_account_code: fieldValue('counter_account_code') }),
        counterparty: fieldValue('counterparty'),
      });
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
  if (!current() || !row || !isEditableLedgerRow(row)) return;
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
