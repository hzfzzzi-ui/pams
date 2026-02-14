import { jget, fmt2, typeLabel, venueLabel, accountLabel } from './api.js';
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
      <td class="nowrap">${venueLabel(r.venue)}</td>
      <td class="nowrap">${accountLabel(r.account)}</td>
      <td class="nowrap">${typeLabel(r.type)||''}</td>
      <td class="mono nowrap">${r.symbol||''}</td>
      <td class="name">${r.name||''}</td>
      <td class="nowrap">${r.currency||''}</td>
      <td class="mono num">${fmt2(r.market_value)}</td>
      <td class="mono num" style="color:${safeNum(r.holding_pnl)>=0?'var(--good)':'var(--bad)'}">${fmt2(r.holding_pnl)}</td>
      <td class="mono num" style="color:${safeNum(r.daily_pnl)>=0?'var(--good)':'var(--bad)'}">${fmt2(r.daily_pnl)}</td>
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
