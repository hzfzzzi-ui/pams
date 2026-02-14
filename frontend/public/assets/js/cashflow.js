import { jget, fmt2 } from './api.js';
import { setActiveNav } from './ui.js';

function safeNum(x){
  const n = Number(x);
  return Number.isNaN(n) ? 0 : n;
}

async function main(){
  setActiveNav();
  const wrap = await jget('/cashflow/series?limit=90&currency=CNY');
  const rows = wrap.rows || [];

  const labels = rows.map(r=>r.date);
  const income = rows.map(r=>safeNum(r.income));
  const expense = rows.map(r=>safeNum(r.expense));
  const net = rows.map(r=>safeNum(r.net));

  const el = document.getElementById('cfChart');
  if (window.__cf) window.__cf.destroy();
  window.__cf = new Chart(el, {
    data: {
      labels,
      datasets: [
        { type:'bar', label:'收入', data: income, backgroundColor:'rgba(67,209,158,.35)', borderColor:'#43d19e', borderWidth:1 },
        { type:'bar', label:'支出', data: expense.map(x=>-Math.abs(x)), backgroundColor:'rgba(255,107,107,.28)', borderColor:'#ff6b6b', borderWidth:1 },
        { type:'line', label:'净额', data: net, borderColor:'#6aa6ff', backgroundColor:'rgba(106,166,255,.15)', tension:.25, yAxisID:'y' },
      ]
    },
    options: {
      responsive:true,
      maintainAspectRatio:false,
      animation:false,
      plugins:{ legend:{ labels:{ color:'#cfe0ff' } }, tooltip:{ callbacks:{ label:(ctx)=>`¥ ${fmt2(ctx.raw)}` } } },
      scales:{
        x:{ ticks:{ color:'#8ea0c7', maxRotation:0, autoSkip:true }, grid:{ color:'rgba(36,48,85,.35)' } },
        y:{ ticks:{ color:'#8ea0c7', callback:(v)=>fmt2(v) }, grid:{ color:'rgba(36,48,85,.35)' } }
      }
    }
  });

  const note = document.getElementById('note');
  if (!rows.length) note.textContent = '暂无收支数据（等待 Firefly 对接流水入库）。';
  else note.textContent = `区间：${rows[0].date} ~ ${rows[rows.length-1].date}`;
}

main().catch(err=>{
  document.getElementById('err').style.display='block';
  document.getElementById('err').textContent = '加载失败：'+String(err);
});
