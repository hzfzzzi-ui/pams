import { CHART_THEME, seriesAppearance } from '../../../shared/chart_theme.js';
import { fmt2 } from '../../../api.js';
import { safeNum } from '../../../utils.js';
import { RETURN_WINDOW_OPTIONS, normalizeReturnWindowKey, filterReturnWindowRows, buildInvestmentNavChartData } from './legacy_model.js';
import { initializeReturnState, updateVisibleReturnState, setReturnWindow } from './state.js';

// Rendering owns DOM listeners and Chart instances, never requests or shared page data.
export function createInvestmentChartRenderer({ root, host, state, presenters, ChartClass = globalThis.Chart }) {
  const { esc, statusBadge, formatNumber, formatPercentRate, fmtCny, fmtSignedCny, fmtSignedPctFromRate, TopContributionPanel } = presenters;
  const getElementById = id => root.getElementById ? root.getElementById(id) : root.querySelector(`#${id}`);
  const body = root.body || root;
  const bindings = new Map();
  let disposed = false;
  let returnChart = null;
  let navChart = null;

  function clearBindings(group) {
    for (const cleanup of bindings.get(group) || []) cleanup();
    bindings.delete(group);
  }

  function listen(group, target, type, callback) {
    const handler = event => { if (!disposed) callback(event); };
    target.addEventListener(type, handler);
    if (!bindings.has(group)) bindings.set(group, []);
    bindings.get(group).push(() => target.removeEventListener(type, handler));
  }

  function onResize() {
    if (disposed) return;
    if (returnChart?.__fallback) returnChart.update();
    if (navChart?.__fallback) navChart.update();
  }
  host.addEventListener?.('resize', onResize);

  function showAvailableReturnSeries(datasets) {
    const visibleSeries = datasets.filter(dataset => dataset.returnSeriesKey !== 'events');
    const hasData = dataset => dataset.data.some(value => value != null);
    visibleSeries.forEach(dataset => { if (!hasData(dataset)) dataset.hidden = true; });
    if (!visibleSeries.some(dataset => !dataset.hidden)) {
      const first = visibleSeries.find(hasData);
      if (first) first.hidden = false;
    }
  }

  function renderInvestmentReturnChart({ resp, sectionResponses, sectionSpecs, loadSection }) {
    if (disposed) return null;
    const el = getElementById('investmentReturnChart');
    if (!el) return null;
    const rows = resp.rows || [];
    const chartState = state;
    chartState.loadSection = loadSection;
    chartState.loadedSections = new Set(sectionSpecs.filter((_, index) => sectionResponses[index]).map(spec => spec.key));
    const isMobileReturnChart = chartState.isMobile;
    const initialWindowData = initializeReturnState(chartState, resp, sectionResponses, sectionSpecs);
    const sectionDatasets = sectionSpecs.map((spec, index) => {
      const sectionData = initialWindowData.sectionDataByKey.get(spec.key) || { data: [], pointRadius: 2 };
      return {
        returnSeriesKey: spec.key,
        label: spec.label,
        data: sectionData.data,
        borderColor: spec.color,
        ...seriesAppearance(index + 1),
        backgroundColor: `${spec.color}1a`,
        tension: 0.25,
        fill: false,
        pointRadius: context => sectionData.bridgeIndices?.has(context.dataIndex) ? 0 : sectionData.pointRadius,
        pointHitRadius: context => sectionData.bridgeIndices?.has(context.dataIndex) ? 0 : 4,
        bridgeIndices: sectionData.bridgeIndices,
        borderWidth: 1.8,
        hidden: true,
        spanGaps: false,
        order: 1,
      };
    });
    if (returnChart) returnChart.destroy();
    if (typeof ChartClass !== 'function') {
      const datasets = [{
          returnSeriesKey: 'total',
          label: '总收益率',
          data: initialWindowData.totalData,
          borderColor: CHART_THEME.line,
          hidden: false,
        }, {
          returnSeriesKey: 'events',
          label: '关键事件',
          data: initialWindowData.eventData,
          borderColor: CHART_THEME.event,
          hidden: false,
        }, ...sectionDatasets];
      showAvailableReturnSeries(datasets);
      returnChart = createInvestmentReturnCanvasFallback(el, {labels: initialWindowData.labels, datasets});
      bindReturnEventPopover(returnChart, chartState);
      renderReturnEventHotspots(returnChart, chartState);
      renderInvestmentReturnToggle(returnChart, sectionSpecs, chartState);
      renderInvestmentReturnWindowToggle(returnChart, chartState);
      renderReturnContributionPanel(chartState.topContribution);
      body.dataset.investmentReturnChartLoaded = 'fallback';
      return rows.length
        ? { latest: rows[rows.length - 1], previous: rows.length > 1 ? rows[rows.length - 2] : null, rows, todayWindow: resp.today_window || null }
        : null;
    }
    const datasets = [{
      returnSeriesKey: 'total',
      label: '总收益率',
      data: initialWindowData.totalData,
      borderColor: CHART_THEME.line,
      backgroundColor: CHART_THEME.fill,
      tension: 0.25,
      fill: true,
      pointRadius: initialWindowData.totalPointRadius,
      borderWidth: 2.4,
      order: 0,
    }, {
      returnSeriesKey: 'events',
      label: '关键事件',
      data: initialWindowData.eventData,
      borderColor: CHART_THEME.event,
      backgroundColor: CHART_THEME.event,
      tension: 0,
      fill: false,
      showLine: false,
      pointStyle: 'triangle',
      pointRadius: initialWindowData.eventPointRadius,
      pointHoverRadius: 7,
      hitRadius: 12,
      borderWidth: 0,
      spanGaps: false,
      order: -1,
    }, ...sectionDatasets];
    showAvailableReturnSeries(datasets);
    returnChart = new ChartClass(el, {
      type: 'line',
      data: {
        labels: initialWindowData.labels,
        datasets,
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        interaction: { mode: 'index', intersect: false },
        layout: { padding: { bottom: isMobileReturnChart ? 8 : 0 } },
        plugins: {
          legend: { display: false },
          tooltip: {
            filter: ctx => !ctx.dataset.bridgeIndices?.has(ctx.dataIndex),
            callbacks: {
              label: (ctx) => {
                const date = ctx.label;
                const value = safeNum(ctx.raw);
                if (ctx.dataset.returnSeriesKey === 'events') {
                  const event = (chartState.visibleEvents || []).find(item => item.date === date);
                  return event
                    ? [`${event.date} · ${event.label}`, returnEventSummary(event)]
                    : '关键事件';
                }
                if (ctx.dataset.label !== '总收益率') {
                  const estimate = chartState.sectionEstimates.get(ctx.dataset.returnSeriesKey);
                  if (estimate) {
                    return `${estimate.label || ctx.dataset.label} · ${value >= 0 ? '+' : ''}${value.toFixed(2)}%`;
                  }
                  return `${ctx.dataset.label}区间收益率：${value >= 0 ? '+' : ''}${value.toFixed(2)}%`;
                }
                const visibleRows = chartState.visibleRows || [];
                const row = visibleRows.find(item => item.date === date) || {};
                return [
                  `区间总收益率：${value >= 0 ? '+' : ''}${value.toFixed(2)}%`,
                  `单日收益率：${safeNum(row.daily_return_rate) * 100 >= 0 ? '+' : ''}${(safeNum(row.daily_return_rate) * 100).toFixed(2)}%`,
                  `累计投资收益：¥${fmt2(chartState.pnlMap.get(date) || 0)}`,
                  `投资市值：¥${fmt2(chartState.valueMap.get(date) || 0)}`,
                ];
              },
            },
          },
        },
        scales: {
          x: {
            ticks: {
              color: CHART_THEME.text,
              maxRotation: 0,
              autoSkip: true,
              autoSkipPadding: isMobileReturnChart ? 34 : 12,
              maxTicksLimit: isMobileReturnChart ? 3 : 8,
              font: { size: isMobileReturnChart ? 11 : 12 },
              padding: isMobileReturnChart ? 8 : 3,
              callback: function(value) {
                const label = this.getLabelForValue(value);
                if (!isMobileReturnChart) return label;
                return /^\d{4}-\d{2}-\d{2}$/.test(label) ? label.slice(5).replace('-', '/') : label;
              },
            },
            grid: { color: CHART_THEME.grid },
          },
          y: {
            ticks: {
              color: CHART_THEME.text,
              font: { size: isMobileReturnChart ? 11 : 12 },
              callback: v => `${safeNum(v).toFixed(1)}%`,
            },
            grid: { color: CHART_THEME.grid },
          },
        },
      },
      plugins: [{
        id: 'returnEventHotspots',
        afterRender(chart) {
          renderReturnEventHotspots(chart, chartState);
        },
      }],
    });
    bindReturnEventPopover(returnChart, chartState);
    renderReturnEventHotspots(returnChart, chartState);
    renderInvestmentReturnToggle(returnChart, sectionSpecs, chartState);
    renderInvestmentReturnWindowToggle(returnChart, chartState);
    renderReturnContributionPanel(chartState.topContribution);
    body.dataset.investmentReturnChartLoaded = 'chart';
    return rows.length
      ? { latest: rows[rows.length - 1], previous: rows.length > 1 ? rows[rows.length - 2] : null, rows, todayWindow: resp.today_window || null }
      : null;
  }

  function drawInvestmentReturnCanvasFallback(canvas, chartData) {
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const width = Math.max(rect.width || canvas.clientWidth || 320, 240);
    const height = Math.max(rect.height || canvas.clientHeight || 220, 160);
    const dpr = host.devicePixelRatio || 1;
    canvas.width = Math.floor(width * dpr);
    canvas.height = Math.floor(height * dpr);
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    const labels = chartData?.labels || [];
    const visibleDatasets = (chartData?.datasets || [])
      .filter(dataset => !dataset.hidden && dataset.returnSeriesKey !== 'events')
      .map(dataset => ({
        ...dataset,
        values: (dataset.data || []).filter(value => value != null).map(value => Number(value)).filter(value => Number.isFinite(value)),
      }))
      .filter(dataset => dataset.values.length);
    if (!labels.length || !visibleDatasets.length) {
      ctx.fillStyle = CHART_THEME.text;
      ctx.font = '12px sans-serif';
      ctx.fillText('暂无收益曲线数据', 16, 28);
      return;
    }
    const allValues = visibleDatasets.flatMap(dataset => dataset.values);
    const minValue = Math.min(...allValues);
    const maxValue = Math.max(...allValues);
    const range = Math.max(maxValue - minValue, 1);
    const pad = { left: 18, right: 18, top: 14, bottom: 24 };
    const innerW = Math.max(width - pad.left - pad.right, 1);
    const innerH = Math.max(height - pad.top - pad.bottom, 1);
    const xAt = (index) => pad.left + (labels.length === 1 ? innerW : innerW * index / (labels.length - 1));
    const yAt = (value) => pad.top + innerH - ((value - minValue) / range) * innerH;

    ctx.strokeStyle = CHART_THEME.grid;
    ctx.lineWidth = 1;
    for (let i = 0; i < 4; i += 1) {
      const y = pad.top + innerH * i / 3;
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(width - pad.right, y);
      ctx.stroke();
    }

    for (const dataset of visibleDatasets) {
      const values = dataset.data || [];
      ctx.beginPath();
      let hasPoint = false;
      let inSegment = false;
      values.forEach((value, index) => {
        if (value == null) {
          inSegment = false;
          return;
        }
        const n = Number(value);
        if (!Number.isFinite(n)) return;
        const x = xAt(index);
        const y = yAt(n);
        if (!inSegment) {
          ctx.moveTo(x, y);
          hasPoint = true;
          inSegment = true;
        } else {
          ctx.lineTo(x, y);
        }
      });
      if (!hasPoint) continue;
      ctx.strokeStyle = dataset.borderColor || CHART_THEME.line;
      ctx.lineWidth = dataset.returnSeriesKey === 'total' ? 2.2 : 1.6;
      ctx.setLineDash(dataset.borderDash || []);
      ctx.stroke();
    }
    ctx.setLineDash([]);

    ctx.fillStyle = CHART_THEME.text;
    ctx.font = '11px sans-serif';
    ctx.fillText(`${maxValue.toFixed(1)}%`, pad.left, 11);
    ctx.fillText(`${minValue.toFixed(1)}%`, pad.left, height - 6);
  }

  function createInvestmentReturnCanvasFallback(canvas, data) {
    const chart = {
      __fallback: true,
      canvas,
      data,
      options: {},
      scales: {},
      destroy() { this.destroyed = true; },
      update() {
        if (this.destroyed) return;
        drawInvestmentReturnCanvasFallback(canvas, this.data);
      },
      getDatasetMeta() {
        return { data: [] };
      },
      getElementsAtEventForMode() {
        return [];
      },
    };
    chart.update();
    return chart;
  }

  function navAxisLabel(value) {
    const n = safeNum(value);
    if (Math.abs(n) >= 10000) return `${(n / 10000).toFixed(Math.abs(n) >= 1000000 ? 0 : 1)}万`;
    return formatNumber(n, { maximumFractionDigits: 0 });
  }

  function renderInvestmentNavSummary(data) {
    const statsEl = getElementById('investmentNavStats');
    if (!statsEl) return;
    if (!data || !data.values.length) {
      statsEl.innerHTML = '<span class="muted">暂无NAV数据</span>';
      return;
    }
    statsEl.innerHTML = `
      <span class="returnNavCurrent"><em>${data.availableAccountCount ? '已覆盖净值' : '最近净值'}${data.estimated ? '（含估算）' : ''} · ${data.latestDate}</em><strong>${fmtCny(data.latest)}</strong></span>
      <span class="returnNavMetric ${data.change == null ? '' : data.change >= 0 ? 'up' : 'down'}">
        <em>区间变化</em><strong>${data.change == null ? '—' : fmtSignedCny(data.change)}</strong>
        <small>${data.changeStart ? `${data.changeStart.slice(5)} 至 ${data.latestDate.slice(5)}` : '可比区间不足'}</small>
      </span>
      <span class="returnNavMetric ${data.changeRate == null ? '' : data.changeRate >= 0 ? 'up' : 'down'}"><em>变化率</em><strong>${data.changeRate == null ? '—' : formatPercentRate(data.changeRate)}</strong></span>
    `;
  }

  function drawInvestmentNavCanvasFallback(canvas, data) {
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const width = Math.max(rect.width || canvas.clientWidth || 320, 240);
    const height = Math.max(rect.height || canvas.clientHeight || 160, 120);
    const dpr = host.devicePixelRatio || 1;
    canvas.width = Math.floor(width * dpr);
    canvas.height = Math.floor(height * dpr);
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    const values = data?.values || [];
    const known = values.filter(value => value != null);
    if (!known.length) {
      ctx.fillStyle = CHART_THEME.text;
      ctx.font = '12px sans-serif';
      ctx.fillText('暂无NAV数据', 16, 28);
      return;
    }
    const pad = { left: 14, right: 16, top: 12, bottom: 22 };
    const innerW = Math.max(width - pad.left - pad.right, 1);
    const innerH = Math.max(height - pad.top - pad.bottom, 1);
    const minValue = Math.min(...known);
    const maxValue = Math.max(...known);
    const range = Math.max(maxValue - minValue, Math.max(Math.abs(maxValue), 1) * 0.02);
    const xAt = (index) => pad.left + (values.length === 1 ? innerW : innerW * index / (values.length - 1));
    const yAt = (value) => pad.top + innerH - ((value - minValue) / range) * innerH;

    ctx.strokeStyle = CHART_THEME.grid;
    ctx.lineWidth = 1;
    for (let i = 0; i < 4; i += 1) {
      const y = pad.top + innerH * i / 3;
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(width - pad.right, y);
      ctx.stroke();
    }

    ctx.strokeStyle = CHART_THEME.line;
    ctx.fillStyle = CHART_THEME.line;
    ctx.lineWidth = 2;
    values.forEach((value, index) => {
      if (value == null) return;
      const scope = data.scopes[index];
      const x = xAt(index);
      const y = yAt(value);
      ctx.beginPath();
      if (index > 0 && values[index - 1] != null && data.scopes[index - 1] === scope) {
        ctx.setLineDash?.(scope === 'complete' ? [] : [5, 4]);
        ctx.moveTo(xAt(index - 1), yAt(values[index - 1]));
        ctx.lineTo(x, y);
        ctx.stroke();
      } else {
        ctx.arc(x, y, 2, 0, Math.PI * 2);
        ctx.fill();
      }
    });
    ctx.setLineDash?.([]);
    const lastIndex = values.findLastIndex(value => value != null);
    const lastX = xAt(lastIndex);
    const lastY = yAt(values[lastIndex]);
    ctx.beginPath();
    ctx.arc(lastX, lastY, 3.5, 0, Math.PI * 2);
    ctx.fillStyle = CHART_THEME.line;
    ctx.fill();

    ctx.fillStyle = CHART_THEME.text;
    ctx.font = '11px sans-serif';
    ctx.fillText(navAxisLabel(maxValue), pad.left, 11);
    ctx.fillText(navAxisLabel(minValue), pad.left, height - 6);
  }

  function renderInvestmentNavChart(rows, windowKey, isMobile) {
    if (disposed) return null;
    const el = getElementById('investmentNavChart');
    if (!el) return null;
    const data = buildInvestmentNavChartData(filterReturnWindowRows(rows, windowKey), isMobile);
    renderInvestmentNavSummary(data);
    if (navChart) navChart.destroy();
    if (typeof ChartClass !== 'function') {
      drawInvestmentNavCanvasFallback(el, data);
      navChart = {
        __fallback: true,
        canvas: el,
        data: { labels: data.labels, datasets: [{ label: 'NAV', data: data.values, navScopes: data.scopes }] },
        destroy() { this.destroyed = true; },
        update() {
          if (this.destroyed) return;
          drawInvestmentNavCanvasFallback(el, {
            labels: this.data.labels,
            values: this.data.datasets[0].data,
            scopes: this.data.datasets[0].navScopes,
          });
        },
      };
      return navChart;
    }
    navChart = new ChartClass(el, {
      type: 'line',
      data: {
        labels: data.labels,
        datasets: [{
          label: 'NAV',
          data: data.values,
          navScopes: data.scopes,
          navPointRadius: data.pointRadius,
          borderColor: CHART_THEME.line,
          backgroundColor: CHART_THEME.fill,
          tension: 0.25,
          fill: data.change != null && data.scopes[0] === 'complete',
          spanGaps: false,
          pointRadius: ctx => {
            if (ctx.type !== 'data') return 0;
            const {data: values, navScopes: scopes, navPointRadius: radius} = ctx.dataset;
            const index = ctx.dataIndex;
            if (values[index] == null) return 0;
            if (scopes[index] === 'complete') return radius;
            const connected = [index - 1, index + 1].some(i => values[i] != null && scopes[i] === scopes[index]);
            return connected ? 0 : 2;
          },
          segment: {
            borderColor: ctx => {
              const scopes = ctx.chart.data.datasets[ctx.datasetIndex].navScopes;
              return scopes[ctx.p0DataIndex] === scopes[ctx.p1DataIndex] ? CHART_THEME.line : 'transparent';
            },
            borderDash: ctx => ctx.chart.data.datasets[ctx.datasetIndex].navScopes[ctx.p0DataIndex] === 'complete' ? [] : [5, 4],
          },
          borderWidth: 2,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: ctx => `${ctx.dataset.navScopes[ctx.dataIndex] === 'complete' ? '组合净值' : '部分账户净值'}：${fmtCny(ctx.raw)}`,
            },
          },
        },
        scales: {
          x: {
            ticks: {
              color: CHART_THEME.text,
              maxRotation: 0,
              autoSkip: true,
              maxTicksLimit: isMobile ? 3 : 7,
              callback: function(value) {
                const label = this.getLabelForValue(value);
                return isMobile && /^\d{4}-\d{2}-\d{2}$/.test(label) ? label.slice(5).replace('-', '/') : label;
              },
            },
            grid: { color: CHART_THEME.grid },
          },
          y: {
            beginAtZero: false,
            ticks: {
              color: CHART_THEME.text,
              callback: navAxisLabel,
            },
            grid: { color: CHART_THEME.grid },
          },
        },
      },
    });
    return navChart;
  }

  function updateInvestmentNavChart(state) {
    const chart = state?.navChart || navChart;
    if (!chart || !state) return;
    const windowRows = filterReturnWindowRows(state.navRows || state.rows || [], state.activeWindow);
    const data = buildInvestmentNavChartData(windowRows, state.isMobile);
    chart.data.labels = data.labels;
    chart.data.datasets[0].data = data.values;
    chart.data.datasets[0].navScopes = data.scopes;
    if (chart.__fallback) {
      chart.update();
      renderInvestmentNavSummary(data);
      return;
    }
    chart.data.datasets[0].fill = data.change != null && data.scopes[0] === 'complete';
    chart.data.datasets[0].navPointRadius = data.pointRadius;
    chart.update('none');
    renderInvestmentNavSummary(data);
  }

  function returnEventSummary(event) {
    const parts = [
      `单日 ${fmtSignedPctFromRate(event.dailyRate)}`,
      `收益 ${fmtSignedCny(event.pnlDelta)}`,
    ];
    if (Math.abs(event.realizedDelta) >= 1000) parts.push(`已实现 ${fmtSignedCny(event.realizedDelta)}`);
    if (Math.abs(event.costDelta) >= 12000) parts.push(`成本 ${fmtSignedCny(event.costDelta)}`);
    return parts.join(' · ');
  }

  function renderReturnContributionPanel(model) {
    const el = getElementById('profitContributionPanel');
    if (!el) return;
    if (state.contributionPending && model?.unavailable) {
      el.innerHTML = '<div class="returnInsightHead"><h3>Top 贡献</h3></div><p role="status">正在加载收益贡献…</p>';
      return;
    }
    el.innerHTML = TopContributionPanel(model || {
      mode: 'period',
      windowLabel: '当前窗口',
      totalPnl: 0,
      winners: [],
      losers: [],
    });
  }

  function hideReturnEventPopover() {
    const el = getElementById('returnEventPopover');
    if (!el) return;
    el.hidden = true;
    el.innerHTML = '';
  }

  function showReturnEventPopover(event, point) {
    const el = getElementById('returnEventPopover');
    const wrap = el?.closest?.('.chartWrap');
    if (!el || !wrap || !event) return;
    const width = Math.min(280, Math.max(220, wrap.clientWidth - 16));
    const left = Math.min(Math.max(safeNum(point?.x) + 12, 8), Math.max(8, wrap.clientWidth - width - 8));
    const top = Math.min(Math.max(safeNum(point?.y) - 8, 8), Math.max(8, wrap.clientHeight - 116));
    el.style.left = `${left}px`;
    el.style.top = `${top}px`;
    el.style.maxWidth = `${width}px`;
    el.innerHTML = `
      <div class="returnEventPopoverHead">
        <strong>${esc(event.date)} · ${esc(event.label)}</strong>
        ${statusBadge('关键点', event.tone === 'bad' ? 'pending' : event.tone === 'warn' ? 'warn' : 'data')}
      </div>
      <div class="returnEventPopoverBody">${esc(returnEventSummary(event))}</div>
    `;
    el.hidden = false;
  }

  function renderReturnEventHotspots(chart, state) {
    if (disposed || !chart || !state) return;
    clearBindings('hotspots');
    const layer = getElementById('returnEventHotspots');
    if (!layer) return;
    const eventDatasetIndex = chart.data.datasets.findIndex(dataset => dataset.returnSeriesKey === 'events');
    const eventDataset = eventDatasetIndex >= 0 ? chart.data.datasets[eventDatasetIndex] : null;
    const eventMeta = eventDatasetIndex >= 0 ? chart.getDatasetMeta(eventDatasetIndex) : null;
    const labels = chart.data.labels || [];
    const events = state.visibleEvents || [];
    if (!eventDataset || !events.length) {
      layer.innerHTML = '';
      return;
    }
    const eventByDate = new Map(events.map(event => [event.date, event]));
    const buttons = [];
    labels.forEach((date, index) => {
      const event = eventByDate.get(date);
      const value = eventDataset.data?.[index];
      if (!event || value == null || !Number.isFinite(Number(value))) return;
      const point = eventMeta?.data?.[index];
      const x = Number.isFinite(Number(point?.x)) ? point.x : chart.scales?.x?.getPixelForValue?.(index);
      const y = Number.isFinite(Number(point?.y)) ? point.y : chart.scales?.y?.getPixelForValue?.(value);
      if (!Number.isFinite(Number(x)) || !Number.isFinite(Number(y))) return;
      buttons.push(`
        <button
          type="button"
          class="returnEventHotspot ${esc(event.tone)}"
          data-return-event-date="${esc(event.date)}"
          style="left:${safeNum(x).toFixed(1)}px; top:${safeNum(y).toFixed(1)}px"
          aria-label="${esc(`${event.date} ${event.label}`)}"
        ></button>
      `);
    });
    layer.innerHTML = buttons.join('');
    layer.querySelectorAll('[data-return-event-date]').forEach((btn) => {
      listen('hotspots', btn, 'click', (event) => {
        event.preventDefault();
        event.stopPropagation();
        const item = eventByDate.get(btn.dataset.returnEventDate);
        showReturnEventPopover(item, {
          x: parseFloat(btn.style.left || '0'),
          y: parseFloat(btn.style.top || '0'),
        });
      });
    });
  }

  function bindReturnEventPopover(chart, state) {
    if (!chart || !state) return;
    chart.options.onClick = (nativeEvent) => {
      if (disposed) return;
      const elements = chart.getElementsAtEventForMode(
        nativeEvent,
        'nearest',
        { intersect: true },
        true,
      );
      const eventPoint = elements.find(item => chart.data.datasets[item.datasetIndex]?.returnSeriesKey === 'events');
      if (!eventPoint) {
        hideReturnEventPopover();
        return;
      }
      const date = chart.data.labels[eventPoint.index];
      const event = (state.visibleEvents || []).find(item => item.date === date);
      showReturnEventPopover(event, eventPoint.element);
    };
  }

  function applyInvestmentReturnWindow(chart, state, nextWindowKey, persist = true) {
    if (disposed || !state) return;
    setReturnWindow(state, nextWindowKey, persist);
    if (!chart) {
      updateInvestmentNavChart(state);
      return;
    }
    const windowData = updateVisibleReturnState(state);
    chart.data.labels = windowData.labels;
    chart.data.datasets.forEach(dataset => {
      if (dataset.returnSeriesKey === 'events') {
        dataset.data = windowData.eventData;
        dataset.pointRadius = windowData.eventPointRadius;
        return;
      }
      if (dataset.returnSeriesKey === 'total') {
        dataset.data = windowData.totalData;
        dataset.pointRadius = windowData.totalPointRadius;
        return;
      }
      const sectionData = windowData.sectionDataByKey.get(dataset.returnSeriesKey);
      if (sectionData) {
        dataset.data = sectionData.data;
        dataset.bridgeIndices = sectionData.bridgeIndices;
        dataset.pointRadius = context => sectionData.bridgeIndices.has(context.dataIndex) ? 0 : sectionData.pointRadius;
        dataset.pointHitRadius = context => sectionData.bridgeIndices.has(context.dataIndex) ? 0 : 4;
      }
    });
    if (state.totalCoverage && !chart.data.datasets.some(row => row.returnSeriesKey !== 'events' && !row.hidden)) {
      const source = chart.data.datasets.find(row => row.returnSeriesKey === 'a_stock');
      if (source) source.hidden = false;
    }
    showAvailableReturnSeries(chart.data.datasets);
    renderInvestmentReturnToggle(chart, state.sectionSpecs, state);
    chart.update('none');
    hideReturnEventPopover();
    renderReturnEventHotspots(chart, state);
    updateInvestmentNavChart(state);
    renderReturnContributionPanel(windowData.topContribution);
  }

  function renderInvestmentReturnWindowToggle(chart, state) {
    clearBindings('window');
    const el = getElementById('investmentReturnWindowToggle');
    if (!el || !state) return;
    const activeWindow = normalizeReturnWindowKey(state.activeWindow);
    el.innerHTML = RETURN_WINDOW_OPTIONS.map(item => `
      <button type="button" data-return-window="${esc(item.key)}" class="${item.key === activeWindow ? 'active' : ''}" aria-pressed="${item.key === activeWindow ? 'true' : 'false'}">
        ${esc(item.label)}
      </button>
    `).join('');
    el.querySelectorAll('[data-return-window]').forEach(btn => {
      listen('window', btn, 'click', () => {
        const key = normalizeReturnWindowKey(btn.dataset.returnWindow);
        applyInvestmentReturnWindow(chart, state, key);
        el.querySelectorAll('[data-return-window]').forEach(item => {
          const active = item.dataset.returnWindow === key;
          item.classList.toggle('active', active);
          item.setAttribute('aria-pressed', active ? 'true' : 'false');
        });
      });
    });
  }

  function renderInvestmentReturnToggle(chart, sectionSpecs, state = null) {
    clearBindings('series');
    const el = getElementById('investmentReturnToggle');
    if (!el || !chart) return;
    const status = getElementById('investmentReturnSeriesStatus');
    const showStatus = (message = '') => {
      if (!status) return;
      status.hidden = !message;
      status.textContent = message;
    };
    const totalStatus = () => state?.totalCoverage && chart.data.datasets.some(row => row.returnSeriesKey === 'total' && !row.hidden)
      ? `部分覆盖：${state.totalCoverage}，按累计盈亏 / 净投入计算` : '';
    showStatus(totalStatus());
    el.setAttribute('aria-multiselectable', 'true');
    const options = [
      { key: 'total', label: '总收益率', datasetLabel: '总收益率', color: CHART_THEME.line },
      ...sectionSpecs.map(spec => ({ key: spec.key, label: spec.label, datasetLabel: spec.label, color: spec.color })),
    ];
    const syncButtons = () => {
      el.querySelectorAll('[data-return-series]').forEach(btn => {
        const item = options.find(option => option.key === btn.dataset.returnSeries);
        const dataset = chart.data.datasets.find(row => row.label === item?.datasetLabel);
        const active = dataset ? !dataset.hidden : false;
        btn.classList.toggle('active', active);
        btn.setAttribute('aria-pressed', active ? 'true' : 'false');
      });
    };
    const toggleSeries = async (mode) => {
      const selected = options.find(item => item.key === mode) || options[0];
      const wasVisible = chart.data.datasets.some(row => row.returnSeriesKey === mode && !row.hidden);
      if (mode !== 'total' && state?.loadSection && !state.loadedSections.has(mode)) {
        showStatus(`正在加载${selected.label}…`);
        if (!await state.loadSection(mode) || disposed || chart !== returnChart) return;
        if (!wasVisible && chart.data.datasets.some(row => row.returnSeriesKey === mode && !row.hidden)) return;
      }
      const dataset = chart.data.datasets.find(row => row.label === selected.datasetLabel);
      const closed = state?.activeWindow === 'ytd' && state.sectionClosedTradeReturns?.get(selected.key);
      const closedStatus = closed?.base_currency === 'CNY' && closed.amount != null
        ? `${selected.label}年内已核对清仓 ${fmtSignedCny(Number(closed.amount))}` : '';
      if (mode === 'total' && !dataset?.data.some(value => value != null) && state?.loadSection && !state.loadedSections.has('a_stock')) {
        showStatus('正在加载可用收益…');
        if (!await state.loadSection('a_stock') || disposed || chart !== returnChart) return;
        if (!dataset.hidden) return;
      }
      if (!dataset?.data.some(value => value != null)) {
        const reason = state?.sectionReasons?.get(selected.key);
        if (closedStatus) {
          showStatus(`${closedStatus} · 完整收益率待核验`);
          return;
        }
        showStatus(reason === 'ibkr_position_history_unavailable'
          ? '美股缺少 IBKR 历史持仓，暂无法绘制收益曲线。'
          : `${selected.label}在此区间暂无可用收益率。`);
        return;
      }
      const estimate = state?.sectionEstimates.get(selected.key);
      let estimateStatus = '';
      if (estimate?.method === 'ibkr_unrealized_pnl_over_position_cost') {
        estimateStatus = 'IBKR 美股持仓估算按未实现盈亏除以持仓成本计算。';
      } else if (estimate?.method === 'fund_partial_snapshot_return') {
        estimateStatus = '基金曲线仅覆盖部分历史估值';
      }
      showStatus(dataset.hidden ? [estimateStatus, closedStatus].filter(Boolean).join(' · ') : '');
      const visibleCount = chart.data.datasets.filter(row => row.returnSeriesKey !== 'events' && !row.hidden).length;
      if (!dataset.hidden && visibleCount <= 1) return;
      dataset.hidden = !dataset.hidden;
      if (state?.totalCoverage && mode === 'total' && !dataset.hidden) {
        const source = chart.data.datasets.find(row => row.returnSeriesKey === 'a_stock');
        if (source) source.hidden = true;
      }
      if (mode === 'total') showStatus(totalStatus());
      syncButtons();
      chart.update('none');
      if (state) renderReturnEventHotspots(chart, state);
    };
    el.innerHTML = options.map(item => {
      return `
      <button type="button" data-return-series="${esc(item.key)}" style="--return-series-color:${esc(item.color || CHART_THEME.line)}">
        ${esc(item.label)}
      </button>
    `;
    }).join('');
    el.querySelectorAll('[data-return-series]').forEach(btn => {
      listen('series', btn, 'click', () => {
        toggleSeries(btn.dataset.returnSeries).catch(() => showStatus('曲线加载失败，请重试。'));
      });
    });
    syncButtons();
    chart.update('none');
    if (state) renderReturnEventHotspots(chart, state);
  }

  return {
    setChartClass(chartClass) { ChartClass = chartClass; },
    renderReturns: renderInvestmentReturnChart,
    updateReturnSeries({ resp, sectionResponses, sectionSpecs }) {
      if (!returnChart || disposed) return;
      state.loadedSections = new Set(sectionSpecs.filter((_, index) => sectionResponses[index]).map(spec => spec.key));
      initializeReturnState(state, resp, sectionResponses, sectionSpecs);
      applyInvestmentReturnWindow(returnChart, state, state.activeWindow, false);
    },
    renderNav: renderInvestmentNavChart,
    renderWindowToggle: renderInvestmentReturnWindowToggle,
    renderContribution: renderReturnContributionPanel,
    applyWindow: applyInvestmentReturnWindow,
    get returnChart() { return returnChart; },
    get navChart() { return navChart; },
    dispose() {
      if (disposed) return;
      disposed = true;
      for (const group of [...bindings.keys()]) clearBindings(group);
      host.removeEventListener?.('resize', onResize);
      if (returnChart?.options) returnChart.options.onClick = null;
      returnChart?.destroy();
      navChart?.destroy();
      if (host.__investmentReturnChart === returnChart) delete host.__investmentReturnChart;
      if (host.__investmentNavChart === navChart) delete host.__investmentNavChart;
      hideReturnEventPopover();
      const hotspots = getElementById('returnEventHotspots');
      if (hotspots) hotspots.innerHTML = '';
      state.navChart = null;
      returnChart = null;
      navChart = null;
    },
  };
}

