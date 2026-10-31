import { safeNum } from '../../../utils.js?v=20261031-gray-v1';
import { fmt2 } from '../../../api.js?v=20261031-gray-v1';
import { CHART_THEME } from '../../../shared/chart_theme.js?v=20261031-gray-v1';

// Legacy category-chart formula is unchanged, now owned by this investment page.
export function createCategoryChartLoader({ document, host: window, get: jget, Chart }) {
function createReturnRateChart(canvasId, apiPath, color, label, interval = 'daily'){
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
              ticks: { color: CHART_THEME.text, maxRotation: 0, autoSkip: true },
              grid: { color: CHART_THEME.grid }
            },
            y: {
              beginAtZero: false,
              ticks: {
                color: CHART_THEME.text,
                callback: (value) => `${safeNum(value).toFixed(1)}%`
              },
              grid: { color: CHART_THEME.grid }
            }
          }
        }
      });
      return { labels, values, returns };
    });
}
return createReturnRateChart;
}
