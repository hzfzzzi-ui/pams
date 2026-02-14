import { jget, fmt2, typeLabel, venueLabel, accountLabel } from './api.js';
import { kpi, renderAlerts, setActiveNav } from './ui.js';

function safeNum(x){
  const n = Number(x);
  return Number.isNaN(n) ? 0 : n;
}

async function main(){
  setActiveNav();
  const asOfDate = new URLSearchParams(location.search).get('asOfDate');

  // Fetch from FastAPI endpoints
  const summary = await jget(`/summary2/latest?baseCurrency=CNY`);
  const holdingsResp = await jget(`/holdings/latest`);

  const asOf = summary.as_of || '--';
  document.getElementById('asof').textContent = `快照：${asOf}（${summary.base_currency||'CNY'}）`;
  document.getElementById('api').textContent = `接口：FastAPI (${location.hostname})`;

  // Calculate totals from summary
  const byClass = summary.by_asset_class || [];
  let total = 0;
  let liab = 0;

  for (const cls of byClass) {
    const mv = safeNum(cls.market_value);
    if (cls.asset_class === 'LIABILITY') {
      liab += Math.abs(mv);  // Convert to positive for display
    } else if (cls.asset_class !== 'IGNORE') {
      total += mv;
    }
  }

  const net = total - liab;
  const hpnl = safeNum(summary.totals?.holding_pnl);
  const dpnl = safeNum(summary.totals?.daily_pnl);

  // nav delta / mdd require time-series; placeholder for now.
  const navDelta = dpnl !== 0 ? fmt2(dpnl) : '';
  const mddText = '—';

  const positions = holdingsResp.rows || [];

  const kpis = [
    kpi('总资产', `¥ ${fmt2(total)}`, navDelta, 'up'),
    kpi('总负债', `¥ ${fmt2(liab)}`, '', 'down'),
    kpi('净资产', `¥ ${fmt2(net)}`, '', safeNum(net)>=0?'up':'down'),
    kpi('最大回撤(MDD)', mddText, '待接入NAV时间序列', 'down'),
    kpi('条目数', String(positions.length), '', 'up'),
  ];
  document.getElementById('kpis').innerHTML = kpis.join('');

  // Asset structure by asset_class from summary
  const byTypeEl = document.getElementById('byType');

  const assetClasses = byClass.filter(c => c.asset_class !== 'LIABILITY' && c.asset_class !== 'IGNORE');
  const byBig = new Map();
  const byBigSmall = new Map();

  // Group by asset_class
  for (const cls of assetClasses) {
    const big = cls.asset_class || '其他';
    const mv = safeNum(cls.market_value);
    byBig.set(big, (byBig.get(big)||0) + mv);

    // Group by venue under each asset class
    if (cls.by_venue && cls.by_venue.length > 0) {
      for (const venue of cls.by_venue) {
        const venueName = venue.venue || 'Unknown';
        const venueMv = safeNum(venue.market_value);
        byBigSmall.set(`${big} > ${venueName}`, venueMv);
      }
    }
  }

  const bigRows = [...byBig.entries()].map(([k,v])=>({k,v})).sort((a,b)=>b.v-a.v);
  const smallRows = [...byBigSmall.entries()].map(([k,v])=>({k,v})).sort((a,b)=>b.v-a.v);

  // breakdown list: big categories, plus top small categories under each big (top 2)
  const smallByBig = new Map();
  for (const r of smallRows) {
    const [big] = r.k.split(' > ');
    if (!smallByBig.has(big)) smallByBig.set(big, []);
    smallByBig.get(big).push(r);
  }

  // Map asset class codes to readable names
  const assetClassNames = {
    'CN_EQUITY': 'A股权益',
    'US_EQUITY': '美股权益',
    'FIXED_INCOME': '固收',
    'CRYPTO': '加密',
    'CASH': '现金',
    'ESOP': '期权',
    'GOLD_COMMODITIES': '黄金商品'
  };

  byTypeEl.innerHTML = bigRows.map(r=>{
    const displayName = assetClassNames[r.k] || r.k;
    const subs = (smallByBig.get(r.k)||[]).slice(0,3).map(s=>`<div class="muted" style="margin-top:4px">${s.k.split(' > ')[1]}：¥ ${fmt2(s.v)}</div>`).join('');
    return `<div class="row" style="flex-direction:column;align-items:stretch">
      <div style="display:flex;justify-content:space-between;gap:10px"><div><b>${displayName}</b></div><div class="mono">¥ ${fmt2(r.v)}</div></div>
      ${subs}
    </div>`;
  }).join('') || `<div class="muted">暂无数据</div>`;

  // asset pie (big categories)
  const pieEl = document.getElementById('pie');
  if (pieEl && bigRows.length) {
    const labels = bigRows.map(x=>assetClassNames[x.k] || x.k);
    const data = bigRows.map(x=>Number(x.v));
    if (window.__pie) window.__pie.destroy();
    window.__pie = new Chart(pieEl, {
      type: 'doughnut',
      data: { labels, datasets: [{ data, backgroundColor: ['#6aa6ff','#43d19e','#ffd166','#F18F01','#9b7bff','#55d6be','#93a4bd','#2E86AB'], borderColor:'rgba(36,48,85,.7)', borderWidth:1 }] },
      options: { responsive:true, maintainAspectRatio:false, animation:false, plugins:{ legend:{ position:'bottom', labels:{ color:'#cfe0ff' } }, tooltip:{ callbacks:{ label:(ctx)=>`${ctx.label}: ¥ ${fmt2(ctx.raw)}` } } } }
    });
  }

  // liability pie (from children array), grouped by account name
  const liabEl = document.getElementById('liabilityPie');
  if (liabEl) {
    const children = summary.children || [];
    const liabilities = children.filter(c => c.asset_class === 'LIABILITY');

    if (liabilities.length > 0) {
      // Group by account name
      const liabMap = new Map();
      for (const item of liabilities) {
        const name = item.name || item.account || 'Unknown';
        const value = Math.abs(safeNum(item.market_value));
        liabMap.set(name, (liabMap.get(name) || 0) + value);
      }

      const entries = [...liabMap.entries()].sort((a,b)=>b[1]-a[1]);
      const labels = entries.map(e=>e[0]);
      const data = entries.map(e=>e[1]);

      if (window.__liab) window.__liab.destroy();
      window.__liab = new Chart(liabEl, {
        type: 'doughnut',
        data: { labels, datasets: [{ data, backgroundColor: ['#ff6b6b','#ffb86b','#ffd56a','#c792ea','#82aaff','#7fdbca','#93a4bd'], borderColor:'rgba(36,48,85,.7)', borderWidth:1 }] },
        options: { responsive:true, maintainAspectRatio:false, animation:false, plugins:{ legend:{ position:'bottom', labels:{ color:'#cfe0ff' } }, tooltip:{ callbacks:{ label:(ctx)=>`${ctx.label}: ¥ ${fmt2(ctx.raw)}` } } } }
      });
    }
  }

  // NAV / net-worth curve
  const navEl = document.getElementById('navChart');
  if (navEl) {
    let series = [];
    try {
      const wrap = await jget('/nav/series?limit=90&baseCurrency=CNY');
      series = wrap.rows || [];
    } catch(e) {
      series = [{ asOfDate: o.asOfDate, netWorth: safeNum(o.netWorth) }];
    }

    const labels = series.map(x=>x.asOfDate);
    const data = series.map(x=>safeNum(x.netWorth));

    if (window.__nav) window.__nav.destroy();
    window.__nav = new Chart(navEl, {
      type: 'line',
      data: { labels, datasets: [{ label:'净资产（CNY）', data, borderColor:'#6aa6ff', backgroundColor:'rgba(106,166,255,.15)', tension:0.25, fill:true, pointRadius: labels.length>1?2:0 }] },
      options: { responsive:true, maintainAspectRatio:false, animation:false, plugins:{ legend:{display:false}, tooltip:{callbacks:{label:(ctx)=>`¥ ${fmt2(ctx.raw)}`}} }, scales:{ x:{ticks:{color:'#cfe0ff'},grid:{color:'rgba(36,48,85,.4)'}}, y:{ticks:{color:'#cfe0ff'},grid:{color:'rgba(36,48,85,.4)'}} } }
    });
  }

  // Cashflow mini chart
  const cfEl = document.getElementById('cashflowMini');
  if (cfEl) {
    let rows = [];
    try {
      const wrap = await jget('/cashflow/series?limit=30&currency=CNY');
      rows = wrap.rows || [];
    } catch(e) {
      rows = [];
    }

    const labels = rows.map(r=>r.date);
    const net = rows.map(r=>safeNum(r.net));

    if (window.__cfMini) window.__cfMini.destroy();
    window.__cfMini = new Chart(cfEl, {
      type: 'bar',
      data: { labels, datasets: [{ label:'净额', data: net, backgroundColor:'rgba(106,166,255,.25)', borderColor:'#6aa6ff', borderWidth:1, borderRadius:6 }] },
      options: { responsive:true, maintainAspectRatio:false, animation:false, plugins:{ legend:{display:false}, tooltip:{callbacks:{label:(ctx)=>`¥ ${fmt2(ctx.raw)}`}} }, scales:{ x:{ticks:{color:'#8ea0c7',maxRotation:0,autoSkip:true},grid:{color:'rgba(36,48,85,.25)'}}, y:{ticks:{color:'#8ea0c7'},grid:{color:'rgba(36,48,85,.25)'}} } }
    });
  }

  // alerts/status placeholders
  try {
    const riskResp = await jget('/risk/latest?limit=10');
    const alerts = (riskResp.rows || []).map(r => ({
      severity: r.severity,
      message: r.title || r.message,
      data: r.details
    }));
    renderAlerts(document.getElementById('alerts'), alerts);
  } catch(e) {
    renderAlerts(document.getElementById('alerts'), []);
  }

  document.getElementById('status').innerHTML = `<div class="muted">状态：FastAPI 后端（CNY 口径 + 自动汇率）。数据截至 ${asOf}</div>`;
}

main().catch(err=>{
  setActiveNav();
  document.getElementById('err').style.display='block';
  document.getElementById('err').textContent = '加载失败：'+String(err);
});
