import { LEDGER_EDIT_DEFAULTS } from './legacy_model.js?v=20261031-ledger-modules-v1';
import { updateLedgerRow } from './requests.js?v=20261031-ledger-modules-v1';

export function createLedgerEditor({ model, templates, sheets, dom, current, reload }) {
  const { isEditableLedgerRow, findLedgerRowById, ledgerCategoryOptions, ledgerSubcategoryOptions, ledgerEditSubmitLabel } = model;
  const { optionHtml, ledgerEditSheetHtml } = templates;

function updateLedgerCategoryOptions(form, preferredValue) {
  const cashflowType = form.querySelector('[data-edit-field="cashflow_type"]')?.value || '';
  const category = form.querySelector('[data-edit-field="category"]');
  if (!category) return '';
  const currentValue = String((preferredValue ?? category.value) || '').trim();
  const options = ledgerCategoryOptions(cashflowType, currentValue);
  category.innerHTML = optionHtml(options, currentValue);
  category.value = options.includes(currentValue) ? currentValue : (options[0] || '');
  return category.value;
}

function updateLedgerSubcategoryOptions(form, preferredValue) {
  const categoryValue = form.querySelector('[data-edit-field="category"]')?.value || '';
  const subcategory = form.querySelector('[data-edit-field="subcategory"]');
  if (!subcategory) return '';
  const options = ledgerSubcategoryOptions(categoryValue, preferredValue ?? subcategory.value);
  const nextValue = preferredValue === null
    ? (options[0] || '')
    : String((preferredValue ?? subcategory.value) || '').trim();
  subcategory.innerHTML = optionHtml(options, nextValue);
  subcategory.value = options.includes(nextValue) ? nextValue : (options[0] || '');
  return subcategory.value;
}

function applyLedgerEditDefaults(form) {
  const cashflowType = form.querySelector('[data-edit-field="cashflow_type"]')?.value || '';
  const defaults = LEDGER_EDIT_DEFAULTS[cashflowType] || {};
  updateLedgerCategoryOptions(form, defaults.category || undefined);
  updateLedgerSubcategoryOptions(form, defaults.subcategory || undefined);
}

function bindLedgerEditSheet(row, session) {
  const form = session.body.querySelector('[data-ledger-edit-form]');
  if (!form) return;
  let pending = false;
  session.listen(form.querySelector('[data-sheet-close]'), 'click', event => {
    event.preventDefault();
    session.close();
  });
  session.listen(form.querySelector('[data-edit-field="cashflow_type"]'), 'change', () => applyLedgerEditDefaults(form));
  session.listen(form.querySelector('[data-edit-field="category"]'), 'change', () => updateLedgerSubcategoryOptions(form, null));
  session.listen(form, 'submit', async event => {
    event.preventDefault();
    if (pending || !current() || !session.active()) return;
    pending = true;
    const submitBtn = form.querySelector('button[type="submit"]');
    if (submitBtn) {
      submitBtn.disabled = true;
      submitBtn.textContent = '保存中...';
    }
    const fieldValue = name => form.querySelector(`[data-edit-field="${name}"]`)?.value?.trim() || '';
    let saved = false;
    try {
      await updateLedgerRow(row, {
        cashflow_type: fieldValue('cashflow_type'),
        category: fieldValue('category'),
        subcategory: fieldValue('subcategory'),
        counter_account_code: fieldValue('counter_account_code'),
        counterparty: fieldValue('counterparty'),
      });
      saved = true;
      if (!current() || !session.active()) return;
      session.close();
      await reload();
    } catch (err) {
      if (!current() || (!saved && !session.active())) return;
      dom.showError(err);
      if (session.active()) {
        pending = false;
        if (submitBtn) {
          submitBtn.disabled = false;
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
