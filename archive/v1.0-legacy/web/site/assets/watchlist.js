import { jget } from './api.js';
import { setActiveNav } from './ui.js';

async function main(){
  setActiveNav();
  const wl = await jget('/watchlist?limit=200');
  const el = document.getElementById('rows');
  el.innerHTML = (wl.rows||[]).map(r=>`
    <tr>
      <td>${r.market}</td><td class="mono">${r.symbol}</td><td>${r.name||''}</td>
      <td>${r.status}</td><td class="muted">${r.thesis||''}</td>
    </tr>
  `).join('') || '<tr><td colspan="5" class="muted">观察池为空（将由每周筛选器自动填充）</td></tr>';
}

main().catch(err=>{
  document.getElementById('err').style.display='block';
  document.getElementById('err').textContent = '加载失败：'+String(err);
});
