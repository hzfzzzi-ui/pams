import { accountLabel } from '../../api.js?v=20261031-gray-v1';
import { escapeHtml, formatCurrency, formatMoneyAxis as axisMoney, formatMoneyFixed2 as money, formatPercent, formatSignedMoneyFixed2 as signedMoney, toFiniteNumber as num } from '../../utils.js?v=20261031-gray-v1';
import { RECENT_SORT_OPTIONS } from './state.js?v=20261031-ledger-modules-v1';
import { LEDGER_EDIT_TYPES, LEDGER_EDIT_DEFAULTS } from './legacy_model.js?v=20261031-ledger-modules-v1';

export function createLedgerTemplates({ state, model }) {
  const { isEditableLedgerRow, ledgerEditType, ledgerEditTypeLabel, ledgerEditActionLabel, ledgerEditSubmitLabel, rowTitle, originalCurrencyText, transferEndpointName, transferRoute, ledgerCategoryOptions, ledgerSubcategoryOptions, accountDisplayName, subAccountDisplayName, accountBalanceSummary, accountInvestmentBalanceEntries, investmentAccountStatsEntries, normalizeAccountRow, hasAccountActivity, accountFlowStatsEntries, formatSyncDate, accountFlowCountText } = model;

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
  const account = accountLabel(row.account_code) || row.account_code || '-';
  const cashflowType = ledgerEditType(row);
  const defaultEdit = LEDGER_EDIT_DEFAULTS[cashflowType] || {};
  const rawCategory = String(row.category || '').trim();
  const rawSubcategory = String(row.subcategory || '').trim();
  const category = rawCategory && rawCategory !== '未分类' ? rawCategory : (defaultEdit.category || '');
  const subcategory = rawSubcategory || defaultEdit.subcategory || '';
  return `
    <form class="ledgerConfirmSheet ledgerEditSheet" data-ledger-edit-form="${escapeHtml(String(row.id || ''))}">
      <div class="ledgerConfirmLead">${row.needs_review ? '选择正确口径后保存，这条流水会从待确认中移除。' : '修改会写入底层现金流事实表，并刷新收支和账户统计。'}</div>
      <div class="ledgerConfirmTitle">${escapeHtml(rowTitle(row))}</div>
      <div class="ledgerConfirmAmount ${amount >= 0 ? 'up' : 'down'}">${signedMoney(amount)}</div>
      <div class="ledgerConfirmMeta">
        <div>
          <span>日期 / 账户</span>
          <strong>${escapeHtml(row.transaction_date || '--')} / ${escapeHtml(account)}</strong>
        </div>
        <div>
          <span>当前口径</span>
          <strong>${escapeHtml(ledgerEditTypeLabel(cashflowType))} / ${escapeHtml(row.coarse_category || '-')}</strong>
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
          <select class="formInput" data-edit-field="cashflow_type">
            ${LEDGER_EDIT_TYPES.map(([value, label]) => `
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
            ${optionHtml(ledgerSubcategoryOptions(category, subcategory), subcategory)}
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

function accountFlowStatsHtml(row) {
  const entries = accountFlowStatsEntries(row);
  if (!entries.length) {
    return '<span>暂无流水</span>';
  }
  return entries.map(([label, value]) => {
    const formatted = typeof value === 'string' ? value : money(value);
    return `<span>${escapeHtml(label)} ${escapeHtml(formatted)}</span>`;
  }).join('');
}

function syncBadgeHtml(row) {
  return `<span class="accountSyncBadge">同步 ${escapeHtml(formatSyncDate(row.latest_sync_at))}</span>`;
}

function accountPendingBadgeHtml(row) {
  const pending = Number(row?.pending_count || 0);
  if (!pending) return '';
  return `<span class="accountPendingBadge">${pending} 笔待确认</span>`;
}

function renderAccountFlowSide(row, balanceRows = null) {
  const rowsForBalance = Array.isArray(balanceRows) && balanceRows.length ? balanceRows : [row];
  const balance = accountBalanceSummary(rowsForBalance);
  return `
    <span class="accountFlowSide">
      <b class="accountBalanceValue ${balance.tone}">${escapeHtml(balance.value)}</b>
      <small>${escapeHtml(balance.meta)}</small>
    </span>
  `;
}

function renderAccountFlowFooter(row, childCountText = '', { showStats = true } = {}) {
  const net = num(row.net);
  const deltaTone = net > 0 ? 'up' : net < 0 ? 'down' : '';
  const stats = showStats ? accountFlowStatsHtml(row) : '';
  return `
    <span class="accountFlowFooter ${stats ? '' : 'is-compact'}">
      ${stats ? `<span class="accountFlowStats">${stats}</span>` : ''}
      <span class="accountFlowFooterMeta">
        <span class="accountFlowNetBadge ${deltaTone}">年初净流入 ${escapeHtml(signedMoney(net))}</span>
        <span>${escapeHtml(accountFlowCountText(row, childCountText))}</span>
      </span>
    </span>
  `;
}

function investmentAccountChipHtml(entry) {
  return `
    <span class="accountInvestmentChip ${entry.tone || ''}">
      <span>${escapeHtml(entry.label)}</span>
      <strong>${escapeHtml(entry.value)}</strong>
    </span>
  `;
}

function renderInvestmentAccountFooter(row, childCountText = '') {
  const normalized = normalizeAccountRow(row);
  const flowEntries = investmentAccountStatsEntries(normalized);
  const balanceEntries = accountInvestmentBalanceEntries(normalized.account_code);
  const entries = [...flowEntries, ...balanceEntries];
  const content = entries.length
    ? entries.map(investmentAccountChipHtml).join('')
    : '<span class="accountInvestmentChip"><span>资金流</span><strong>暂无入金记录</strong></span>';
  return `
    <span class="accountFlowFooter accountInvestmentFooter">
      <span class="accountInvestmentStats">${content}</span>
      <span class="accountFlowFooterMeta">
        <span>${escapeHtml(accountFlowCountText(normalized, childCountText))}</span>
      </span>
    </span>
  `;
}

function renderAccountFlowRow(row, { label, detail, classes = '', filterable = true } = {}) {
  const normalized = normalizeAccountRow(row);
  const active = hasAccountActivity(row);
  const tagName = filterable && active ? 'button' : 'div';
  const typeAttr = tagName === 'button' ? ' type="button"' : '';
  const filterAttr = tagName === 'button' ? ` data-account-flow-filter="${escapeHtml(normalized.account_code || '')}"` : '';
  const rowClasses = [
    classes,
    normalized.account_kind === 'investment' ? 'accountInvestmentRow' : '',
    active ? '' : 'is-empty',
  ].filter(Boolean).join(' ');
  return `
    <${tagName}${typeAttr} class="accountFlowRow ${rowClasses}"${filterAttr}>
      <span class="accountFlowHeader">
        <span class="accountFlowMain">
          <strong>${escapeHtml(label || accountDisplayName(normalized.account_code))}</strong>
          <small class="accountFlowMetaLine"><span>${escapeHtml(detail || normalized.account_code || '')}</span>${syncBadgeHtml(normalized)}${accountPendingBadgeHtml(normalized)}</small>
        </span>
        ${renderAccountFlowSide(normalized)}
      </span>
      ${normalized.account_kind === 'investment' ? renderInvestmentAccountFooter(normalized) : renderAccountFlowFooter(normalized)}
    </${tagName}>
  `;
}

function renderAccountGroup(group) {
  const summary = group.summary;
  const childRows = group.children || [];
  const shouldShowChildren = group.forceChildren || childRows.length > 1;
  if (group.standalone && !shouldShowChildren) {
    const row = childRows[0] || summary;
    return renderAccountFlowRow(row, {
      label: group.label,
      detail: row.account_code,
      classes: 'accountStandaloneRow',
    });
  }

  const collapsed = shouldShowChildren && !state.expandedAccountGroups.has(group.code);
  const activeChildren = childRows.filter(hasAccountActivity).length;
  const childCountText = `${activeChildren}/${childRows.length} 子 · ${summary.row_count || 0} 笔${summary.pending_count ? ` · ${summary.pending_count} 待确认` : ''}`;
  return `
    <div class="accountGroupBlock" data-account-group="${escapeHtml(group.code)}">
      <button type="button" class="accountFlowRow accountGroupRow" data-account-group-toggle="${escapeHtml(group.code)}" aria-expanded="${collapsed ? 'false' : 'true'}">
        <span class="accountFlowHeader">
          <span class="accountFlowMain">
            <strong>${escapeHtml(group.label)}</strong>
            <small class="accountFlowMetaLine">${syncBadgeHtml(summary)}${accountPendingBadgeHtml(summary)}<span class="accountGroupToggle">${collapsed ? '展开' : '收起'}</span></small>
          </span>
          ${renderAccountFlowSide(summary, childRows)}
        </span>
        ${renderAccountFlowFooter(summary, childCountText, { showStats: false })}
      </button>
      <div class="accountSubList" ${collapsed ? 'hidden' : ''}>
        ${childRows.map((row) => renderAccountFlowRow(row, {
          label: subAccountDisplayName(row.account_code),
          detail: row.account_code,
          classes: 'accountSubRow',
        })).join('')}
      </div>
    </div>
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

  return { recentSearchSheetHtml, transferRouteHtml, rowDetailHtml, optionHtml, ledgerEditSheetHtml, accountFlowStatsHtml, syncBadgeHtml, accountPendingBadgeHtml, renderAccountFlowSide, renderAccountFlowFooter, investmentAccountChipHtml, renderInvestmentAccountFooter, renderAccountFlowRow, renderAccountGroup, recentSortSheetHtml };
}
