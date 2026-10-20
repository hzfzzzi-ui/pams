import { accountLabel, jget, jpost } from './api.js?v=20260606-refresh-v3';
import { closeBottomSheet, openBottomSheet, setActiveNav } from './ui.js?v=20260620-design-system-v2';
import {
  byId as $,
  clearError,
  escapeHtml,
  formatMoneyAxis as axisMoney,
  formatMoneyFixed2 as money,
  formatPercent,
  formatSignedMoneyFixed2 as signedMoney,
  setText,
  showError,
  toFiniteNumber as num,
} from './utils.js?v=20260607-shared-format-v1';

const state = {
  data: null,
  accounts: null,
  trendMode: 'monthly',
  activePanel: 'overview',
};

const LEDGER_PANEL_HASH = {
  overview: '#cashflow-workbench',
  recent: '#recent-flow-section',
  income: '#income-section',
  account: '#accountIssueStrip',
  transfer: '#transfers',
  review: '#review-section',
};

const LEDGER_HASH_PANEL = Object.fromEntries(
  Object.entries(LEDGER_PANEL_HASH).map(([panel, hash]) => [hash, panel]),
);

function flowLabel(flowType) {
  const labels = {
    income: '收入',
    expense: '消费',
    transfer: '资金流转',
    investment_return: '投资收益',
  };
  return labels[flowType] || flowType || '未知';
}

function flowClass(flowType) {
  if (flowType === 'income') return 'income';
  if (flowType === 'expense') return 'expense';
  if (flowType === 'transfer') return 'transfer';
  return 'other';
}

function rowTitle(row) {
  return [row.summary, row.description, row.counterparty].filter(Boolean).join(' / ') || '未命名流水';
}

function rowDetailHtml(row) {
  return `
    <div class="rowDetailGrid">
      <div><strong>账户</strong><span>${escapeHtml(accountLabel(row.account_code) || row.account_code || '-')}</span></div>
      <div><strong>对方</strong><span>${escapeHtml(row.counterparty || '不适用')}</span></div>
      <div><strong>原始类型</strong><span>${escapeHtml(row.flow_type || '-')}</span></div>
      <div><strong>粗分类</strong><span>${escapeHtml(row.coarse_category || '-')}</span></div>
      <div><strong>摘要</strong><span>${escapeHtml(row.summary || '-')}</span></div>
      <div><strong>说明</strong><span>${escapeHtml(row.description || '不适用')}</span></div>
      <div><strong>状态</strong><span>${row.needs_review ? '待确认' : '已归类'}</span></div>
    </div>
  `;
}

function reviewConfirmSheetHtml(row) {
  const amount = num(row.amount_cny ?? row.amount);
  const account = accountLabel(row.account_code) || row.account_code || '-';
  return `
    <div class="ledgerConfirmSheet">
      <div class="ledgerConfirmLead">确认前先核对这笔待确认支出的摘要、账户和对方信息。确认后会从待确认流水中移除。</div>
      <div class="ledgerConfirmTitle">${escapeHtml(rowTitle(row))}</div>
      <div class="ledgerConfirmAmount down">${signedMoney(amount)}</div>
      <div class="ledgerConfirmMeta">
        <div>
          <span>日期 / 账户</span>
          <strong>${escapeHtml(row.transaction_date || '--')} / ${escapeHtml(account)}</strong>
        </div>
        <div>
          <span>待确认类型</span>
          <strong>${escapeHtml(row.coarse_category || '待确认支出')}</strong>
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
      <div class="ledgerConfirmActions">
        <button type="button" class="ledgerActionBtn ghost" data-sheet-close>取消</button>
        <button type="button" class="ledgerActionBtn confirm" data-sheet-confirm="${escapeHtml(String(row.id || ''))}">确认这笔消费</button>
      </div>
    </div>
  `;
}

function renderKpis(data) {
  const month = data.periods?.month || {};
  const investmentTransfer = investmentTransferAmount(data.transfer_summary || []);
  const totalTransfer = num(month.internal_transfer || 0);
  const internalTransfer = Math.max(totalTransfer - investmentTransfer, 0);
  setText('kpiMonthIncome', money(month.income || 0));
  setText('kpiMonthExpense', money(month.expense || 0));
  setText('kpiDisposableNet', signedMoney(month.net || 0));
  setText('kpiInvestmentTransfer', money(investmentTransfer || 0));
  setText('kpiTransfer', money(internalTransfer || 0));
  setText('kpiPending', String(month.pending_count || 0));
  const netEl = $('kpiDisposableNet');
  if (netEl) {
    netEl.classList.toggle('up', num(month.net) >= 0);
    netEl.classList.toggle('down', num(month.net) < 0);
  }
  const pendingEl = $('kpiPending');
  if (pendingEl) pendingEl.classList.toggle('down', num(month.pending_count) > 0);
}

function renderLedgerMobileHero(data) {
  const el = $('ledgerMobileHero');
  if (!el) return;
  const month = data.periods?.month || {};
  const investmentTransfer = investmentTransferAmount(data.transfer_summary || []);
  const totalTransfer = num(month.internal_transfer || 0);
  const internalTransfer = Math.max(totalTransfer - investmentTransfer, 0);
  const net = num(month.net || 0);
  const pending = num(month.pending_count || 0);
  el.innerHTML = `
    <div class="ledgerMobileLabel">本月净流入</div>
    <div class="ledgerMobileValue ${net >= 0 ? 'up' : 'down'}">${signedMoney(net)}</div>
    <div class="ledgerMobileStats">
      <div class="ledgerMobileStat">
        <span>待确认</span>
        <strong class="${pending ? 'down' : ''}">${pending}</strong>
      </div>
      <div class="ledgerMobileStat">
        <span>收入</span>
        <strong>${money(month.income || 0)}</strong>
      </div>
      <div class="ledgerMobileStat">
        <span>转账</span>
        <strong>${money(internalTransfer || 0)}</strong>
      </div>
    </div>
  `;
}

function accountIssueTone(account) {
  if (account.status === 'ready_for_review' || account.review_status === 'stale') return 'warn';
  if (account.status === 'in_transit') return 'data';
  if (account.status === 'missing_data') return 'pending';
  if (num(account.primary_balance?.amount) < 0) return 'bad';
  return 'ok';
}

function renderAccountIssueStrip(accountsResp) {
  const el = $('ledgerAccountIssueStrip');
  if (!el) return;
  const summary = accountsResp?.summary || {};
  const accounts = accountsResp?.accounts || [];
  const issueRows = accounts
    .filter((account) => (
      account.status === 'ready_for_review' ||
      account.status === 'in_transit' ||
      account.status === 'missing_data' ||
      account.review_status === 'stale' ||
      num(account.primary_balance?.amount) < 0
    ))
    .slice(0, 4);
  const chips = [
    ['差异', summary.ready_for_review || 0, 'warn'],
    ['过期', summary.stale_review || 0, 'warn'],
    ['在途', summary.in_transit || 0, 'data'],
    ['缺数据', summary.missing_data || 0, 'pending'],
  ];
  el.innerHTML = `
    <div class="accountIssueChips">
      ${chips.map(([label, value, tone]) => `<a class="miniBadge ${value ? tone : 'ok'}" href="accounts.html">${escapeHtml(label)} ${escapeHtml(value)}</a>`).join('')}
    </div>
    <div class="accountIssueRows">
      ${issueRows.length ? issueRows.map((account) => {
        const amount = account.primary_balance?.amount;
        const currency = account.primary_balance?.currency || account.base_currency || 'CNY';
        const tone = accountIssueTone(account);
        return `<a class="accountIssueRow" href="accounts.html">
          <span>
            <strong>${escapeHtml(account.display_name || account.subaccount_label || account.account_code)}</strong>
            <small>${escapeHtml(account.status_label || account.review_hint || '待核对')}</small>
          </span>
          <em class="mono ${tone === 'bad' ? 'down' : ''}">${money(amount || 0, currency)}</em>
        </a>`;
      }).join('') : '<div class="muted">暂无账户异常</div>'}
    </div>
  `;
}

function investmentTransferAmount(rows) {
  return (rows || [])
    .filter((row) => String(row.match_type || '').includes('investment'))
    .reduce((sum, row) => sum + num(row.amount), 0);
}

function renderCategoryBars(id, rows, emptyText) {
  const el = $(id);
  if (!el) return;
  if (!rows?.length) {
    el.innerHTML = `<div class="muted">${escapeHtml(emptyText)}</div>`;
    return;
  }
  el.innerHTML = rows.map((row) => `
    <div class="categoryBarRow">
      <div class="categoryBarHead">
        <span>${escapeHtml(row.category)}</span>
        <span class="mono">${money(row.amount)} / ${formatPercent(row.share, 1)}</span>
      </div>
      <div class="categoryTrack"><div class="categoryFill" style="width:${Math.max(num(row.share) * 100, 2)}%"></div></div>
      <div class="muted">${row.count || 0} 笔</div>
    </div>
  `).join('');
}

function renderCashflowBridge(data) {
  const el = $('cashflowBridge');
  if (!el) return;
  const month = data.periods?.month || {};
  const totalTransfer = num(month.internal_transfer || 0);
  const investTransfer = investmentTransferAmount(data.transfer_summary || []);
  const otherTransfer = Math.max(totalTransfer - investTransfer, 0);
  const rows = [
    ['收入', money(month.income || 0), 'good'],
    ['消费', signedMoney(-Math.abs(num(month.expense || 0))), 'bad'],
    ['可支配净流入', signedMoney(month.net || 0), num(month.net || 0) >= 0 ? 'good' : 'bad'],
    ['投资转账', signedMoney(-Math.abs(investTransfer)), 'data'],
    ['内部转账', signedMoney(otherTransfer), 'data'],
    ['待确认流水', String(month.pending_count || 0), num(month.pending_count || 0) ? 'pending' : 'data'],
  ];
  el.innerHTML = rows.map(([label, value, tone]) => `
    <div class="cashflowBridgeItem ${tone}">
      <div class="cashflowBridgeLabel">${escapeHtml(label)}</div>
      <div class="cashflowBridgeValue mono">${escapeHtml(value)}</div>
    </div>
  `).join('');
}

function renderReviewEntry(data) {
  const el = $('reviewEntry');
  if (!el) return;
  const month = data.periods?.month || {};
  const pending = Number(month.pending_count || 0);
  const rows = (data.review_rows || []).slice(0, 3);
  el.innerHTML = `
    <div style="display:flex;justify-content:space-between;gap:10px;align-items:center">
      <a href="#review-section">待确认流水 ${pending}</a>
      <span class="miniBadge ${pending ? 'warn' : 'ok'}">${pending ? '高优先级' : '已清空'}</span>
    </div>
    ${rows.length ? `<div style="margin-top:8px">${rows.map((row) => `
      <div class="analysisRow">
        <span class="analysisLabel">${escapeHtml(row.transaction_date || '')} ${escapeHtml(row.coarse_category || '')}</span>
        <span class="analysisValue down">${signedMoney(row.amount_cny || row.amount || 0)} / 详情</span>
      </div>
    `).join('')}</div>` : ''}
  `;
}

function renderTransferSummary(rows) {
  const el = $('transferSummary');
  if (!el) return;
  if (!rows?.length) {
    el.innerHTML = '<div class="muted">本期未识别到资金流转匹配。</div>';
    return;
  }
  el.innerHTML = rows.slice(0, 10).map((row) => `
    <div class="analysisRow">
      <div>
        <div class="analysisLabel">${escapeHtml(row.relation_code)}</div>
        <div class="analysisSub">${escapeHtml(row.match_type)} / ${row.count || 0} 笔</div>
      </div>
      <div class="analysisValue">${money(row.amount)}</div>
    </div>
  `).join('');
}

function renderRules(rows) {
  const el = $('classificationRules');
  if (!el) return;
  el.innerHTML = (rows || []).map((rule) => `
    <div class="ruleItem">
      <div class="ruleName">${escapeHtml(rule.name)}</div>
      <div class="muted">${escapeHtml(rule.description)}</div>
    </div>
  `).join('');
}

function renderTrendChart(data) {
  const rows = state.trendMode === 'weekly' ? data.weekly_trend || [] : data.monthly_trend || [];
  const ctx = $('cashflowTrendChart');
  if (!ctx || !window.Chart) return;
  if (window.__cashflowTrendChart) window.__cashflowTrendChart.destroy();
  window.__cashflowTrendChart = new Chart(ctx, {
    data: {
      labels: rows.map((r) => r.period),
      datasets: [
        {
          type: 'bar',
          label: '收入',
          data: rows.map((r) => num(r.income)),
          backgroundColor: 'rgba(63,185,80,.42)',
          borderColor: 'rgba(63,185,80,.8)',
          borderWidth: 1,
        },
        {
          type: 'bar',
          label: '消费',
          data: rows.map((r) => num(r.expense)),
          backgroundColor: 'rgba(248,81,73,.38)',
          borderColor: 'rgba(248,81,73,.82)',
          borderWidth: 1,
        },
        {
          type: 'line',
          label: '净流入',
          data: rows.map((r) => num(r.net)),
          borderColor: '#58a6ff',
          backgroundColor: 'rgba(88,166,255,.12)',
          tension: 0.25,
          pointRadius: 2,
          yAxisID: 'net',
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { labels: { color: '#8b949e' } },
        tooltip: {
          callbacks: {
            label: (ctxItem) => `${ctxItem.dataset.label}: ${money(ctxItem.parsed.y)}`,
          },
        },
      },
      scales: {
        x: { ticks: { color: '#8b949e' }, grid: { color: 'rgba(139,148,158,.12)' } },
        y: { ticks: { color: '#8b949e', callback: axisMoney }, grid: { color: 'rgba(139,148,158,.14)' } },
        net: { position: 'right', ticks: { color: '#8b949e', callback: axisMoney }, grid: { drawOnChartArea: false } },
      },
    },
  });
}

function bindReviewConfirmButton(rowId) {
  const confirmBtn = document.querySelector('#appBottomSheet [data-sheet-confirm]');
  const closeBtn = document.querySelector('#appBottomSheet [data-sheet-close]');
  if (closeBtn) {
    closeBtn.addEventListener('click', (event) => {
      event.preventDefault();
      closeBottomSheet();
    }, { once: true });
  }
  if (!confirmBtn) return;
  confirmBtn.addEventListener('click', async () => {
    confirmBtn.disabled = true;
    confirmBtn.textContent = '确认中...';
    try {
      await jpost(`/api/ledger/review/${rowId}/confirm`, {
        confirmed_category: '其他消费',
        confirmed_subcategory: '手动确认',
      });
      closeBottomSheet();
      await loadWorkbench();
    } catch (err) {
      showError(err);
      confirmBtn.disabled = false;
      confirmBtn.textContent = '确认这笔消费';
    }
  }, { once: true });
}

function renderRows(id, rows, limit = 80) {
  const el = $(id);
  if (!el) return;
  el.classList.toggle('reviewList', id === 'reviewRows');
  if (!rows?.length) {
    el.innerHTML = '<div class="ledgerEmpty">暂无流水</div>';
    return;
  }
  const displayRows = rows.slice(0, limit);
  el.innerHTML = displayRows.map((row, index) => {
    const rawId = row.id || row.transaction_id || `${row.transaction_date || 'row'}-${index}`;
    const detailId = `${id}-detail-${String(rawId).replace(/[^a-zA-Z0-9_-]/g, '_')}`;
    const amount = num(row.amount_cny ?? row.amount);
    const amountClass = amount >= 0 ? 'up' : 'down';
    const title = rowTitle(row);
    const account = accountLabel(row.account_code) || row.account_code || '';
    const category = row.coarse_category || flowLabel(row.flow_type);
    const counterparty = row.counterparty ? ` / ${row.counterparty}` : '';
    const isReviewRow = id === 'reviewRows' && row.needs_review;
    const actionButtons = isReviewRow
      ? `
          <div class="ledgerRowActions">
            <button class="ledgerActionBtn confirm" type="button" data-ledger-confirm="${escapeHtml(String(row.id || ''))}">确认</button>
          </div>
        `
      : '';
    return `
      <div class="ledgerListRow" role="button" tabindex="0" data-detail-target="${escapeHtml(detailId)}" title="点击查看账户、对方和原始摘要">
        <span class="ledgerListMain">
          <strong title="${escapeHtml(title)}">${escapeHtml(title)}</strong>
          <small>${escapeHtml(row.transaction_date || '')} / ${escapeHtml(account)} / ${escapeHtml(category)}${escapeHtml(counterparty)}</small>
        </span>
        <span class="ledgerListSide">
          <b class="${amountClass}">${signedMoney(amount)}</b>
          <small class="ledgerListMeta">
            <span class="flowTag ${flowClass(row.flow_type)}">${escapeHtml(flowLabel(row.flow_type))}</span>
            ${row.needs_review ? '<span class="ledgerReviewHint">待确认支出</span>' : ''}
          </small>
          ${actionButtons}
        </span>
      </div>
      <div id="${escapeHtml(detailId)}" class="ledgerInlineDetail" hidden>
        ${rowDetailHtml(row)}
      </div>
    `;
  }).join('');

  el.querySelectorAll('.ledgerListRow').forEach((rowEl) => {
    const openDetail = () => {
      const detail = document.getElementById(rowEl.dataset.detailTarget);
      if (!detail) return;
      if (window.matchMedia('(max-width: 760px)').matches) {
        const title = rowEl.querySelector('.ledgerListMain strong')?.innerText || '流水详情';
        openBottomSheet(title, detail.innerHTML);
        return;
      }
      detail.hidden = !detail.hidden;
      rowEl.classList.toggle('open', !detail.hidden);
    };
    rowEl.addEventListener('click', openDetail);
    rowEl.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        openDetail();
      }
    });
  });

  el.querySelectorAll('[data-ledger-confirm]').forEach((btn) => {
    btn.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      const rowId = Number(btn.dataset.ledgerConfirm || 0);
      if (!rowId) return;
      const row = displayRows.find((item) => Number(item.id || 0) === rowId);
      if (!row) return;
      openBottomSheet('确认消费', reviewConfirmSheetHtml(row));
      bindReviewConfirmButton(rowId);
    });
  });
}

function filteredRecentRows() {
  const flow = $('recentFlowFilter')?.value || '';
  const search = ($('recentSearch')?.value || '').trim().toLowerCase();
  return (state.data?.recent_rows || []).filter((row) => {
    if (flow === 'review' && !row.needs_review) return false;
    if (flow && flow !== 'review' && row.flow_type !== flow) return false;
    if (search && !rowTitle(row).toLowerCase().includes(search) && !String(row.coarse_category || '').toLowerCase().includes(search)) {
      return false;
    }
    return true;
  });
}

function renderAll(data) {
  renderLedgerMobileHero(data);
  renderKpis(data);
  renderAccountIssueStrip(state.accounts);
  renderCategoryBars('expenseCategories', data.expense_categories || [], '本月暂无消费流水。');
  renderCategoryBars('incomeCategories', data.income_categories || [], '本月暂无收入流水。');
  renderTransferSummary(data.transfer_summary || []);
  renderRules(data.classification_rules || []);
  renderTrendChart(data);
  renderCashflowBridge(data);
  renderReviewEntry(data);
  renderRows('reviewRows', data.review_rows || [], 60);
  renderRows('recentRows', filteredRecentRows(), 120);
}

async function loadWorkbench() {
  clearError();
  const month = $('monthPicker')?.value || '';
  const params = new URLSearchParams({ baseCurrency: 'CNY', recentDays: '30' });
  if (month) params.set('month', month);
  const [data, accounts] = await Promise.all([
    jget(`/api/ledger/workbench?${params.toString()}`),
    jget('/api/accounts/overview').catch(() => ({ summary: {}, accounts: [] })),
  ]);
  state.data = data;
  state.accounts = accounts;
  if ($('monthPicker') && !$('monthPicker').value) $('monthPicker').value = data.month;
  renderAll(data);
}

function bindEvents() {
  bindLedgerAnchors();
  $('btnReloadLedger')?.addEventListener('click', loadWorkbench);
  $('monthPicker')?.addEventListener('change', loadWorkbench);
  $('trendMonthly')?.addEventListener('click', () => {
    state.trendMode = 'monthly';
    $('trendMonthly')?.classList.add('active');
    $('trendWeekly')?.classList.remove('active');
    if (state.data) renderTrendChart(state.data);
  });
  $('trendWeekly')?.addEventListener('click', () => {
    state.trendMode = 'weekly';
    $('trendWeekly')?.classList.add('active');
    $('trendMonthly')?.classList.remove('active');
    if (state.data) renderTrendChart(state.data);
  });
  $('recentFlowFilter')?.addEventListener('change', () => renderRows('recentRows', filteredRecentRows(), 120));
  $('recentSearch')?.addEventListener('input', () => renderRows('recentRows', filteredRecentRows(), 120));
}

function setLedgerPanel(panel = 'overview') {
  state.activePanel = LEDGER_PANEL_HASH[panel] ? panel : 'overview';
  document.body.dataset.ledgerTab = state.activePanel;
  document.querySelectorAll('[data-ledger-panel]').forEach((group) => {
    group.hidden = group.dataset.ledgerPanel !== state.activePanel;
  });
  document.querySelectorAll('.ledgerAnchorNav a').forEach((link) => {
    link.classList.toggle('is-active', link.dataset.ledgerTab === state.activePanel);
  });
}

function bindLedgerAnchors() {
  document.querySelectorAll('.ledgerAnchorNav a').forEach((link) => {
    link.addEventListener('click', (event) => {
      event.preventDefault();
      const panel = link.dataset.ledgerTab || 'overview';
      const nextHash = LEDGER_PANEL_HASH[panel] || LEDGER_PANEL_HASH.overview;
      if (window.location.hash !== nextHash) {
        history.replaceState(null, '', `${window.location.pathname}${window.location.search}${nextHash}`);
      }
      setLedgerPanel(panel);
      window.scrollTo({ top: 0, behavior: 'smooth' });
    });
  });
  window.addEventListener('hashchange', () => {
    setLedgerPanel(LEDGER_HASH_PANEL[window.location.hash] || 'overview');
  });
  setLedgerPanel(LEDGER_HASH_PANEL[window.location.hash] || 'overview');
}

async function init() {
  setActiveNav();
  bindEvents();
  await loadWorkbench();
}

init().catch(showError);
