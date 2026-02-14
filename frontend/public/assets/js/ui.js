export function setActiveNav(){
  const path = (location.pathname || '/').split('/').pop() || 'index.html';
  document.querySelectorAll('[data-nav]')?.forEach(a=>{
    const href = a.getAttribute('href') || '';
    const key = href.split('/').pop();
    if (key === path) a.classList.add('active');
  });
}

export function kpi(label, value, deltaText, deltaDir){
  const dirClass = deltaDir === 'up' ? 'up' : 'down';
  const delta = deltaText ? `<span class="${dirClass}">${deltaText}</span>` : '<span class="muted">—</span>';
  return `
    <div class="card kpi">
      <div class="label">${label}</div>
      <div class="value mono">${value}</div>
      <div class="delta">${delta}</div>
    </div>
  `;
}

export function renderAlerts(el, alerts){
  el.innerHTML = (alerts||[]).map(a=>{
    const tagClass = a.type === 'risk' ? 'risk' : (a.type==='warn'?'warn':'data');
    const tagText = a.type === 'risk' ? '风控' : (a.type==='warn'?'关注':'数据');
    return `
      <div class="alert" style="margin-bottom:10px">
        <span class="tag ${tagClass}">${tagText}</span>
        <div style="font-weight:800;margin:6px 0 4px 0;font-size:13px">${a.title}</div>
        <div class="muted">${a.body}</div>
      </div>
    `;
  }).join('') || '<div class="muted">暂无</div>';
}
