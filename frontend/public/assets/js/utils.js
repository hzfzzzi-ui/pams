import { fmt2, fmtMoneyFixed2, fmtSignedMoneyFixed2, jget } from './api.js?v=20260606-refresh-v3';

export const byId = (id) => document.getElementById(id);

export function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

export function toFiniteNumber(value, fallback = 0) {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}

export function safeNum(x){
  const n = Number(x);
  return Number.isNaN(n) ? 0 : n;
}

export function formatPlainMoney(value, currency = '') {
  const prefix = currency ? `${currency} ` : '';
  return `${prefix}${toFiniteNumber(value).toLocaleString('zh-CN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

export function formatCurrency(value, currency = 'CNY', options = {}) {
  const n = toFiniteNumber(value);
  const code = String(currency || 'CNY').toUpperCase();
  const symbol = code === 'CNY' ? '¥' : code === 'USD' ? '$' : `${code} `;
  const sign = options.signed ? (n >= 0 ? '+' : '-') : '';
  const maximumFractionDigits = options.maximumFractionDigits ?? 2;
  const minimumFractionDigits = options.minimumFractionDigits ?? maximumFractionDigits;
  return `${sign}${symbol}${Math.abs(n).toLocaleString('zh-CN', {
    minimumFractionDigits,
    maximumFractionDigits,
  })}`;
}

export function formatMoneyFixed2(value, currency = 'CNY') {
  return fmtMoneyFixed2(value, currency);
}

export function formatSignedMoneyFixed2(value, currency = 'CNY') {
  return fmtSignedMoneyFixed2(value, currency);
}

export function formatMoneyAxis(value) {
  return toFiniteNumber(value).toLocaleString('zh-CN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

export function formatPercent(value, digits = 2) {
  return `${(toFiniteNumber(value) * 100).toFixed(digits)}%`;
}

export function formatQuantity(value, options = {}) {
  if (value === null || value === undefined || value === '') return '—';
  return toFiniteNumber(value).toLocaleString('zh-CN', {
    minimumFractionDigits: options.minimumFractionDigits ?? 0,
    maximumFractionDigits: options.maximumFractionDigits ?? 6,
  });
}

export function readOptionalNumber(idOrElement) {
  const el = typeof idOrElement === 'string' ? byId(idOrElement) : idOrElement;
  const value = el?.value ?? '';
  if (value === '') return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

export function pnlPct(r){
  const mv = safeNum(r.market_value_cny ?? r.market_value);
  const pnl = safeNum(r.holding_pnl_cny ?? r.holding_pnl);
  const explicitCost = safeNum(r.cost_cny ?? r.cost);
  const cost = explicitCost > 0 ? explicitCost : mv - pnl;
  return cost > 0 ? (pnl / cost * 100) : 0;
}

export function showError(err, id = 'err'){
  const el = byId(id);
  if (el) {
    el.style.display = 'block';
    el.textContent = err?.message || String(err);
  }
}

export function clearError(id = 'err') {
  const el = byId(id);
  if (!el) return;
  el.style.display = 'none';
  el.textContent = '';
}

export function setText(id, value) {
  const el = byId(id);
  if (el) el.textContent = value;
}

export function createLineChart(canvasId, apiPath, color, label, interval = 'daily'){
  const separator = apiPath.includes('?') ? '&' : '?';
  return jget(`${apiPath}${separator}days=90&interval=${interval}`)
    .then(history => {
      const labels = history.series.map(d => d.date);
      const values = history.series.map(d => d.value);

      const ctx = document.getElementById(canvasId);
      new Chart(ctx, {
        type: 'line',
        data: {
          labels,
          datasets: [{
            label,
            data: values,
            borderColor: color,
            borderDash: [5, 5],
            fill: false,
            tension: 0.1
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: {
            x: {
              ticks: { color: '#6e7681', maxRotation: 0, autoSkip: true },
              grid: { color: 'rgba(110,118,129,.12)' }
            },
            y: {
              beginAtZero: false,
              ticks: {
                color: '#6e7681',
                callback: (value) => '¥' + value.toLocaleString()
              },
              grid: { color: 'rgba(110,118,129,.12)' }
            }
          }
        }
      });
    });
}

export function createReturnRateChart(canvasId, apiPath, color, label, interval = 'daily'){
  const separator = apiPath.includes('?') ? '&' : '?';
  const requestPath = apiPath.includes('interval=') ? apiPath : `${apiPath}${separator}interval=${interval}`;
  return jget(requestPath)
    .then(history => {
      const rows = history.rows || history.series || [];
      const labels = rows.map(d => d.date);
      const values = rows.map(d => safeNum(d.market_value ?? d.value));
      const flows = rows.map(d => safeNum(d.net_flow));
      let fallbackBase = 0;
      const returns = rows.map(d => {
        if (d.return_rate !== undefined || d.display_return_rate !== undefined || d.capital_return_rate !== undefined || d.time_weighted_return_rate !== undefined) {
          return safeNum(d.return_rate ?? d.display_return_rate ?? d.capital_return_rate ?? d.time_weighted_return_rate) * 100;
        }
        const value = safeNum(d.value);
        if (!fallbackBase && Math.abs(value) > 0.0001) fallbackBase = value;
        return fallbackBase ? ((value / fallbackBase) - 1) * 100 : 0;
      });

      const ctx = document.getElementById(canvasId);
      if (!ctx) return null;
      const key = `__${canvasId}`;
      if (window[key]) window[key].destroy();
      window[key] = new Chart(ctx, {
        type: 'line',
        data: {
          labels,
          datasets: [{
            label,
            data: returns,
            borderColor: color,
            backgroundColor: `${color}18`,
            fill: true,
            tension: 0.2,
            pointRadius: labels.length > 45 ? 0 : 2,
            borderWidth: 2
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          animation: false,
          plugins: {
            legend: { display: false },
            tooltip: {
              callbacks: {
                label: (ctx) => [
                  `${label}：${safeNum(ctx.raw) >= 0 ? '+' : ''}${safeNum(ctx.raw).toFixed(2)}%`,
                  `市值：¥${fmt2(values[ctx.dataIndex] || 0)}`,
                  `净投入：${flows[ctx.dataIndex] >= 0 ? '+' : '-'}¥${fmt2(Math.abs(flows[ctx.dataIndex] || 0))}`,
                ],
              },
            },
          },
          scales: {
            x: {
              ticks: { color: '#6e7681', maxRotation: 0, autoSkip: true },
              grid: { color: 'rgba(110,118,129,.12)' }
            },
            y: {
              beginAtZero: false,
              ticks: {
                color: '#6e7681',
                callback: (value) => `${safeNum(value).toFixed(1)}%`
              },
              grid: { color: 'rgba(110,118,129,.12)' }
            }
          }
        }
      });
      return { labels, values, returns };
    });
}
