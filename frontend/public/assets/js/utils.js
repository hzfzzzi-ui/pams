import { jget, fmt2 } from './api.js';

export function safeNum(x){
  const n = Number(x);
  return Number.isNaN(n) ? 0 : n;
}

export function pnlPct(r){
  const mv = safeNum(r.market_value);
  const pnl = safeNum(r.holding_pnl);
  const cost = mv - pnl;
  return cost > 0 ? (pnl / cost * 100) : 0;
}

export function showError(msg){
  const el = document.getElementById('err');
  if (el) {
    el.style.display = 'block';
    el.textContent = msg;
  }
}

export function createLineChart(canvasId, apiPath, color, label, interval = 'daily'){
  return jget(`${apiPath}?days=90&interval=${interval}`)
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
