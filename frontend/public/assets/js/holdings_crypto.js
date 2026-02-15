import { jget, fmt2, accountLabel } from './api.js';
import { setActiveNav } from './ui.js';

function safeNum(x){
  const n=Number(x);return Number.isNaN(n)?0:n;
}

function renderRows(rows){
  const tbody = document.getElementById('rows');
  tbody.innerHTML='';

  for(const r of rows){
    const tr=document.createElement('tr');
    tr.innerHTML = `
      <td class="nowrap">${accountLabel(r.account)}</td>
      <td class="nowrap">${r.minor_category||''}</td>
      <td class="name">${r.name||''}</td>
      <td class="nowrap">${r.currency||''}</td>
      <td class="mono num">${fmt2(r.market_value)}</td>
      <td class="mono num" style="color:${safeNum(r.holding_pnl)>=0?'var(--good)':'var(--bad)'}"> ${fmt2(r.holding_pnl)}</td>
      <td class="mono num" style="color:${safeNum(r.daily_pnl)>=0?'var(--good)':'var(--bad)'}"> ${fmt2(r.daily_pnl)}</td>
    `;
    tbody.appendChild(tr);
  }

  document.getElementById('totalCount').textContent = rows.length;
}

async function renderTrendChart(){
  const history = await jget('/holdings/history/加密?days=90&interval=daily');

  const labels = history.series.map(d => d.date);
  const values = history.series.map(d => d.value);

  const ctx = document.getElementById('trendChart');
  new Chart(ctx, {
    type: 'line',
    data: {
      labels: labels,
      datasets: [{
        label: '加密资产市值',
        data: values,
        borderColor: '#FFCE56',
        backgroundColor: 'rgba(255, 206, 86, 0.1)',
        fill: true,
        tension: 0.1
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {
          display: false
        }
      },
      scales: {
        y: {
          beginAtZero: false,
          ticks: {
            callback: function(value) {
              return '¥' + value.toLocaleString();
            }
          }
        }
      }
    }
  });
}

async function main(){
  setActiveNav();

  try {
    // 加载持仓数据
    const holdings = await jget('/holdings/by-category/加密?limit=500');
    document.getElementById('asof').textContent = holdings.as_of || '--';

    const allRows = holdings.rows || [];
    renderRows(allRows);

    // 加载趋势图
    await renderTrendChart();

  } catch (err) {
    document.getElementById('err').style.display='block';
    document.getElementById('err').textContent = '加载失败：'+String(err);
  }
}

main();
