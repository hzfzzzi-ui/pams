import { jget, fmt2, accountLabel } from './api.js';
import { setActiveNav } from './ui.js';
import { safeNum, pnlPct, createLineChart } from './utils.js';

// 渲染行: A股/美股 (7列: 代码,名称,买入价格,数量,当前价格,持仓市值,盈亏比)
function renderRowStock(r){
  const pct = pnlPct(r);
  return `
    <td class="nowrap">${r.symbol||'—'}</td>
    <td class="name">${r.name||''}</td>
    <td class="mono num">${r.buy_price ? fmt2(r.buy_price) : '—'}</td>
    <td class="mono num">${r.quantity ? fmt2(r.quantity) : '—'}</td>
    <td class="mono num">${r.current_price ? fmt2(r.current_price) : '—'}</td>
    <td class="mono num">${fmt2(r.market_value)}</td>
    <td class="mono num" style="color:${pct>=0?'var(--good)':'var(--bad)'}">${pct>=0?'+':''}${pct.toFixed(2)}%</td>
  `;
}

// 渲染行: 加密 (6列: 名称,买入价格,数量,当前价格,持仓市值,盈亏比)
function renderRowCrypto(r){
  const pct = pnlPct(r);
  return `
    <td class="name">${r.name||''}</td>
    <td class="mono num">${r.buy_price ? fmt2(r.buy_price) : '—'}</td>
    <td class="mono num">${r.quantity ? fmt2(r.quantity) : '—'}</td>
    <td class="mono num">${r.current_price ? fmt2(r.current_price) : '—'}</td>
    <td class="mono num">${fmt2(r.market_value)}</td>
    <td class="mono num" style="color:${pct>=0?'var(--good)':'var(--bad)'}">${pct>=0?'+':''}${pct.toFixed(2)}%</td>
  `;
}

// 渲染行: 其他 (6列: 账户,小类,名称,币种,市值,盈亏比)
function renderRowOther(r){
  const pct = pnlPct(r);
  return `
    <td class="nowrap">${accountLabel(r.account)}</td>
    <td class="nowrap">${r.minor_category||''}</td>
    <td class="name">${r.name||''}</td>
    <td class="nowrap">${r.currency||''}</td>
    <td class="mono num">${fmt2(r.market_value)}</td>
    <td class="mono num" style="color:${pct>=0?'var(--good)':'var(--bad)'}">${pct>=0?'+':''}${pct.toFixed(2)}%</td>
  `;
}

// 渲染持仓表格和汇总统计
function renderSection(rows, renderRowFn, tbodyId, countId, totalValueId, totalPnlId, dailyPnlId, countKpiId, holdingPnlId, fxRates){
  const tbody = document.getElementById(tbodyId);
  tbody.innerHTML='';

  let totalValue = 0;
  let totalPnl = 0;
  let dailyPnl = 0;

  for(const r of rows){
    const tr=document.createElement('tr');
    tr.innerHTML = renderRowFn(r);
    tbody.appendChild(tr);

    const fx = (fxRates && r.currency) ? (fxRates[r.currency] || 1) : 1;
    totalValue += safeNum(r.market_value) * fx;
    totalPnl += safeNum(r.holding_pnl) * fx;
    dailyPnl += safeNum(r.daily_pnl) * fx;
  }

  document.getElementById(countId).textContent = rows.length;
  document.getElementById(countKpiId).textContent = rows.length;
  document.getElementById(totalValueId).textContent = fmt2(totalValue);

  const pnlElem = document.getElementById(totalPnlId);
  pnlElem.textContent = fmt2(totalPnl);
  pnlElem.style.color = totalPnl >= 0 ? 'var(--good)' : 'var(--bad)';

  const dailyElem = document.getElementById(dailyPnlId);
  dailyElem.textContent = fmt2(dailyPnl);
  dailyElem.style.color = dailyPnl >= 0 ? 'var(--good)' : 'var(--bad)';

  if (holdingPnlId) {
    const hpElem = document.getElementById(holdingPnlId);
    const sectionCost = totalValue - totalPnl;
    const sectionPct = sectionCost > 0 ? (totalPnl / sectionCost * 100) : 0;
    hpElem.textContent = (sectionPct >= 0 ? '+' : '') + sectionPct.toFixed(2) + '%';
    hpElem.style.color = sectionPct >= 0 ? 'var(--good)' : 'var(--bad)';
  }

  return { totalValue, totalPnl, dailyPnl };
}

async function main(){
  setActiveNav();

  try {
    const [aStock, usStock, crypto, preciousMetal, other, fxData] = await Promise.all([
      jget('/holdings/by-category/A股?limit=500'),
      jget('/holdings/by-category/美股?limit=500'),
      jget('/holdings/by-category/加密?limit=500'),
      jget('/holdings/by-category/贵金属?limit=500'),
      jget('/holdings/by-category/其他?limit=500'),
      jget('/api/fx-rates/latest?base_currency=CNY')
    ]);

    // 构建汇率映射: {CNY: 1, USD: 7.25, MXN: 0.35, ...}
    const fxRates = { CNY: 1 };
    for (const r of fxData.rates || []) {
      fxRates[r.quote_currency] = Number(r.rate);
    }

    const asof = aStock.as_of || usStock.as_of || crypto.as_of || preciousMetal.as_of || other.as_of || '--';
    document.getElementById('asof').textContent = asof;

    const categories = [
      { label: 'A股', color: '#f85149', ...renderSection(aStock.rows || [], renderRowStock, 'rowsAStock', 'countAStock', 'totalValueAStock', 'totalPnlAStock', 'dailyPnlAStock', 'countKpiAStock', 'holdingPnlAStock', fxRates) },
      { label: '美股', color: '#58a6ff', ...renderSection(usStock.rows || [], renderRowStock, 'rowsUSStock', 'countUSStock', 'totalValueUSStock', 'totalPnlUSStock', 'dailyPnlUSStock', 'countKpiUSStock', 'holdingPnlUSStock', fxRates) },
      { label: '加密', color: '#bc8cff', ...renderSection(crypto.rows || [], renderRowCrypto, 'rowsCrypto', 'countCrypto', 'totalValueCrypto', 'totalPnlCrypto', 'dailyPnlCrypto', 'countKpiCrypto', 'holdingPnlCrypto', fxRates) },
      { label: '贵金属', color: '#d29922', ...renderSection(preciousMetal.rows || [], renderRowCrypto, 'rowsPreciousMetal', 'countPreciousMetal', 'totalValuePreciousMetal', 'totalPnlPreciousMetal', 'dailyPnlPreciousMetal', 'countKpiPreciousMetal', 'holdingPnlPreciousMetal', fxRates) },
      { label: '其他', color: '#6e7681', ...renderSection(other.rows || [], renderRowOther, 'rowsOther', 'countOther', 'totalValueOther', 'totalPnlOther', 'dailyPnlOther', 'countKpiOther', 'holdingPnlOther', fxRates) },
    ];

    // 汇总统计（所有值已通过FX转换为CNY）
    const grandValue = categories.reduce((s, c) => s + c.totalValue, 0);
    const grandPnl = categories.reduce((s, c) => s + c.totalPnl, 0);
    const grandDaily = categories.reduce((s, c) => s + c.dailyPnl, 0);
    const grandCost = grandValue - grandPnl;
    const pnlPct = grandCost > 0 ? (grandPnl / grandCost * 100) : 0;

    document.getElementById('grandTotalValue').textContent = '¥' + fmt2(grandValue);

    const gpElem = document.getElementById('grandTotalPnl');
    gpElem.textContent = (grandPnl >= 0 ? '+¥' : '-¥') + fmt2(Math.abs(grandPnl));
    gpElem.style.color = grandPnl >= 0 ? 'var(--good)' : 'var(--bad)';

    const pctElem = document.getElementById('grandTotalPnlPct');
    pctElem.textContent = (pnlPct >= 0 ? '+' : '') + pnlPct.toFixed(2) + '%';
    pctElem.style.color = pnlPct >= 0 ? 'var(--good)' : 'var(--bad)';

    const gdElem = document.getElementById('grandDailyPnl');
    gdElem.textContent = (grandDaily >= 0 ? '+¥' : '-¥') + fmt2(Math.abs(grandDaily));
    gdElem.style.color = grandDaily >= 0 ? 'var(--good)' : 'var(--bad)';

    // 资产分布饼图（带百分比标签）
    const pieData = categories.filter(c => c.totalValue > 0);
    const pieTotal = pieData.reduce((s, c) => s + c.totalValue, 0);
    const pieCtx = document.getElementById('categoryPieChart');
    new Chart(pieCtx, {
      type: 'doughnut',
      data: {
        labels: pieData.map(c => {
          const pct = ((c.totalValue / pieTotal) * 100).toFixed(1);
          return `${c.label} ${pct}%`;
        }),
        datasets: [{
          data: pieData.map(c => c.totalValue),
          backgroundColor: pieData.map(c => c.color),
          borderColor: '#161a1e',
          borderWidth: 2
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: true,
        plugins: {
          legend: { position: 'bottom', labels: { color: '#e1e4e8', padding: 16 } },
          tooltip: {
            callbacks: {
              label: (ctx) => {
                const pct = ((ctx.raw / pieTotal) * 100).toFixed(1);
                return `${ctx.label}: ¥${fmt2(ctx.raw)} (${pct}%)`;
              }
            }
          }
        }
      }
    });

    await Promise.all([
      createLineChart('chartAStock', '/holdings/history/A股', '#f85149', 'A股市值', 'daily'),
      createLineChart('chartUSStock', '/holdings/history/美股', '#58a6ff', '美股市值', 'daily'),
      createLineChart('chartCrypto', '/holdings/history/加密', '#bc8cff', '加密市值', 'daily'),
      createLineChart('chartPreciousMetal', '/holdings/history/贵金属', '#d29922', '贵金属市值', 'daily'),
      createLineChart('chartOther', '/holdings/history/其他', '#6e7681', '其他市值', 'weekly')
    ]);

  } catch (err) {
    document.getElementById('err').style.display='block';
    document.getElementById('err').textContent = '加载失败：'+String(err);
    console.error(err);
  }
}

main();
