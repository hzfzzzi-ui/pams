import { jget, fmt2, accountLabel } from './api.js';
import { kpi, renderAlertsStrip, setActiveNav } from './ui.js';

function safeNum(x){
  const n = Number(x);
  return Number.isNaN(n) ? 0 : n;
}

// FX state: store rates for all 3 base currencies
let fxRatesByBase = {}; // { CNY: [...rates], USD: [...rates], MXN: [...rates] }
let fxReversed = false; // false = X/CNY, true = CNY/X

async function main(){
  setActiveNav();

  // Parallel fetch all data
  const [summary, navResp, ddResp, fxCNY, fxUSD, fxMXN, txResp, riskResp,
         aStockHistory, usStockHistory, cryptoHistory] = await Promise.all([
    jget('/summary2/latest?baseCurrency=CNY'),
    jget('/nav/series?limit=90&baseCurrency=CNY').catch(() => ({ rows: [] })),
    jget('/nav/drawdown?limit=365').catch(() => ({ max_drawdown: null, rows: [] })),
    jget('/api/fx-rates/latest?base_currency=CNY').catch(() => ({ rates: [] })),
    jget('/api/fx-rates/latest?base_currency=USD').catch(() => ({ rates: [] })),
    jget('/api/fx-rates/latest?base_currency=MXN').catch(() => ({ rates: [] })),
    jget('/api/transactions/recent?limit=15').catch(() => ({ transactions: [] })),
    jget('/risk/latest?limit=10').catch(() => ({ rows: [] })),
    jget('/holdings/history/A股?days=90&interval=daily').catch(() => ({ series: [] })),
    jget('/holdings/history/美股?days=90&interval=daily').catch(() => ({ series: [] })),
    jget('/holdings/history/加密?days=90&interval=daily').catch(() => ({ series: [] })),
  ]);

  const asOf = summary.as_of || '--';
  document.getElementById('asof').textContent = `${asOf}（${summary.base_currency || 'CNY'}）`;
  document.getElementById('api').textContent = `FastAPI (${location.hostname})`;

  // ==================== Row 1: KPIs (3) + FX Rates ====================
  const byClass = summary.by_asset_class || [];
  let total = 0;
  let liab = 0;

  for (const cls of byClass) {
    const mv = safeNum(cls.market_value);
    if (cls.asset_class === 'LIABILITY') {
      liab += Math.abs(mv);
    } else if (cls.asset_class !== 'IGNORE') {
      total += mv;
    }
  }

  const net = total - liab;
  const hpnl = safeNum(summary.totals?.holding_pnl);
  const navDelta = hpnl !== 0 ? (hpnl > 0 ? '+' : '') + fmt2(hpnl) : '';

  const kpis = [
    kpi('总资产', `¥ ${fmt2(total)}`, navDelta, hpnl >= 0 ? 'up' : 'down'),
    kpi('总负债', `¥ ${fmt2(liab)}`, '', 'down'),
    kpi('净资产', `¥ ${fmt2(net)}`, '', net >= 0 ? 'up' : 'down'),
  ];
  document.getElementById('kpis').innerHTML = kpis.join('');

  // FX Rates with toggle (real bidirectional rates from public sources)
  fxRatesByBase = {
    CNY: fxCNY.rates || [],
    USD: fxUSD.rates || [],
    MXN: fxMXN.rates || [],
  };
  renderFxRates(fxCNY.as_of);
  document.getElementById('fxToggleBtn').addEventListener('click', () => {
    fxReversed = !fxReversed;
    renderFxRates(fxCNY.as_of);
  });

  // ==================== Row 2: Alerts strip ====================
  const alerts = (riskResp.rows || []).map(r => ({
    severity: r.severity,
    title: r.title || r.message || r.code,
    details: r.details,
  }));
  renderAlertsStrip(document.getElementById('alerts'), alerts);

  // ==================== Row 3: Asset + Liability ====================
  renderAssetStructure(byClass, summary.children || []);
  renderLiabilityStructure(byClass, summary.children || []);

  // ==================== Row 4: NAV + Sub-charts + Holdings PnL ====================
  renderNavChart(navResp);
  renderMDD(ddResp);
  renderCategoryNavChart(aStockHistory, usStockHistory, cryptoHistory);
  renderHoldingsPnl(byClass);

  // ==================== Row 5: Recent Cashflow ====================
  await renderCashflowSection(txResp);
}

// ==================== FX Rates ====================
function renderFxRates(asOfDate) {
  const fxBody = document.getElementById('fxBody');
  const targetCurrencies = ['USD', 'MXN'];
  const rows = [];

  if (!fxReversed) {
    // Default: X→CNY (e.g., 1 USD = 6.91 CNY)
    // Use rates from base_currency=CNY where quote_currency matches
    const cnyRates = fxRatesByBase.CNY || [];
    for (const currency of targetCurrencies) {
      const rateObj = cnyRates.find(r => r.quote_currency === currency);
      if (rateObj) {
        const rate = safeNum(rateObj.rate);
        rows.push(`<div class="fxRow"><span class="fxPair">${currency}/CNY</span><span class="fxRate">${rate.toFixed(4)}</span></div>`);
      }
    }
  } else {
    // Reversed: CNY→X (e.g., 1 CNY = 0.1446 USD)
    // Use real rates from base_currency=X where quote_currency=CNY
    for (const currency of targetCurrencies) {
      const baseRates = fxRatesByBase[currency] || [];
      const rateObj = baseRates.find(r => r.quote_currency === 'CNY');
      if (rateObj) {
        const rate = safeNum(rateObj.rate);
        rows.push(`<div class="fxRow"><span class="fxPair">CNY/${currency}</span><span class="fxRate">${rate.toFixed(4)}</span></div>`);
      }
    }
  }

  if (asOfDate) {
    rows.push(`<div class="muted" style="margin-top:4px;font-size:10px">${asOfDate}</div>`);
  }

  fxBody.innerHTML = rows.length > 0 ? rows.join('') : '<div class="muted">暂无汇率数据</div>';
}

// ==================== Asset Structure ====================
function renderAssetStructure(byClass, children) {
  const byTypeEl = document.getElementById('byType');
  const assetClasses = byClass.filter(c => c.asset_class !== 'LIABILITY' && c.asset_class !== 'IGNORE');

  const byBig = new Map();
  const byBigSmall = new Map();

  for (const cls of assetClasses) {
    const big = cls.asset_class || '其他';
    const mv = safeNum(cls.market_value);
    byBig.set(big, (byBig.get(big) || 0) + mv);

    if (cls.by_venue) {
      for (const venue of cls.by_venue) {
        const venueName = accountLabel(venue.venue) || venue.venue || 'Unknown';
        const venueMv = safeNum(venue.market_value);
        const key = `${big} > ${venueName}`;
        byBigSmall.set(key, (byBigSmall.get(key) || 0) + venueMv);
      }
    }
  }

  const bigRows = [...byBig.entries()].map(([k, v]) => ({ k, v })).sort((a, b) => b.v - a.v);

  const smallByBig = new Map();
  for (const [k, v] of byBigSmall.entries()) {
    const [big] = k.split(' > ');
    if (!smallByBig.has(big)) smallByBig.set(big, []);
    smallByBig.get(big).push({ k, v });
  }

  byTypeEl.innerHTML = bigRows.map(r => {
    const subs = (smallByBig.get(r.k) || [])
      .sort((a, b) => b.v - a.v)
      .slice(0, 3)
      .map(s => `<div class="muted" style="margin-top:4px">${s.k.split(' > ')[1]}：¥ ${fmt2(s.v)}</div>`)
      .join('');
    return `<div class="row" style="flex-direction:column;align-items:stretch">
      <div style="display:flex;justify-content:space-between;gap:10px"><div><b>${r.k}</b></div><div class="mono">¥ ${fmt2(r.v)}</div></div>
      ${subs}
    </div>`;
  }).join('') || '<div class="muted">暂无数据</div>';

  // Asset pie
  const pieEl = document.getElementById('pie');
  if (pieEl && bigRows.length) {
    const labels = bigRows.map(x => x.k);
    const data = bigRows.map(x => Number(x.v));
    if (window.__pie) window.__pie.destroy();
    window.__pie = new Chart(pieEl, {
      type: 'doughnut',
      data: { labels, datasets: [{ data, backgroundColor: ['#6aa6ff','#43d19e','#ffd166','#F18F01','#9b7bff','#55d6be','#93a4bd','#2E86AB'], borderColor: 'rgba(36,48,85,.7)', borderWidth: 1 }] },
      options: { responsive: true, maintainAspectRatio: false, animation: false, plugins: { legend: { position: 'bottom', labels: { color: '#cfe0ff', font: { size: 11 }, padding: 8 } }, tooltip: { callbacks: { label: ctx => `${ctx.label}: ¥ ${fmt2(ctx.raw)}` } } } }
    });
  }
}

// ==================== Liability Structure (with amounts) ====================
function renderLiabilityStructure(byClass, children) {
  const liabListEl = document.getElementById('liabilityList');
  const liabEl = document.getElementById('liabilityPie');

  const liabilities = children.filter(c => c.asset_class === 'LIABILITY');

  if (liabilities.length === 0) {
    liabListEl.innerHTML = '<div class="muted">暂无负债</div>';
    return;
  }

  const liabMap = new Map();
  for (const item of liabilities) {
    const name = item.name || accountLabel(item.account) || item.account || 'Unknown';
    const value = Math.abs(safeNum(item.market_value));
    liabMap.set(name, (liabMap.get(name) || 0) + value);
  }

  const entries = [...liabMap.entries()].sort((a, b) => b[1] - a[1]);
  const totalLiab = entries.reduce((s, e) => s + e[1], 0);

  liabListEl.innerHTML = entries.map(([name, value]) => {
    const pct = totalLiab > 0 ? ((value / totalLiab) * 100).toFixed(1) : '0';
    return `<div class="row" style="flex-direction:column;align-items:stretch">
      <div style="display:flex;justify-content:space-between;gap:10px">
        <div><b>${name}</b></div>
        <div class="mono" style="color:var(--bad)">¥ ${fmt2(value)}</div>
      </div>
      <div class="muted" style="margin-top:2px">占比 ${pct}%</div>
    </div>`;
  }).join('');

  if (liabEl && entries.length > 0) {
    const labels = entries.map(e => e[0]);
    const data = entries.map(e => e[1]);
    if (window.__liab) window.__liab.destroy();
    window.__liab = new Chart(liabEl, {
      type: 'doughnut',
      data: { labels, datasets: [{ data, backgroundColor: ['#ff6b6b','#ffb86b','#ffd56a','#c792ea','#82aaff','#7fdbca','#93a4bd'], borderColor: 'rgba(36,48,85,.7)', borderWidth: 1 }] },
      options: { responsive: true, maintainAspectRatio: false, animation: false, plugins: { legend: { position: 'bottom', labels: { color: '#cfe0ff', font: { size: 11 }, padding: 8 } }, tooltip: { callbacks: { label: ctx => `${ctx.label}: ¥ ${fmt2(ctx.raw)}` } } } }
    });
  }
}

// ==================== NAV Chart ====================
function renderNavChart(navResp) {
  const navEl = document.getElementById('navChart');
  if (!navEl) return;

  const series = navResp.rows || [];
  const labels = series.map(x => x.asOfDate);
  const data = series.map(x => safeNum(x.netWorth));

  if (window.__nav) window.__nav.destroy();
  window.__nav = new Chart(navEl, {
    type: 'line',
    data: {
      labels,
      datasets: [{
        label: '净资产（CNY）',
        data,
        borderColor: '#6aa6ff',
        backgroundColor: 'rgba(106,166,255,.1)',
        tension: 0.25,
        fill: true,
        pointRadius: labels.length > 30 ? 0 : 2,
        borderWidth: 2,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: ctx => `¥ ${fmt2(ctx.raw)}` } }
      },
      scales: {
        x: { ticks: { color: '#8ea0c7', maxRotation: 0, autoSkip: true, maxTicksLimit: 8 }, grid: { color: 'rgba(36,48,85,.3)' } },
        y: { ticks: { color: '#8ea0c7', callback: v => '¥' + fmt2(v) }, grid: { color: 'rgba(36,48,85,.3)' } }
      }
    }
  });
}

// ==================== Category NAV Chart (A股/美股/加密) ====================
function renderCategoryNavChart(aStock, usStock, crypto) {
  const el = document.getElementById('catNavChart');
  if (!el) return;

  // Merge all dates
  const allDates = new Set();
  for (const s of (aStock.series || [])) allDates.add(s.date);
  for (const s of (usStock.series || [])) allDates.add(s.date);
  for (const s of (crypto.series || [])) allDates.add(s.date);

  const labels = [...allDates].sort();
  const aMap = new Map((aStock.series || []).map(s => [s.date, s.value]));
  const uMap = new Map((usStock.series || []).map(s => [s.date, s.value]));
  const cMap = new Map((crypto.series || []).map(s => [s.date, s.value]));

  if (window.__catNav) window.__catNav.destroy();
  window.__catNav = new Chart(el, {
    type: 'line',
    data: {
      labels,
      datasets: [
        {
          label: 'A股',
          data: labels.map(d => aMap.get(d) ?? null),
          borderColor: '#36A2EB',
          backgroundColor: 'rgba(54,162,235,.08)',
          tension: 0.25, fill: false, borderWidth: 2,
          pointRadius: labels.length > 30 ? 0 : 2,
        },
        {
          label: '美股',
          data: labels.map(d => uMap.get(d) ?? null),
          borderColor: '#FF6384',
          backgroundColor: 'rgba(255,99,132,.08)',
          tension: 0.25, fill: false, borderWidth: 2,
          pointRadius: labels.length > 30 ? 0 : 2,
        },
        {
          label: '加密',
          data: labels.map(d => cMap.get(d) ?? null),
          borderColor: '#FFCE56',
          backgroundColor: 'rgba(255,206,86,.08)',
          tension: 0.25, fill: false, borderWidth: 2,
          pointRadius: labels.length > 30 ? 0 : 2,
        },
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      spanGaps: true,
      plugins: {
        legend: { position: 'top', labels: { color: '#cfe0ff', font: { size: 11 }, padding: 10, usePointStyle: true } },
        tooltip: { mode: 'index', intersect: false, callbacks: { label: ctx => `${ctx.dataset.label}: ¥ ${fmt2(ctx.raw)}` } }
      },
      scales: {
        x: { ticks: { color: '#8ea0c7', maxRotation: 0, autoSkip: true, maxTicksLimit: 8 }, grid: { color: 'rgba(36,48,85,.3)' } },
        y: { ticks: { color: '#8ea0c7', callback: v => '¥' + fmt2(v) }, grid: { color: 'rgba(36,48,85,.3)' } }
      }
    }
  });
}

// ==================== MDD Badge ====================
function renderMDD(ddResp) {
  const badge = document.getElementById('mddBadge');
  if (!badge) return;

  const mdd = ddResp.max_drawdown;
  if (mdd !== null && mdd !== undefined) {
    const pct = (safeNum(mdd) * 100).toFixed(2);
    badge.textContent = `MDD: ${pct}%`;
  } else {
    badge.textContent = 'MDD: --';
  }
}

// ==================== Holdings PnL Analysis ====================
function renderHoldingsPnl(byClass) {
  const el = document.getElementById('holdingsAnalysis');
  if (!el) return;

  const assetClasses = byClass.filter(c => c.asset_class !== 'LIABILITY' && c.asset_class !== 'IGNORE');

  // Total asset value for weight calculation
  let totalAsset = 0;
  let totalPnl = 0;
  for (const cls of assetClasses) {
    totalAsset += safeNum(cls.market_value);
    totalPnl += safeNum(cls.holding_pnl);
  }

  // Category weight sorted by value
  const categories = assetClasses
    .map(c => ({ name: c.asset_class, value: safeNum(c.market_value) }))
    .sort((a, b) => b.value - a.value);

  // Target categories for PnL display
  const targetCategories = ['A股', '美股', '加密', '贵金属'];

  let html = '<div class="analysisList">';

  // Category weight section
  html += '<div style="font-weight:700;font-size:12px;color:var(--muted);margin-bottom:4px">类别权重</div>';
  for (const cat of categories) {
    const pct = totalAsset > 0 ? ((cat.value / totalAsset) * 100).toFixed(1) : '0';
    html += `<div class="analysisRow">
      <span class="analysisLabel">${cat.name}</span>
      <span class="analysisValue">${pct}%</span>
    </div>`;
  }

  // Category PnL from by_asset_class (reliable aggregated data)
  html += '<div style="font-weight:700;font-size:12px;color:var(--muted);margin:12px 0 4px 0">分类持仓收益</div>';
  for (const catName of targetCategories) {
    const cls = assetClasses.find(c => c.asset_class === catName);
    const mv = cls ? safeNum(cls.market_value) : 0;
    const pnl = cls ? safeNum(cls.holding_pnl) : 0;
    const cost = mv - pnl;
    const pnlColor = pnl >= 0 ? 'var(--good)' : 'var(--bad)';
    const pnlSign = pnl >= 0 ? '+' : '';
    const pnlPct = cost > 0 ? ((pnl / cost) * 100).toFixed(2) : '0.00';
    html += `<div class="analysisRow" style="flex-wrap:wrap">
      <div>
        <div style="font-size:13px;font-weight:600">${catName}</div>
        <div class="analysisSub">市值 ¥${fmt2(mv)}</div>
      </div>
      <div style="text-align:right">
        <div class="analysisValue" style="color:${pnlColor}">${pnlSign}¥${fmt2(Math.abs(pnl))}</div>
        <div class="analysisSub" style="color:${pnlColor}">${pnlSign}${pnlPct}%</div>
      </div>
    </div>`;
  }

  // Total PnL
  html += '<div style="font-weight:700;font-size:12px;color:var(--muted);margin:12px 0 4px 0">加总收益</div>';
  const totalCost = totalAsset - totalPnl;
  const totalPnlColor = totalPnl >= 0 ? 'var(--good)' : 'var(--bad)';
  const totalPnlSign = totalPnl >= 0 ? '+' : '';
  const totalPnlPct = totalCost > 0 ? ((totalPnl / totalCost) * 100).toFixed(2) : '0.00';
  html += `<div class="analysisRow" style="flex-wrap:wrap;padding:8px 0;border-top:2px solid var(--line)">
    <div>
      <div style="font-size:14px;font-weight:700">总持仓收益</div>
      <div class="analysisSub">总市值 ¥${fmt2(totalAsset)}</div>
    </div>
    <div style="text-align:right">
      <div style="font-size:16px;font-weight:800;color:${totalPnlColor}">${totalPnlSign}¥${fmt2(Math.abs(totalPnl))}</div>
      <div class="analysisSub" style="color:${totalPnlColor}">${totalPnlSign}${totalPnlPct}%</div>
    </div>
  </div>`;

  html += '</div>';
  el.innerHTML = html;
}

// ==================== Cashflow Section ====================
async function renderCashflowSection(txResp) {
  const transactions = txResp.transactions || [];

  const now = new Date();
  const currentMonth = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;

  let dailyData = [];
  try {
    dailyData = await jget(`/api/transactions/daily/${currentMonth}`);
  } catch (e) {
    dailyData = [];
  }

  // Render expense-only line chart
  const cfEl = document.getElementById('cashflowMini');
  if (cfEl && dailyData.length > 0) {
    const labels = dailyData.map(d => `${d.day}日`);
    const expense = dailyData.map(d => safeNum(d.expense));

    if (window.__cfMini) window.__cfMini.destroy();
    window.__cfMini = new Chart(cfEl, {
      type: 'line',
      data: {
        labels,
        datasets: [{
          label: '支出',
          data: expense,
          borderColor: '#ff6b6b',
          backgroundColor: 'rgba(255,107,107,.1)',
          tension: 0.3,
          fill: true,
          pointRadius: labels.length > 20 ? 0 : 3,
          borderWidth: 2,
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: { label: ctx => `支出: ¥ ${fmt2(ctx.raw)}` } }
        },
        scales: {
          x: { ticks: { color: '#8ea0c7', maxRotation: 0, autoSkip: true }, grid: { color: 'rgba(36,48,85,.25)' } },
          y: { ticks: { color: '#8ea0c7', callback: v => '¥' + fmt2(v) }, grid: { color: 'rgba(36,48,85,.25)' } }
        }
      }
    });
  } else if (cfEl) {
    const ctx = cfEl.getContext('2d');
    ctx.clearRect(0, 0, cfEl.width, cfEl.height);
    ctx.fillStyle = '#8ea0c7';
    ctx.font = '13px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('本月暂无支出数据', cfEl.width / 2, cfEl.height / 2);
  }

  // Render recent transactions list (expense only)
  const txListEl = document.getElementById('recentTxList');
  if (txListEl) {
    const expenseTxs = transactions.filter(tx => safeNum(tx.amount) < 0);
    if (expenseTxs.length === 0) {
      txListEl.innerHTML = '<div class="muted">暂无支出记录</div>';
    } else {
      txListEl.innerHTML = expenseTxs.map(tx => {
        const amt = Math.abs(safeNum(tx.amount_cny != null ? tx.amount_cny : tx.amount));
        const desc = tx.description || tx.category || '--';
        const acct = tx.account_name || accountLabel(tx.account_code) || tx.account_code || '';
        const date = tx.date || '';
        return `<div class="txRow">
          <div>
            <div class="txDesc">${desc}</div>
            <div class="txMeta">${date} · ${acct}</div>
          </div>
          <div class="txAmt" style="color:var(--bad)">-¥${fmt2(amt)}</div>
        </div>`;
      }).join('');
    }
  }
}

main().catch(err => {
  setActiveNav();
  document.getElementById('err').style.display = 'block';
  document.getElementById('err').textContent = '加载失败：' + String(err);
  console.error(err);
});
