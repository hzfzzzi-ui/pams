import { CHART_THEME, seriesAppearance } from '../../../shared/chart_theme.js?v=20261031-modules-v1';
import { fmt2 } from '../../../api.js?v=20261031-modules-v1';
import { safeNum } from '../../../utils.js?v=20261031-modules-v1';
import { RETURN_WINDOW_OPTIONS, normalizeReturnWindowKey, returnWindowLabel, filterReturnWindowRows, buildInvestmentNavChartData } from './legacy_model.js?v=20261031-modules-v1';
import { initializeReturnState, updateVisibleReturnState, setReturnWindow } from './state.js?v=20261031-modules-v1';

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

  function renderInvestmentReturnChart({ resp, sectionResponses, sectionSpecs }) {
    if (disposed) return null;
    const el = getElementById('investmentReturnChart');
    if (!el) return null;
    const rows = resp.rows || [];
    const chartState = state;
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
        pointRadius: sectionData.pointRadius,
        borderWidth: 1.8,
        hidden: true,
        spanGaps: true,
        order: 1,
      };
    });
    const noteEl = getElementById('investmentReturnNote');
    if (noteEl) {
      const notes = resp.coverage_notes || [];
      noteEl.style.display = notes.length ? 'block' : 'none';
      noteEl.textContent = notes.length
        ? `含多口径数据，分类线仅作参考。${notes.join(' ')}`
        : '';
    }

    if (returnChart) returnChart.destroy();
    if (typeof ChartClass !== 'function') {
      returnChart = createInvestmentReturnCanvasFallback(el, {
        labels: initialWindowData.labels,
        datasets: [{
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
        }, ...sectionDatasets],
      });
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
    returnChart = new ChartClass(el, {
      type: 'line',
      data: {
        labels: initialWindowData.labels,
        datasets: [{
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
        }, ...sectionDatasets],
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
                  return `${ctx.dataset.label}区间收益率：${value >= 0 ? '+' : ''}${value.toFixed(2)}%`;
                }
                const visibleRows = chartState.visibleRows || [];
                const row = visibleRows[ctx.dataIndex] || {};
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
        values: (dataset.data || []).map(value => Number(value)).filter(value => Number.isFinite(value)),
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
      values.forEach((value, index) => {
        const n = Number(value);
        if (!Number.isFinite(n)) return;
        const x = xAt(index);
        const y = yAt(n);
        if (!hasPoint) {
          ctx.moveTo(x, y);
          hasPoint = true;
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

  function renderInvestmentNavSummary(data, windowKey) {
    const rangeEl = getElementById('investmentNavRange');
    const statsEl = getElementById('investmentNavStats');
    if (rangeEl) rangeEl.textContent = returnWindowLabel(windowKey);
    if (!statsEl) return;
    if (!data || !data.values.length) {
      statsEl.innerHTML = '<span class="muted">暂无NAV数据</span>';
      return;
    }
    statsEl.innerHTML = `
      <span><em>当前</em><strong>${fmtCny(data.latest)}</strong></span>
      <span class="${data.change >= 0 ? 'up' : 'down'}"><em>区间</em><strong>${fmtSignedCny(data.change)}</strong></span>
      <span class="${data.changeRate >= 0 ? 'up' : 'down'}"><em>变化</em><strong>${formatPercentRate(data.changeRate)}</strong></span>
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
    if (!values.length) {
      ctx.fillStyle = CHART_THEME.text;
      ctx.font = '12px sans-serif';
      ctx.fillText('暂无NAV数据', 16, 28);
      return;
    }
    const pad = { left: 14, right: 16, top: 12, bottom: 22 };
    const innerW = Math.max(width - pad.left - pad.right, 1);
    const innerH = Math.max(height - pad.top - pad.bottom, 1);
    const minValue = Math.min(...values);
    const maxValue = Math.max(...values);
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

    const gradient = ctx.createLinearGradient(0, pad.top, 0, height - pad.bottom);
    gradient.addColorStop(0, 'rgba(245,245,247,.12)');
    gradient.addColorStop(1, 'rgba(245,245,247,0)');
    ctx.beginPath();
    values.forEach((value, index) => {
      const x = xAt(index);
      const y = yAt(value);
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.lineTo(xAt(values.length - 1), height - pad.bottom);
    ctx.lineTo(xAt(0), height - pad.bottom);
    ctx.closePath();
    ctx.fillStyle = gradient;
    ctx.fill();

    ctx.beginPath();
    values.forEach((value, index) => {
      const x = xAt(index);
      const y = yAt(value);
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = CHART_THEME.line;
    ctx.lineWidth = 2;
    ctx.stroke();

    const lastX = xAt(values.length - 1);
    const lastY = yAt(values[values.length - 1]);
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
    renderInvestmentNavSummary(data, windowKey);
    if (navChart) navChart.destroy();
    if (typeof ChartClass !== 'function') {
      drawInvestmentNavCanvasFallback(el, data);
      navChart = {
        __fallback: true,
        canvas: el,
        data: { labels: data.labels, datasets: [{ label: 'NAV', data: data.values }] },
        destroy() { this.destroyed = true; },
        update() {
          if (this.destroyed) return;
          drawInvestmentNavCanvasFallback(el, {
            labels: this.data.labels,
            values: this.data.datasets[0].data,
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
          borderColor: CHART_THEME.line,
          backgroundColor: CHART_THEME.fill,
          tension: 0.25,
          fill: true,
          pointRadius: data.pointRadius,
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
              label: (ctx) => `NAV：${fmtCny(ctx.raw)}`,
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
    if (chart.__fallback) {
      chart.update();
      renderInvestmentNavSummary(data, state.activeWindow);
      return;
    }
    chart.data.datasets[0].pointRadius = data.pointRadius;
    chart.update('none');
    renderInvestmentNavSummary(data, state.activeWindow);
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
        dataset.pointRadius = sectionData.pointRadius;
      }
    });
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
    el.setAttribute('aria-multiselectable', 'true');
    const options = [
      { key: 'total', label: '总收益率', datasetLabel: '总收益率' },
      ...sectionSpecs.map(spec => ({ key: spec.key, label: spec.label, datasetLabel: spec.label })),
    ];
    const syncButtons = () => {
      el.querySelectorAll('[data-return-series]').forEach(btn => {
        const item = options.find(option => option.key === btn.dataset.returnSeries);
        const dataset = chart.data.datasets.find(row => row.label === item?.datasetLabel);
        const active = dataset ? !dataset.hidden : false;
        btn.classList.toggle('active', active);
        btn.disabled = false;
        btn.title = '';
        btn.setAttribute('aria-pressed', active ? 'true' : 'false');
      });
    };
    const toggleSeries = (mode) => {
      const selected = options.find(item => item.key === mode) || options[0];
      const dataset = chart.data.datasets.find(row => row.label === selected.datasetLabel);
      if (!dataset) return;
      const visibleCount = chart.data.datasets.filter(row => row.returnSeriesKey !== 'events' && !row.hidden).length;
      if (!dataset.hidden && visibleCount <= 1) return;
      dataset.hidden = !dataset.hidden;
      syncButtons();
      chart.update('none');
      if (state) renderReturnEventHotspots(chart, state);
    };
    el.innerHTML = options.map((item, index) => {
      return `
      <button type="button" data-return-series="${esc(item.key)}" class="${index === 0 ? 'active' : ''}" aria-pressed="${index === 0 ? 'true' : 'false'}">
        ${esc(item.label)}
      </button>
    `;
    }).join('');
    el.querySelectorAll('[data-return-series]').forEach(btn => {
      listen('series', btn, 'click', () => toggleSeries(btn.dataset.returnSeries));
    });
    chart.data.datasets.forEach((dataset) => {
      dataset.hidden = dataset.returnSeriesKey === 'events' ? false : dataset.label !== '总收益率';
    });
    syncButtons();
    chart.update('none');
    if (state) renderReturnEventHotspots(chart, state);
  }

  return {
    renderReturns: renderInvestmentReturnChart,
    renderNav: renderInvestmentNavChart,
    renderWindowToggle: renderInvestmentReturnWindowToggle,
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
