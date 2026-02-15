import { jget, fmt2 } from './api.js';
import { setActiveNav, renderAlerts } from './ui.js';

function pretty(obj){
  return JSON.stringify(obj, null, 2);
}

function card(label, value, hint){
  return `
    <div class="row">
      <div class="muted">${label}</div>
      <div style="text-align:right">
        <div class="mono" style="font-weight:800">${value}</div>
        ${hint?`<div class="muted">${hint}</div>`:''}
      </div>
    </div>
  `;
}

async function main(){
  setActiveNav();
  const ipsWrap = await jget('/policy/ips');
  const risk = await jget('/risk/latest');
  const dd = await jget('/nav/drawdown?limit=365');

  document.getElementById('ipsJson').textContent = pretty(ipsWrap);
  document.getElementById('ddJson').textContent = pretty(dd);

  const ips = ipsWrap?.value || {};
  const ipsCards = [
    card('持仓数量目标', `${ips.positions_target_min||10}–${ips.positions_target_max||15}`, '核心仓+卫星仓，总计10–15'),
    card('组合最大回撤', `${Math.round((ips.portfolio_max_drawdown||-0.25)*100)}%`, '触发后生成降风险草案'),
    card('行业上限', `${Math.round((ips.sector_cap||0.4)*100)}%`, '目前行业先用type代理，后续补行业映射'),
    card('再平衡带宽', `${Math.round((ips.rebalance_band||0.1)*100)}%`, '偏离触发再平衡草案'),
    card('提醒模式', `${ips.alert_mode||'A2'} / ${ips.screenshot_ingest||'B1'}`, '重大新闻提醒 + 截图实时入库'),
    card('交易执行', `${ips.trade_execution||'shareholder_confirm_required'}`, '任何真实交易必须股东确认'),
  ].join('');
  document.getElementById('ipsCards').innerHTML = ipsCards;

  // drawdown chart
  const rows = dd.rows || [];
  const labels = rows.map(r=>r.as_of);
  const drawdowns = rows.map(r=>Number(r.drawdown)*100);
  const navs = rows.map(r=>Number(r.total_market_value));

  const anyIncomplete = rows.some(r=>r.completeness && r.completeness !== 'ok');
  document.getElementById('ddNote').textContent = anyIncomplete
    ? '提示：当前数据覆盖不完整（completeness!=ok），回撤曲线可能失真。'
    : '数据覆盖正常。';

  const ctx = document.getElementById('ddChart');
  if (window.__ddChart) window.__ddChart.destroy();
  window.__ddChart = new Chart(ctx, {
    data: {
      labels,
      datasets: [
        { type:'line', label:'总市值', data: navs, yAxisID:'y', borderColor:'#b33b3b', backgroundColor:'rgba(179,59,59,.15)', tension:.25 },
        { type:'line', label:'回撤(%)', data: drawdowns, yAxisID:'y1', borderColor:'#c43e3e', backgroundColor:'rgba(196,62,62,.12)', tension:.25 },
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: { legend: { labels: { color: '#e8e4dc' } } },
      scales: {
        x: { ticks: { color: '#8a8578' }, grid: { color: 'rgba(42,37,32,.45)' } },
        y: { position:'left', ticks: { color: '#8a8578' }, grid: { color: 'rgba(42,37,32,.45)' } },
        y1: { position:'right', ticks: { color: '#8a8578', callback: (v)=>v+'%' }, grid: { drawOnChartArea: false } },
      }
    }
  });

  // risk events
  const alerts = (risk.events||[]).map(e=>({
    type: e.severity==='crit'?'risk':(e.severity==='warn'?'warn':'data'),
    title: `${e.severity.toUpperCase()} · ${e.title}`,
    body: `${e.code} · ${JSON.stringify(e.details)}`
  }));
  renderAlerts(document.getElementById('alerts'), alerts);
}

main().catch(err=>{
  document.getElementById('err').style.display='block';
  document.getElementById('err').textContent = '加载失败：'+String(err);
});
