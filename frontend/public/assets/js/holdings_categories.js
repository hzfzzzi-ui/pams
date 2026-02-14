import { jget, fmt2, accountLabel } from './api.js';
import { setActiveNav } from './ui.js';

function safeNum(x){
  const n=Number(x);return Number.isNaN(n)?0:n;
}

// 渲染持仓表格和汇总统计
function renderRows(rows, tbodyId, countId, totalValueId, totalPnlId, dailyPnlId, countKpiId){
  const tbody = document.getElementById(tbodyId);
  tbody.innerHTML='';

  // 计算汇总
  let totalValue = 0;
  let totalPnl = 0;
  let dailyPnl = 0;

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

    // 累加汇总数据
    totalValue += safeNum(r.market_value);
    totalPnl += safeNum(r.holding_pnl);
    dailyPnl += safeNum(r.daily_pnl);
  }

  // 更新计数
  document.getElementById(countId).textContent = rows.length;
  document.getElementById(countKpiId).textContent = rows.length;

  // 更新汇总KPI
  document.getElementById(totalValueId).textContent = fmt2(totalValue);

  const pnlElem = document.getElementById(totalPnlId);
  pnlElem.textContent = fmt2(totalPnl);
  pnlElem.style.color = totalPnl >= 0 ? 'var(--good)' : 'var(--bad)';

  const dailyElem = document.getElementById(dailyPnlId);
  dailyElem.textContent = fmt2(dailyPnl);
  dailyElem.style.color = dailyPnl >= 0 ? 'var(--good)' : 'var(--bad)';
}

// 渲染折线图
function renderChart(canvasId, category, color, interval = 'daily'){
  return jget(`/holdings/history/${category}?days=90&interval=${interval}`)
    .then(history => {
      const labels = history.series.map(d => d.date);
      const values = history.series.map(d => d.value);

      const ctx = document.getElementById(canvasId);
      new Chart(ctx, {
        type: 'line',
        data: {
          labels: labels,
          datasets: [{
            label: `${category}市值`,
            data: values,
            borderColor: color,
            backgroundColor: color.replace(')', ', 0.1)').replace('rgb', 'rgba'),
            fill: true,
            tension: 0.1
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { display: false }
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
    });
}

async function main(){
  setActiveNav();

  try {
    // 并行加载所有4个类别的数据
    const [aStock, usStock, crypto, other] = await Promise.all([
      jget('/holdings/by-category/A股?limit=500'),
      jget('/holdings/by-category/美股?limit=500'),
      jget('/holdings/by-category/加密?limit=500'),
      jget('/holdings/by-category/其他?limit=500')
    ]);

    // 设置快照时间（取第一个有效值）
    const asof = aStock.as_of || usStock.as_of || crypto.as_of || other.as_of || '--';
    document.getElementById('asof').textContent = asof;

    // 渲染持仓表格和汇总统计
    renderRows(aStock.rows || [], 'rowsAStock', 'countAStock', 'totalValueAStock', 'totalPnlAStock', 'dailyPnlAStock', 'countKpiAStock');
    renderRows(usStock.rows || [], 'rowsUSStock', 'countUSStock', 'totalValueUSStock', 'totalPnlUSStock', 'dailyPnlUSStock', 'countKpiUSStock');
    renderRows(crypto.rows || [], 'rowsCrypto', 'countCrypto', 'totalValueCrypto', 'totalPnlCrypto', 'dailyPnlCrypto', 'countKpiCrypto');
    renderRows(other.rows || [], 'rowsOther', 'countOther', 'totalValueOther', 'totalPnlOther', 'dailyPnlOther', 'countKpiOther');

    // 并行渲染所有图表
    await Promise.all([
      renderChart('chartAStock', 'A股', '#36A2EB', 'daily'),
      renderChart('chartUSStock', '美股', '#FF6384', 'daily'),
      renderChart('chartCrypto', '加密', '#FFCE56', 'daily'),
      renderChart('chartOther', '其他', '#4BC0C0', 'weekly')
    ]);

  } catch (err) {
    document.getElementById('err').style.display='block';
    document.getElementById('err').textContent = '加载失败：'+String(err);
    console.error(err);
  }
}

main();
