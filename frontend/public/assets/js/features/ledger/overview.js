import { escapeHtml, formatMoneyFixed2 as money, formatPercent, formatSignedMoneyFixed2 as signedMoney, toFiniteNumber as num } from '../../utils.js';
import { CHART_THEME } from '../../shared/chart_theme.js';
// Restore the ledger's established semantic palette without changing shared charts or chrome.
const CATEGORY_PIE_COLORS = {
  income: ['#3fb950', '#58a6ff', '#d29922', '#a371f7', '#2ea043', '#79c0ff', '#8b949e'],
  expense: ['#f85149', '#d29922', '#ff7b72', '#a371f7', '#f0883e', '#db6d28', '#8b949e'],
};

const trendAxisMoney = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 });
const trendAxisCompact = new Intl.NumberFormat('zh-CN', { notation: 'compact', maximumFractionDigits: 1 });

function trendPeriodLabel(period) {
  const match = /^\d{4}-(\d{2})(?:-(\d{2}))?$/.exec(period);
  if (!match) return period;
  const [, month, day] = match;
  return day ? `${Number(month)}/${Number(day)}` : `${Number(month)}月`;
}

function trendOptions(showPeriods) {
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    layout: { padding: { right: 14 } },
    interaction: { mode: 'index', intersect: false },
    plugins: {
      legend: { display: false },
      tooltip: {
        callbacks: {
          label: item => `${item.dataset.label} ${money(item.parsed.y)}`,
        },
      },
    },
    scales: {
      x: {
        type: 'category',
        offset: true,
        ticks: {
          display: showPeriods,
          color: CHART_THEME.text,
          font: { size: 11 },
          maxRotation: 0,
          maxTicksLimit: 6,
          callback(value) { return trendPeriodLabel(this.getLabelForValue(value)); },
        },
        grid: { display: false },
      },
      y: {
        beginAtZero: true,
        // Both plots reserve the same gutter so their dates align.
        afterFit(scale) { scale.width = 55; },
        ticks: {
          color: CHART_THEME.text,
          font: { size: 11 },
          maxTicksLimit: showPeriods ? 3 : 4,
          callback: value => (Math.abs(value) >= 10000 ? trendAxisCompact : trendAxisMoney).format(value),
        },
        grid: { color: context => context.tick.value === 0 ? 'rgba(255,255,255,.26)' : CHART_THEME.grid },
      },
    },
  };
}

export function createLedgerOverview({ root, host, state, model, dom, ChartClass = host.Chart }) {
  const { $, setText } = dom;
  const { overviewPeriodKey, overviewPeriodLabel, currentOverviewPeriod, currentOverviewCategories, overviewFlowKey, overviewFlowLabel, overviewPieRows } = model;
  let trendChart = null;
  let netTrendChart = null;
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
  const expenseImpact = amounts.expense == null ? null : -amounts.expense;
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
        <strong class="${amountTone(expenseImpact)}">${amountText(expenseImpact, signedMoney)}</strong>
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

  const hasNegativeCategory = rows.some(row => num(row.amount) < 0);
  const chartRows = hasNegativeCategory ? [] : overviewPieRows(rows);
  const hasRows = chartRows.length > 0;
  canvas.hidden = !hasRows;
  if (empty) {
    empty.hidden = hasRows;
    empty.textContent = hasRows ? '' : hasNegativeCategory ? '分类净额含退款，见下方排行。' : `暂无${overviewFlowLabel(flowType)}分类`;
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
    total.hidden = !rows.some(row => row.amount != null);
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

function disposeTrendCharts() {
  trendChart?.destroy();
  netTrendChart?.destroy();
  if (host.__cashflowTrendChart === trendChart) delete host.__cashflowTrendChart;
  trendChart = netTrendChart = null;
}

function renderTrendChart(data) {
  const rows = state.trendMode === 'weekly' ? data.weekly_trend || [] : data.monthly_trend || [];
  const coverage = $('ledgerTrendCoverage');
  if (coverage) {
    coverage.hidden = !rows.some(row => row.unclassified_count);
    coverage.textContent = coverage.hidden ? '' : '有待分类流水的期间仅显示已确认小计。';
  }
  const amounts = rows.map(row => row.confirmed_subtotal ?? row);
  const hasAmounts = amounts.some(row => row.income != null || row.expense != null || row.net != null);
  const plots = $('ledgerTrendPlots');
  const empty = $('ledgerTrendEmpty');
  const ctx = $('cashflowTrendChart');
  const netCtx = $('cashflowNetTrendChart');
  disposeTrendCharts();
  if (empty) empty.hidden = hasAmounts;
  if (plots) plots.hidden = !hasAmounts || typeof ChartClass !== 'function';
  if (!hasAmounts || !ctx || !netCtx || typeof ChartClass !== 'function') return;

  const labels = rows.map(row => row.period);
  const latestNet = amounts.at(-1).net;
  setText('ledgerTrendNetPeriod', trendPeriodLabel(labels.at(-1)));
  setText('ledgerTrendNetValue', latestNet == null ? '—' : signedMoney(latestNet));
  trendChart = new ChartClass(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [
        {
          label: '收入',
          data: amounts.map(row => row.income ?? null),
          backgroundColor: '#51b78b',
          borderRadius: 2,
          maxBarThickness: 18,
        },
        {
          label: '消费',
          data: amounts.map(row => row.expense ?? null),
          backgroundColor: '#db6d7a',
          borderRadius: 2,
          maxBarThickness: 18,
        },
      ],
    },
    options: trendOptions(false),
  });
  netTrendChart = new ChartClass(netCtx, {
    type: 'line',
    data: {
      labels,
      datasets: [{
        label: '净流入',
        data: amounts.map(row => row.net ?? null),
        borderColor: '#58a6ff',
        backgroundColor: '#58a6ff',
        borderWidth: 2,
        borderDash: [5, 3],
        pointRadius: labels.length > 20 ? 0 : 2.5,
        pointHoverRadius: 4,
        spanGaps: false,
      }],
    },
    options: trendOptions(true),
  });
  host.__cashflowTrendChart = trendChart;
}

  return {
    setChartClass(chartClass) { ChartClass = chartClass; },
    renderOverviewPeriodButtons, renderOverviewFlowButtons, renderLedgerMobileHero, renderCategoryBars, renderOverviewCategoryPieFallback, renderOverviewCategoryPie, renderOverviewCategoryInsight, renderRules, renderTrendChart,
    dispose() {
      disposeTrendCharts();
      pieChart?.destroy();
      if (host.__overviewCategoryPieChart === pieChart) delete host.__overviewCategoryPieChart;
      $('overviewCategoryPieChart')?.parentElement?.querySelector('.categoryPieFallback')?.remove();
      pieChart = null;
    },
  };
}
