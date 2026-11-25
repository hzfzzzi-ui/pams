import { CHART_THEME } from '../../../shared/chart_theme.js';

export function drawInvestmentReturnCanvasFallback(canvas, chartData, host) {
  if (!canvas) return;
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(rect.width || canvas.clientWidth || 320, 240);
  const height = Math.max(rect.height || canvas.clientHeight || 220, 160);
  const dpr = host?.devicePixelRatio || 1;
  canvas.width = Math.floor(width * dpr);
  canvas.height = Math.floor(height * dpr);
  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);
  const labels = chartData?.labels || [];
  const visibleDatasets = (chartData?.datasets || [])
    .filter(dataset => !dataset.hidden && dataset.returnSeriesKey !== 'events')
    .map(dataset => {
      const data = dataset.data || [];
      return { ...dataset, data, values: data.filter(value => value != null).map(Number).filter(Number.isFinite) };
    })
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

export function createInvestmentReturnCanvasFallback(canvas, data, host) {
  const chart = {
    __fallback: true,
    canvas,
    data,
    options: { scales: { x: {} } },
    scales: {},
    destroy() { this.destroyed = true; },
    update() {
      if (this.destroyed) return;
      drawInvestmentReturnCanvasFallback(canvas, this.data, host);
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

export function drawInvestmentNavCanvasFallback(canvas, data, { host, formatAxisLabel }) {
  if (!canvas) return;
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(rect.width || canvas.clientWidth || 320, 240);
  const height = Math.max(rect.height || canvas.clientHeight || 160, 120);
  const dpr = host?.devicePixelRatio || 1;
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
    ctx.fillText('区间内暂无估值', 16, 28);
    return;
  }
  const pad = { left: 14, right: 16, top: 12, bottom: 22 };
  const innerW = Math.max(width - pad.left - pad.right, 1);
  const innerH = Math.max(height - pad.top - pad.bottom, 1);
  const minValue = Math.min(...known);
  const maxValue = Math.max(...known);
  const range = Math.max(maxValue - minValue, Math.max(Math.abs(maxValue), 1) * 0.02);
  const xAt = (index) => pad.left + (values.length === 1 ? innerW / 2 : innerW * index / (values.length - 1));
  const yAt = (value) => pad.top + innerH * (minValue === maxValue ? 0.5 : 1 - (value - minValue) / range);

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
  ctx.fillStyle = ctx.strokeStyle;
  ctx.lineWidth = 2;
  const firstIndex = values.findIndex(value => value != null);
  ctx.beginPath();
  ctx.moveTo(xAt(firstIndex), yAt(values[firstIndex]));
  values.forEach((value, index) => {
    if (index > firstIndex && value != null) ctx.lineTo(xAt(index), yAt(value));
  });
  if (known.length === 1) {
    ctx.arc(xAt(firstIndex), yAt(values[firstIndex]), 2, 0, Math.PI * 2);
    ctx.fill();
  } else {
    ctx.stroke();
  }

  ctx.fillStyle = CHART_THEME.text;
  ctx.font = '11px sans-serif';
  ctx.fillText(formatAxisLabel(maxValue), pad.left, 11);
  ctx.fillText(formatAxisLabel(minValue), pad.left, height - 6);
}
