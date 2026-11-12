import { escapeHtml, formatMoneyFixed2 as money, formatPercent, formatSignedMoneyFixed2 as signedMoney, toFiniteNumber as num } from '../../utils.js';
import { CHART_THEME } from '../../shared/chart_theme.js';
import { TRANSFER_TYPES } from './transfers.js';
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
  const selectedCategories = {income: '', expense: ''};

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

function renderTransferSummary(data) {
  const rows = overviewPeriodKey() === 'month' ? data.transfer_summary_month : data.transfer_summary_ytd;
  $('overviewTransferSection').hidden = !Array.isArray(rows);
  if (!Array.isArray(rows)) return;
  $('transferSummary').innerHTML = Object.entries(TRANSFER_TYPES).map(([kind, label]) => {
    const item = rows.find(row => row.match_type === kind);
    if (kind === 'fx_conversion' && !item) return '';
    const amount = item ? item.amount : 0;
    return `<div><span>${label}</span><strong>${escapeHtml(amount == null ? '—' : money(amount))}</strong><small>${escapeHtml(item?.count ?? 0)} 对</small></div>`;
  }).join('');
}

function renderLedgerMobileHero(data) {
  const el = $('ledgerMobileHero');
  if (!el) return;
  const period = currentOverviewPeriod(data);
  const amounts = period.confirmed_subtotal ?? period;
  const disposableNet = amounts.disposable_net ?? amounts.net;
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
    </div>
    <div class="ledgerMobileStats">
      <div class="ledgerMobileStat">
        <span>投资入金</span>
        <strong>${amountText(period.investment_funding)}</strong>
      </div>
      <div class="ledgerMobileStat">
        <span>${unclassified ? '待分类' : '子类待补'}</span>
        <strong class="${unclassified ? 'down' : ''}">${unclassified || subcategoryPending}</strong>
      </div>
    </div>
    ${unclassified || otherPending ? `<p class="ledgerOverviewCoverage">${unclassified
      ? (disposableNet == null ? '暂无已确认且可计入的收支。' : '仅统计已确认且金额可用的收支。') : ''}${otherPending ? `另有 ${otherPending} 笔待核对。` : ''}</p>` : ''}
  `;
}

function renderCategoryBars(id, rows, emptyText, flowType, {selectable = false, selected} = {}) {
  const el = $(id);
  if (!el) return;
  if (!rows?.length) {
    el.innerHTML = `<div class="muted">${escapeHtml(emptyText)}</div>`;
    return;
  }
  const colors = CATEGORY_PIE_COLORS[flowType] || CATEGORY_PIE_COLORS.income;
  const rowBody = (row, index) => `
    <span class="categoryBarRank" style="--ledger-rank-color:${colors[index % colors.length]}">${index + 1}</span>
    <span class="categoryBarMain">
      <span class="categoryBarName">${escapeHtml(row.category)}</span>
      <span class="categoryBarCount">${row.count || 0} 笔</span>
    </span>
    <span class="categoryBarMeta">
      <b>${row.amount == null ? '—' : money(row.amount)}</b>
      <span>${row.share == null ? '—' : formatPercent(row.share, 1)}</span>
    </span>`;
  const rendered = selected ? [selected] : rows;
  el.innerHTML = `<div class="categoryBarList ${flowType}">${rendered.map(row => {
    const body = rowBody(row, rows.indexOf(row));
    if (!selectable) return `<div class="categoryBarRow">${body}</div>`;
    return `<button type="button" class="categoryBarRow categoryBarButton" data-overview-category="${escapeHtml(row.category)}"
      aria-expanded="${Boolean(selected)}">${body}</button>`;
  }).join('')}${selected ? `<div class="categoryBarChildren">${selected.children.map((row, index) =>
    `<div class="categoryBarRow">${rowBody(row, index)}</div>`).join('')}</div>` : ''}</div>`;
}

function renderOverviewCategoryPieFallback(canvas, chartRows, colors) {
  const wrap = canvas?.parentElement;
  if (!wrap) return;

  wrap.querySelector('.categoryPieFallback')?.remove();
  const total = chartRows.reduce((sum, row) => sum + num(row.amount), 0);
  if (!total) return;

  let cursor = 0;
  const slices = chartRows.map((row, index) => {
    const start = cursor;
    cursor += (num(row.amount) / total) * 100;
    return `${colors[index]} ${start.toFixed(2)}% ${cursor.toFixed(2)}%`;
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

function renderOverviewCategoryPie(rows, flowType, hierarchical = false) {
  const canvas = $('overviewCategoryPieChart');
  const empty = $('overviewCategoryPieEmpty');
  if (pieChart) {
    pieChart.destroy();
    if (host.__overviewCategoryPieChart === pieChart) delete host.__overviewCategoryPieChart;
    pieChart = null;
  }
  canvas?.parentElement?.querySelector('.categoryPieFallback')?.remove();
  if (!canvas) return;

  const chartRows = hierarchical ? rows.filter(row => num(row.amount) > 0) : overviewPieRows(rows);
  const hasRows = chartRows.length > 0;
  canvas.hidden = !hasRows;
  if (empty) {
    empty.hidden = hasRows;
    empty.textContent = hasRows ? '' : `暂无${overviewFlowLabel(flowType)}分类`;
  }
  if (!hasRows) return;
  const palette = CATEGORY_PIE_COLORS[flowType] || CATEGORY_PIE_COLORS.income;
  const colors = chartRows.map((row, index) => palette[(hierarchical ? rows.indexOf(row) : index) % palette.length]);
  if (typeof ChartClass !== 'function') {
    renderOverviewCategoryPieFallback(canvas, chartRows, colors);
    return;
  }
  canvas.hidden = false;

  pieChart = new ChartClass(canvas, {
    type: 'doughnut',
    data: {
      labels: chartRows.map((row) => row.category),
      datasets: [{
        data: chartRows.map((row) => num(row.amount)),
        backgroundColor: colors,
        borderColor: CHART_THEME.background,
        borderWidth: 2,
        hoverOffset: 3,
      }],
    },
    options: {
      responsive: true,
      animation: false,
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

function renderCategoryMonths(insights, overall, flowType, selected) {
  const el = $('overviewCategoryMonths');
  if (!el) return;
  const periods = insights?.periods || [];
  el.hidden = !periods.length;
  if (!periods.length) {
    el.textContent = '';
    return;
  }
  const byMonth = new Map((overall || []).map(row => [row.period, (row.confirmed_subtotal ?? row)[flowType]]));
  const amounts = selected
    ? insights.monthly[flowType][selected.category]
    : periods.map(period => byMonth.has(period) ? byMonth.get(period) : 0);
  const max = Math.max(0, ...amounts.map(num));
  const months = periods.map(trendPeriodLabel);
  el.innerHTML = `<span class="categoryMonthsLabel">每月趋势</span>
    <div class="categoryMonthBars">${amounts.map((amount, index) => {
      const value = amount == null ? '待核对' : money(amount);
      return `<button type="button" class="categoryMonthBar" aria-label="${months[index]} ${value}" aria-pressed="false"
        data-category-month style="--month-height:${max ? num(amount) / max * 100 : 0}%">
        <span class="categoryMonthTip" aria-hidden="true">${months[index]}　${value}</span>
      </button>`;
    }).join('')}</div><div class="categoryMonthAxis" aria-hidden="true">${months.length <= 2
      ? months.map(month => `<span>${month}</span>`).join('')
      : [months[0], months[Math.floor((months.length - 1) / 2)], months.at(-1)].map(month => `<span>${month}</span>`).join('')}</div>`;
}

function renderOverviewCategoryInsight(data) {
  const flowType = overviewFlowKey();
  const insights = data.category_insights;
  const rows = insights?.[overviewPeriodKey()]?.[flowType] ?? currentOverviewCategories(data, flowType);
  const selected = insights && rows.find(row => row.category === selectedCategories[flowType]);
  const total = $('overviewCategoryTotal');
  if (total) {
    const period = currentOverviewPeriod(data);
    const amount = selected ? selected.amount : (period.confirmed_subtotal ?? period)[flowType];
    total.hidden = !rows.length;
    setText('overviewCategoryTotalLabel', `${selected?.category || overviewFlowLabel(flowType)}合计`);
    setText('overviewCategoryTotalValue', amount == null ? '—' : money(amount));
  }
  setText('categoryInsightTitle', `${overviewFlowLabel(flowType)}排行`);
  renderOverviewFlowButtons();
  renderCategoryBars(
    'overviewCategoryRank',
    rows,
    `${overviewPeriodLabel()}暂无${overviewFlowLabel(flowType)}流水。`,
    flowType,
    {selectable: Boolean(insights), selected},
  );
  renderOverviewCategoryPie(selected ? selected.children : rows, flowType, Boolean(insights));
  renderCategoryMonths(insights, data.monthly_trend, flowType, selected);
}

function toggleOverviewCategory(data, category) {
  const flowType = overviewFlowKey();
  selectedCategories[flowType] = selectedCategories[flowType] === category ? '' : category;
  renderOverviewCategoryInsight(data);
  [...$('overviewCategoryRank').querySelectorAll('[data-overview-category]')]
    .find(button => button.dataset.overviewCategory === category)?.focus({preventScroll: true});
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
    renderOverviewPeriodButtons, renderOverviewFlowButtons, renderLedgerMobileHero, renderTransferSummary, renderCategoryBars, renderOverviewCategoryPieFallback, renderOverviewCategoryPie, renderOverviewCategoryInsight, toggleOverviewCategory, renderRules, renderTrendChart,
    dispose() {
      disposeTrendCharts();
      pieChart?.destroy();
      if (host.__overviewCategoryPieChart === pieChart) delete host.__overviewCategoryPieChart;
      $('overviewCategoryPieChart')?.parentElement?.querySelector('.categoryPieFallback')?.remove();
      pieChart = null;
    },
  };
}
