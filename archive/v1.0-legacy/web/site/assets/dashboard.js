import { jget, fmt2, typeLabel, venueLabel, accountLabel } from './api.js';
import { kpi, renderAlerts, setActiveNav } from './ui.js';

function safeNum(x){
  const n = Number(x);
  return Number.isNaN(n) ? 0 : n;
}

async function main(){
  setActiveNav();
  const asOfDate = new URLSearchParams(location.search).get('asOfDate');
  const o = await jget(`/overview${asOfDate?`?asOfDate=${encodeURIComponent(asOfDate)}&baseCurrency=CNY`:'?baseCurrency=CNY'}`);

  const asOf = o.asOfDate || '--';
  document.getElementById('asof').textContent = `快照：${asOf}（${o.baseCurrency||'CNY'}）`;
  document.getElementById('api').textContent = `接口：${location.hostname}:18888`;

  const total = o.totalAssets ?? 0;
  const liab = o.totalLiabilities ?? 0;
  const net = o.netWorth ?? (Number(total)+Number(liab));
  const hpnl = 0;
  const dpnl = 0;

  // nav delta / mdd require time-series; placeholder for now.
  const navDelta = '';
  const mddText = '—';

  const kpis = [
    kpi('总资产', `¥ ${fmt2(total)}`, navDelta, 'up'),
    kpi('总负债', `¥ ${fmt2(liab)}`, '', 'down'),
    kpi('净资产', `¥ ${fmt2(net)}`, '', safeNum(net)>=0?'up':'down'),
    kpi('最大回撤(MDD)', mddText, '待接入NAV时间序列', 'down'),
    kpi('条目数', String((o.positions||[]).length), '', 'up'),
  ];
  document.getElementById('kpis').innerHTML = kpis.join('');

  // Asset structure by bottom-table categories (大类/小类) from notes.
  const byTypeEl = document.getElementById('byType');

  const assets = (o.positions || []).filter(p => p.accountKind !== 'liability');
  const byBig = new Map();
  const byBigSmall = new Map();

  for (const p of assets) {
    let note = {};
    try { note = p.notes ? JSON.parse(p.notes) : {}; } catch(e) {}
    const big = note.big || note.raw?.['大类'] || '其他';
    const small = note.small || note.raw?.['小类'] || '';
    const mv = safeNum(p.marketValueBase);
    byBig.set(big, (byBig.get(big)||0) + mv);
    if (small) byBigSmall.set(`${big} > ${small}`, (byBigSmall.get(`${big} > ${small}`)||0) + mv);
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

  byTypeEl.innerHTML = bigRows.map(r=>{
    const subs = (smallByBig.get(r.k)||[]).slice(0,2).map(s=>`<div class="muted" style="margin-top:4px">${s.k.split(' > ')[1]}：¥ ${fmt2(s.v)}</div>`).join('');
    return `<div class="row" style="flex-direction:column;align-items:stretch">
      <div style="display:flex;justify-content:space-between;gap:10px"><div><b>${r.k}</b></div><div class="mono">¥ ${fmt2(r.v)}</div></div>
      ${subs}
    </div>`;
  }).join('') || `<div class="muted">暂无数据</div>`;

  // asset pie (big categories)
  const pieEl = document.getElementById('pie');
  if (pieEl && bigRows.length) {
    const labels = bigRows.map(x=>x.k);
    const data = bigRows.map(x=>Number(x.v));
    if (window.__pie) window.__pie.destroy();
    window.__pie = new Chart(pieEl, {
      type: 'doughnut',
      data: { labels, datasets: [{ data, backgroundColor: ['#6aa6ff','#43d19e','#ffd166','#F18F01','#9b7bff','#55d6be','#93a4bd','#2E86AB'], borderColor:'rgba(36,48,85,.7)', borderWidth:1 }] },
      options: { responsive:true, maintainAspectRatio:false, animation:false, plugins:{ legend:{ position:'bottom', labels:{ color:'#cfe0ff' } }, tooltip:{ callbacks:{ label:(ctx)=>`${ctx.label}: ¥ ${fmt2(ctx.raw)}` } } } }
    });
  }

  // liability pie (from positions), grouped by liability accountName
  const liabEl = document.getElementById('liabilityPie');
  if (liabEl) {
    const m = new Map();
    for (const p of o.positions || []) {
      if (p.accountKind !== 'liability') continue;
      const k = p.accountName || p.accountId;
      const v = Math.abs(safeNum(p.marketValueBase));
      m.set(k, (m.get(k)||0) + v);
    }
    const entries = [...m.entries()].sort((a,b)=>b[1]-a[1]);
    const labels = entries.map(e=>e[0]);
    const data = entries.map(e=>e[1]);
    if (window.__liab) window.__liab.destroy();
    window.__liab = new Chart(liabEl, {
      type: 'doughnut',
      data: { labels, datasets: [{ data, backgroundColor: ['#ff6b6b','#ffb86b','#ffd56a','#c792ea','#82aaff','#7fdbca','#93a4bd'], borderColor:'rgba(36,48,85,.7)', borderWidth:1 }] },
      options: { responsive:true, maintainAspectRatio:false, animation:false, plugins:{ legend:{ position:'bottom', labels:{ color:'#cfe0ff' } }, tooltip:{ callbacks:{ label:(ctx)=>`${ctx.label}: ¥ ${fmt2(ctx.raw)}` } } } }
    });
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

  // alerts/status placeholders (node api not yet provides these endpoints)
  renderAlerts(document.getElementById('alerts'), []);
  document.getElementById('status').innerHTML = `<div class="muted">状态：已切到本机 18888 API（CNY 口径 + 自动汇率）。</div>`;
}

main().catch(err=>{
  setActiveNav();
  document.getElementById('err').style.display='block';
  document.getElementById('err').textContent = '加载失败：'+String(err);
});
