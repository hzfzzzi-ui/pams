import { accountLabel } from '../../api.js';
import { escapeHtml, formatCurrency, formatMoneyAxis as axisMoney, formatMoneyFixed2 as money, formatPercent, formatSignedMoneyFixed2 as signedMoney, toFiniteNumber as num } from '../../utils.js';
import { CHART_THEME } from '../../shared/chart_theme.js';
// Restore the ledger's established semantic palette without changing shared charts or chrome.
const CATEGORY_PIE_COLORS = {
  income: ['#3fb950', '#58a6ff', '#d29922', '#a371f7', '#2ea043', '#79c0ff', '#8b949e'],
  expense: ['#f85149', '#d29922', '#ff7b72', '#a371f7', '#f0883e', '#db6d28', '#8b949e'],
};

export function createLedgerOverview({ root, host, state, model, dom, ChartClass = host.Chart }) {
  const { $, setText } = dom;
  const { overviewPeriodKey, overviewPeriodLabel, currentOverviewPeriod, currentOverviewCategories, overviewFlowKey, overviewFlowLabel, overviewPieRows } = model;
  let trendChart = null;
  let pieChart = null;

function renderOverviewPeriodButtons() {
  root.querySelectorAll('[data-overview-period]').forEach((btn) => {
    btn.setAttribute('aria-pressed', String(btn.dataset.overviewPeriod === overviewPeriodKey()));
  });
}

function renderOverviewFlowButtons() {
  root.querySelectorAll('[data-overview-flow]').forEach((btn) => {
    btn.setAttribute('aria-pressed', String(btn.dataset.overviewFlow === overviewFlowKey()));
  });
}

function renderLedgerMobileHero(data) {
  const el = $('ledgerMobileHero');
  if (!el) return;
  const period = currentOverviewPeriod(data);
  const amounts = period.confirmed_subtotal ?? period;
  const disposableNet = amounts.disposable_net ?? amounts.net;
  const cashNet = amounts.cash_net;
  const unclassified = num(period.unclassified_count);
  const subcategoryPending = num(period.subcategory_pending_count);
  const otherPending = Math.max(0, num(period.pending_count) - unclassified - subcategoryPending);
  const amountText = (value, formatter = money) => value == null ? '—' : formatter(value);
  const amountTone = value => value == null ? '' : value < 0 ? 'down' : 'up';
  el.innerHTML = `
    <div class="ledgerMobileLabel">${overviewPeriodLabel()}可支配结余${unclassified ? '（已确认小计）' : ''}</div>
    <div class="ledgerMobileValue ${amountTone(disposableNet)}">${amountText(disposableNet, signedMoney)}</div>
    <div class="ledgerMobileStats">
      <div class="ledgerMobileStat">
        <span>收入</span>
        <strong>${amountText(amounts.income)}</strong>
      </div>
      <div class="ledgerMobileStat">
        <span>消费</span>
        <strong class="${amounts.expense == null ? '' : 'down'}">${amountText(amounts.expense, value => signedMoney(-Math.abs(value)))}</strong>
      </div>
      <div class="ledgerMobileStat">
        <span>${unclassified ? '待分类' : '子类待补'}</span>
        <strong class="${unclassified ? 'down' : ''}">${unclassified || subcategoryPending}</strong>
      </div>
    </div>
    ${unclassified || otherPending ? `<p class="ledgerOverviewCoverage">${unclassified
      ? (disposableNet == null ? '暂无已确认且可计入的收支。' : '仅统计已确认且金额可用的收支。') : ''}${otherPending ? `另有 ${otherPending} 笔待核对。` : ''}</p>` : ''}
    <div class="ledgerMobileStats ledgerFundingStats">
      <div class="ledgerMobileStat">
        <span>投资入金</span>
        <strong>${amountText(period.investment_funding)}</strong>
      </div>
      <div class="ledgerMobileStat">
        <span>现金净变化</span>
        <strong class="${amountTone(cashNet)}">${amountText(cashNet, signedMoney)}</strong>
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
  const colors = CATEGORY_PIE_COLORS[flowType] || CATEGORY_PIE_COLORS.income;
  el.innerHTML = `
    <div class="categoryBarList ${flowType === 'expense' ? 'expense' : 'income'}">
      ${rows.map((row, index) => `
        <div class="categoryBarRow">
          <div class="categoryBarRank" style="--ledger-rank-color:${colors[Math.min(index, colors.length - 1)]}">${index + 1}</div>
          <div class="categoryBarMain">
            <div class="categoryBarName">${escapeHtml(row.category)}</div>
            <div class="categoryBarCount">${row.count || 0} 笔</div>
          </div>
          <div class="categoryBarMeta">
            <b>${row.amount == null ? '—' : money(row.amount)}</b>
            <span>${row.share == null ? '—' : formatPercent(row.share, 1)}</span>
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
    type: 'doughnut',
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
      cutout: '74%',
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
  const total = $('overviewCategoryTotal');
  if (total) {
    const period = currentOverviewPeriod(data);
    const amount = (period.confirmed_subtotal ?? period)[flowType];
    total.hidden = !rows.some(row => num(row.amount) > 0);
    setText('overviewCategoryTotalLabel', `${overviewFlowLabel(flowType)}合计`);
    setText('overviewCategoryTotalValue', amount == null ? '—' : money(amount));
  }
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
  const coverage = $('ledgerTrendCoverage');
  if (coverage) {
    coverage.hidden = !rows.some(row => row.unclassified_count);
    coverage.textContent = coverage.hidden ? '' : '有待分类流水的期间仅显示已确认小计。';
  }
  const amounts = rows.map(row => row.confirmed_subtotal ?? row);
  const ctx = $('cashflowTrendChart');
  if (!ctx || typeof ChartClass !== 'function') return;
  const tickFont = () => ({ size: host?.matchMedia?.('(max-width: 389px)').matches ? 10 : 12 });
  if (trendChart) trendChart.destroy();
  trendChart = new ChartClass(ctx, {
    data: {
      labels: rows.map((r) => r.period),
      datasets: [
        {
          type: 'bar',
          label: '收入',
          data: amounts.map(row => row.income ?? null),
          backgroundColor: 'rgba(63,185,80,.42)',
          borderColor: 'rgba(63,185,80,.8)',
          borderWidth: 1,
        },
        {
          type: 'bar',
          label: '消费',
          data: amounts.map(row => row.expense ?? null),
          backgroundColor: 'rgba(248,81,73,.38)',
          borderColor: 'rgba(248,81,73,.82)',
          borderWidth: 1,
        },
        {
          type: 'line',
          label: '净流入',
          borderDash: [5, 3],
          pointStyle: 'rectRot',
          data: amounts.map(row => row.net ?? null),
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
        y: { ticks: { color: CHART_THEME.text, callback: axisMoney, font: tickFont }, grid: { color: CHART_THEME.grid } },
        net: { position: 'right', ticks: { color: CHART_THEME.text, callback: axisMoney, font: tickFont }, grid: { drawOnChartArea: false } },
      },
    },
  });
  host.__cashflowTrendChart = trendChart;
}

  return {
    renderOverviewPeriodButtons, renderOverviewFlowButtons, renderLedgerMobileHero, renderCategoryBars, renderOverviewCategoryPieFallback, renderOverviewCategoryPie, renderOverviewCategoryInsight, renderTransferSummary, renderRules, renderTrendChart,
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
