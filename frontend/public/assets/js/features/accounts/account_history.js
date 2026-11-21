import { RETURN_WINDOW_OPTIONS } from '../../shared/chart_window.js';
import { CHART_THEME } from '../../shared/chart_theme.js';
import { loadChart } from '../../shared/load_chart.js';

const metrics = {
  net_value: {label: '净值', color: '#d9a13d'},
  liabilities: {label: '负债', color: '#68a9ed'},
};
const quality = {estimated: '估算', partial: '部分已知'};
const utcTime = day => Date.parse(day + 'T00:00:00Z');
const axisNumber = new Intl.NumberFormat('zh-CN', {notation: 'compact', maximumFractionDigits: 2});

function pointValue(row, key) {
  const value = row[key];
  return value != null && value !== '' && Number.isFinite(Number(value)) ? Number(value) : null;
}

function describeValue(row, key, money) {
  if (!row || pointValue(row, key) == null) return '暂无记录';
  return [money.format(row[key]), quality[row[key + '_status']]].filter(Boolean).join(' · ');
}

function draw(canvas, ChartClass, rows, keys, start, end, money) {
  const dates = rows.map(row => utcTime(row.date));
  const datasets = keys.map(key => {
    const data = rows.map((row, i) => ({x: dates[i], y: pointValue(row, key)}));
    const sparse = data.filter(point => point.y != null).length <= 4;
    return {label: metrics[key].label, data, spanGaps: true, tension: 0, borderWidth: 2, clip: 5,
      borderColor: metrics[key].color, backgroundColor: metrics[key].color,
      pointRadius: sparse ? 3 : 0, pointHoverRadius: 4, pointHitRadius: 8};
  });
  const min = utcTime(start), max = utcTime(end);
  return new ChartClass(canvas, {type: 'line', data: {datasets}, options: {
    responsive: true, maintainAspectRatio: false, animation: false, parsing: false,
    interaction: {mode: 'index', axis: 'x', intersect: false},
    plugins: {legend: {display: false}, tooltip: {callbacks: {
      title: items => rows[items[0].dataIndex].date,
      label: item => item.dataset.label + '  ' + describeValue(rows[item.dataIndex], keys[item.datasetIndex], money),
    }}},
    scales: {
      x: {type: 'linear', min, max, grid: {display: false}, border: {color: CHART_THEME.grid},
        afterBuildTicks(scale) {
          scale.ticks = [min, (min + max) / 2, max].map(value => ({value}));
        },
        ticks: {maxRotation: 0, color: CHART_THEME.text, callback: v => new Date(v).toISOString().slice(5, 10).replace('-', '/')}},
      y: {border: {display: false}, grid: {color: CHART_THEME.grid}, ticks: {maxTicksLimit: 4,
        color: CHART_THEME.text, callback: v => axisNumber.format(v)}},
    },
  }});
}

// Only an opened account detail owns a request and a chart instance.
export function mountAccountHistory(container, code, get) {
  const doc = container.ownerDocument;
  const win = doc.defaultView;
  const request = new win.AbortController();
  const section = doc.createElement('section');
  section.className = 'accountHistory';
  section.innerHTML = `
    <div class="accountHistoryHead"><div><h4>账户走势</h4><small class="accountHistoryScope"></small></div><div class="returnWindowSegment" role="group" aria-label="账户走势时间范围" hidden></div></div>
    <div class="accountHistoryValues"></div>
    <div class="accountHistoryPlot" hidden><canvas tabindex="0" role="img" aria-label="账户历史走势，左右方向键可逐日查看记录"></canvas></div>
    <p class="accountHistoryStatus" role="status"></p>
    <button class="btn" type="button" data-history-retry hidden>重新读取</button>`;
  container.append(section);
  const heading = section.querySelector('h4');
  const windows = section.querySelector('[role=group]');
  const values = section.querySelector('.accountHistoryValues');
  const plot = section.querySelector('.accountHistoryPlot');
  const canvas = plot.firstElementChild;
  const status = section.querySelector('[role=status]');
  const retry = section.querySelector('[data-history-retry]');
  let payload, keys, money, ChartClass, chart;
  let period = 'ytd';
  let visibleRows = [], selected = -1;

  function showValues(row) {
    values.replaceChildren();
    for (const key of keys) {
      const latest = row || visibleRows.findLast(item => pointValue(item, key) != null);
      const item = doc.createElement('div');
      const label = doc.createElement('span');
      label.style.setProperty('--series-color', metrics[key].color);
      label.textContent = metrics[key].label;
      const amount = doc.createElement('strong');
      amount.textContent = describeValue(latest, key, money);
      item.append(label, amount);
      if (latest && pointValue(latest, key) != null) {
        const date = doc.createElement('small');
        date.textContent = latest.date;
        item.append(date);
      }
      values.append(item);
    }
  }

  function render() {
    chart?.destroy();
    chart = null;
    selected = -1;
    canvas.setAttribute('aria-label', '账户历史走势，左右方向键可逐条查看记录');
    const start = payload.period_starts[period], end = payload.end_date;
    visibleRows = payload.rows.filter(row => row.date >= start && row.date <= end && keys.some(key => pointValue(row, key) != null));
    for (const button of windows.children) button.setAttribute('aria-pressed', String(button.dataset.window === period));
    values.replaceChildren();
    plot.hidden = !visibleRows.length || !ChartClass;
    retry.hidden = true;
    if (!visibleRows.length) {
      status.textContent = '所选时段暂无历史记录';
      return;
    }
    showValues();
    if (!ChartClass) {
      status.textContent = '图表暂未加载';
      retry.hidden = false;
      return;
    }
    chart = draw(canvas, ChartClass, visibleRows, keys, start, end, money);
    const statuses = new Set(visibleRows.flatMap(row => keys.map(key => pointValue(row, key) == null ? null : row[key + '_status'])));
    const notes = [statuses.has('estimated') && '含估算记录', statuses.has('partial') && '含部分已知记录'];
    status.textContent = notes.filter(Boolean).join(' · ');
  }

  for (const {key, label} of RETURN_WINDOW_OPTIONS) {
    const button = doc.createElement('button');
    button.type = 'button';
    button.dataset.window = key;
    button.textContent = label;
    button.addEventListener('click', () => {
      if (period === key) return;
      period = key;
      render();
    });
    windows.append(button);
  }
  canvas.addEventListener('keydown', event => {
    if (!chart || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const last = visibleRows.length - 1;
    if (event.key === 'Home') selected = 0;
    else if (event.key === 'End') selected = last;
    else selected = Math.max(0, Math.min(last, (selected < 0 ? last : selected) + (event.key === 'ArrowRight' ? 1 : -1)));
    const row = visibleRows[selected];
    const active = keys.flatMap((key, datasetIndex) => pointValue(row, key) == null ? [] : [{datasetIndex, index: selected}]);
    chart.setActiveElements(active);
    chart.tooltip.setActiveElements(active, {x: chart.scales.x.getPixelForValue(utcTime(row.date)), y: chart.chartArea.top});
    chart.update('none');
    showValues(row);
    canvas.setAttribute('aria-label', [row.date, ...keys.map(key => metrics[key].label + describeValue(row, key, money))].join('，'));
  });
  function dispose() {
    request.abort();
    chart?.destroy();
    container.removeEventListener('sheetclear', dispose);
    section.remove();
  }
  async function load() {
    retry.hidden = true;
    status.textContent = '正在读取历史记录…';
    try {
      payload ??= await get('/api/accounts/' + encodeURIComponent(code) + '/history', {cache: false, signal: request.signal});
      if (request.signal.aborted) return;
      keys = payload.series.filter(key => Object.hasOwn(metrics, key));
      money = new Intl.NumberFormat('zh-CN', {style: 'currency', currency: payload.currency});
      heading.textContent = keys.length ? keys.map(key => metrics[key].label).join('与') + '走势' : '账户走势';
      section.querySelector('.accountHistoryScope').textContent = '整户 · ' + payload.currency;
      if (payload.rows.length) ChartClass = await loadChart(win);
      if (request.signal.aborted) return;
      windows.hidden = false;
      render();
    } catch {
      if (request.signal.aborted) return;
      payload = null;
      windows.hidden = plot.hidden = true;
      status.textContent = '暂时无法读取账户走势';
      retry.hidden = false;
    }
  }
  container.addEventListener('sheetclear', dispose, {once: true});
  retry.addEventListener('click', load);
  void load();
  return dispose;
}
