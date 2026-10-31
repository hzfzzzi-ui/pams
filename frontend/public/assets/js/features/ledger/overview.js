import { accountLabel } from '../../api.js?v=20261031-modules-v1';
import { escapeHtml, formatCurrency, formatMoneyAxis as axisMoney, formatMoneyFixed2 as money, formatPercent, formatSignedMoneyFixed2 as signedMoney, toFiniteNumber as num } from '../../utils.js?v=20261031-modules-v1';
import { CHART_THEME, SERIES_COLORS } from '../../shared/chart_theme.js?v=20261031-modules-v1';
const CATEGORY_PIE_COLORS = { income: SERIES_COLORS, expense: SERIES_COLORS };

export function createLedgerOverview({ root, host, state, model, dom, ChartClass = host.Chart }) {
  const { $, setText } = dom;
  const { overviewPeriodKey, overviewPeriodLabel, currentOverviewPeriod, overviewDisposableNet, overviewInvestmentTransfer, overviewOtherTransfer, overviewCashNet, currentOverviewCategories, overviewFlowKey, overviewFlowLabel, overviewPieRows } = model;
  let trendChart = null;
  let pieChart = null;

function renderOverviewPeriodButtons() {
  root.querySelectorAll('[data-overview-period]').forEach((btn) => {
    btn.classList.toggle('active', (btn.dataset.overviewPeriod || 'ytd') === overviewPeriodKey());
  });
}

function renderOverviewFlowButtons() {
  root.querySelectorAll('[data-overview-flow]').forEach((btn) => {
    btn.classList.toggle('active', (btn.dataset.overviewFlow || 'income') === overviewFlowKey());
  });
}

function renderKpis(data) {
  const period = currentOverviewPeriod(data);
  const investmentTransfer = overviewInvestmentTransfer(data, period);
  const internalTransfer = overviewOtherTransfer(data, period);
  const disposableNet = overviewDisposableNet(period);
  setText('kpiMonthIncome', money(period.income || 0));
  setText('kpiMonthExpense', money(period.expense || 0));
  setText('kpiDisposableNet', signedMoney(disposableNet));
  setText('kpiInvestmentTransfer', money(investmentTransfer || 0));
  setText('kpiTransfer', money(internalTransfer || 0));
  setText('kpiPending', String(period.pending_count || 0));
  const netEl = $('kpiDisposableNet');
  if (netEl) {
    netEl.classList.toggle('up', disposableNet >= 0);
    netEl.classList.toggle('down', disposableNet < 0);
  }
  const pendingEl = $('kpiPending');
  if (pendingEl) pendingEl.classList.toggle('down', num(period.pending_count) > 0);
}

function renderLedgerMobileHero(data) {
  const el = $('ledgerMobileHero');
  if (!el) return;
  const period = currentOverviewPeriod(data);
  const disposableNet = overviewDisposableNet(period);
  const pending = num(period.pending_count || 0);
  el.innerHTML = `
    <div class="ledgerMobileLabel">${overviewPeriodLabel()}可支配结余</div>
    <div class="ledgerMobileValue ${disposableNet >= 0 ? 'up' : 'down'}">${signedMoney(disposableNet)}</div>
    <div class="ledgerMobileStats">
      <div class="ledgerMobileStat">
        <span>收入</span>
        <strong>${money(period.income || 0)}</strong>
      </div>
      <div class="ledgerMobileStat">
        <span>消费</span>
        <strong class="down">${signedMoney(-Math.abs(num(period.expense || 0)))}</strong>
      </div>
      <div class="ledgerMobileStat">
        <span>待确认</span>
        <strong class="${pending ? 'down' : ''}">${pending}</strong>
      </div>
    </div>
  `;
}

function renderCategoryBars(id, rows, emptyText, flowType = 'income') {
  const el = $(id);
  if (!el) return;
  if (!rows?.length) {
    el.innerHTML = `<div class="muted">${escapeHtml(emptyText)}</div>`;
    return;
  }
  el.innerHTML = `
    <div class="categoryBarList ${flowType === 'expense' ? 'expense' : 'income'}">
      ${rows.map((row, index) => `
        <div class="categoryBarRow">
          <div class="categoryBarRank">${index + 1}</div>
          <div class="categoryBarMain">
            <div class="categoryBarName">${escapeHtml(row.category)}</div>
            <div class="categoryTrack"><div class="categoryFill" style="width:${Math.max(num(row.share) * 100, 2)}%"></div></div>
            <div class="categoryBarCount">${row.count || 0} 笔</div>
          </div>
          <div class="categoryBarMeta">
            <b>${money(row.amount)}</b>
            <span>${formatPercent(row.share, 1)}</span>
          </div>
        </div>
      `).join('')}
    </div>
  `;
}

function renderOverviewCategoryPieFallback(canvas, chartRows, flowType) {
  const wrap = canvas?.parentElement;
  if (!wrap) return;

  wrap.querySelector('.categoryPieFallback')?.remove();
  const total = chartRows.reduce((sum, row) => sum + num(row.amount), 0);
  if (!total) return;

  const colors = CATEGORY_PIE_COLORS[flowType] || CATEGORY_PIE_COLORS.income;
  let cursor = 0;
  const slices = chartRows.map((row, index) => {
    const start = cursor;
    cursor += (num(row.amount) / total) * 100;
    return `${colors[index % colors.length]} ${start.toFixed(2)}% ${cursor.toFixed(2)}%`;
  }).join(', ');

  const fallback = (root.ownerDocument || root).createElement('div');
  fallback.className = 'categoryPieFallback';
  fallback.style.background = `conic-gradient(${slices})`;
  fallback.setAttribute(
    'aria-label',
    chartRows.map((row) => `${row.category} ${formatPercent(num(row.amount) / total, 1)}`).join('，'),
  );
  wrap.insertBefore(fallback, canvas);
  canvas.hidden = true;
}

function renderOverviewCategoryPie(rows, flowType) {
  const canvas = $('overviewCategoryPieChart');
  const empty = $('overviewCategoryPieEmpty');
  if (pieChart) {
    pieChart.destroy();
    if (host.__overviewCategoryPieChart === pieChart) delete host.__overviewCategoryPieChart;
    pieChart = null;
  }
  canvas?.parentElement?.querySelector('.categoryPieFallback')?.remove();
  if (!canvas) return;

  const chartRows = overviewPieRows(rows);
  const hasRows = chartRows.length > 0;
  canvas.hidden = !hasRows;
  if (empty) {
    empty.hidden = hasRows;
    empty.textContent = hasRows ? '' : `暂无${overviewFlowLabel(flowType)}分类`;
  }
  if (!hasRows) return;
  if (typeof ChartClass !== 'function') {
    renderOverviewCategoryPieFallback(canvas, chartRows, flowType);
    return;
  }
  canvas.hidden = false;

  const colors = CATEGORY_PIE_COLORS[flowType] || CATEGORY_PIE_COLORS.income;
  pieChart = new ChartClass(canvas, {
    type: 'pie',
    data: {
      labels: chartRows.map((row) => row.category),
      datasets: [{
        data: chartRows.map((row) => num(row.amount)),
        backgroundColor: chartRows.map((_, index) => colors[index % colors.length]),
        borderColor: CHART_THEME.background,
        borderWidth: 2,
        hoverOffset: 3,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (ctxItem) => {
              const value = num(ctxItem.parsed);
              const total = chartRows.reduce((sum, row) => sum + num(row.amount), 0);
              const share = total ? value / total : 0;
              return `${ctxItem.label}: ${money(value)} / ${formatPercent(share, 1)}`;
            },
          },
        },
      },
    },
  });
  host.__overviewCategoryPieChart = pieChart;
}

function renderOverviewCategoryInsight(data) {
  const flowType = overviewFlowKey();
  const rows = currentOverviewCategories(data, flowType);
  setText('categoryInsightTitle', `${overviewFlowLabel(flowType)}排行`);
  renderOverviewFlowButtons();
  renderCategoryBars(
    'overviewCategoryRank',
    rows,
    `${overviewPeriodLabel()}暂无${overviewFlowLabel(flowType)}流水。`,
    flowType,
  );
  renderOverviewCategoryPie(rows, flowType);
}

function renderCashflowBridge(data) {
  const el = $('cashflowBridge');
  if (!el) return;
  const period = currentOverviewPeriod(data);
  const investmentMovement = overviewInvestmentTransfer(data, period);
  const otherTransfer = overviewOtherTransfer(data, period);
  const internalMovement = num(period.internal_transfer ?? (investmentMovement + otherTransfer));
  const cashNet = overviewCashNet(data, period);
  const fundingItems = [
    ['内部资金流动', money(internalMovement), 'data'],
    ['投资账户流动', money(investmentMovement), 'data'],
    ['现金净变化', signedMoney(cashNet), cashNet >= 0 ? 'good' : 'bad'],
  ];
  el.innerHTML = `
    <div class="cashflowBridgeLine">
      <div class="cashflowBridgeHead">
        <span>资金调拨</span>
        <strong>NAV 内部迁移，不计入消费</strong>
      </div>
      <div class="cashflowTransferGrid">
        ${fundingItems.map(([label, value, tone]) => `
          <div class="cashflowTransferStat ${tone}">
            <span>${escapeHtml(label)}</span>
            <strong>${escapeHtml(value)}</strong>
          </div>
        `).join('')}
      </div>
    </div>
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
  if (!ctx || typeof ChartClass !== 'function') return;
  if (trendChart) trendChart.destroy();
  trendChart = new ChartClass(ctx, {
    data: {
      labels: rows.map((r) => r.period),
      datasets: [
        {
          type: 'bar',
          label: '收入',
          data: rows.map((r) => num(r.income)),
          backgroundColor: SERIES_COLORS[1],
          borderColor: SERIES_COLORS[1],
          borderWidth: 1,
        },
        {
          type: 'bar',
          label: '消费',
          data: rows.map((r) => num(r.expense)),
          backgroundColor: SERIES_COLORS[3],
          borderColor: SERIES_COLORS[3],
          borderWidth: 1,
        },
        {
          type: 'line',
          label: '净流入',
          borderDash: [5, 3],
          pointStyle: 'rectRot',
          data: rows.map((r) => num(r.net)),
          borderColor: CHART_THEME.line,
          backgroundColor: CHART_THEME.fill,
          tension: 0.25,
          pointRadius: 2,
          yAxisID: 'net',
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      layout: { padding: { left: 8, right: 18, top: 2, bottom: 4 } },
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { labels: { color: CHART_THEME.text, boxWidth: 28, boxHeight: 10, padding: 12 } },
        tooltip: {
          callbacks: {
            label: (ctxItem) => `${ctxItem.dataset.label}: ${money(ctxItem.parsed.y)}`,
          },
        },
      },
      scales: {
        x: { ticks: { color: CHART_THEME.text }, grid: { color: CHART_THEME.grid } },
        y: { ticks: { color: CHART_THEME.text, callback: axisMoney }, grid: { color: CHART_THEME.grid } },
        net: { position: 'right', ticks: { color: CHART_THEME.text, callback: axisMoney }, grid: { drawOnChartArea: false } },
      },
    },
  });
  host.__cashflowTrendChart = trendChart;
}

  return {
    renderOverviewPeriodButtons, renderOverviewFlowButtons, renderKpis, renderLedgerMobileHero, renderCategoryBars, renderOverviewCategoryPieFallback, renderOverviewCategoryPie, renderOverviewCategoryInsight, renderCashflowBridge, renderTransferSummary, renderRules, renderTrendChart,
    dispose() {
      trendChart?.destroy();
      pieChart?.destroy();
      if (host.__cashflowTrendChart === trendChart) delete host.__cashflowTrendChart;
      if (host.__overviewCategoryPieChart === pieChart) delete host.__overviewCategoryPieChart;
      $('overviewCategoryPieChart')?.parentElement?.querySelector('.categoryPieFallback')?.remove();
      trendChart = pieChart = null;
    },
  };
}
