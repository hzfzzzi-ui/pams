import { jget, fmt2, typeLabel, accountLabel } from './api.js';
import { setActiveNav } from './ui.js';
import { pnlPct } from './utils.js';

function renderRows(rows){
  const tbody = document.getElementById('rows');
  tbody.innerHTML='';
  for(const r of rows){
    const pct = pnlPct(r);
    const tr=document.createElement('tr');
    tr.innerHTML = `
      <td class="nowrap">${r.symbol||'—'}</td>
      <td class="name">${r.name||''}</td>
      <td class="mono num">${r.buy_price ? fmt2(r.buy_price) : '—'}</td>
      <td class="mono num">${r.quantity ? fmt2(r.quantity) : '—'}</td>
      <td class="mono num">${r.current_price ? fmt2(r.current_price) : '—'}</td>
      <td class="mono num">${fmt2(r.market_value)}</td>
      <td class="mono num" style="color:${pct>=0?'var(--good)':'var(--bad)'}">${pct>=0?'+':''}${pct.toFixed(2)}%</td>
    `;
    tbody.appendChild(tr);
  }
}

async function main(){
  setActiveNav();
  const holdings = await jget('/holdings/latest');
  document.getElementById('asof').textContent = holdings.as_of || '--';

  const allRows = holdings.rows || [];

  // populate type filter
  const types = Array.from(new Set(allRows.map(r=>r.type).filter(Boolean))).sort();
  const sel = document.getElementById('typeFilter');
  if (sel){
    for (const t of types){
      const opt=document.createElement('option');
      opt.value=t;
      opt.textContent=typeLabel(t) || t;
      sel.appendChild(opt);
    }
    const apply = ()=>{
      const v = sel.value;
      const rows = v ? allRows.filter(r=>r.type===v) : allRows;
      renderRows(rows);
    };
    sel.addEventListener('change', apply);
    apply();
  } else {
    renderRows(allRows);
  }
}

main().catch(err=>{
  document.getElementById('err').style.display='block';
  document.getElementById('err').textContent = '加载失败：'+String(err);
});
