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
    const sev = a.type || a.severity || 'info';
    const tagClass = sev === 'risk' || sev === 'crit' ? 'risk' : (sev === 'warn' ? 'warn' : 'data');
    const tagText = sev === 'risk' || sev === 'crit' ? '风控' : (sev === 'warn' ? '关注' : '数据');
    const title = a.title || a.message || '';
    const body = a.body || a.details || a.data || '';
    return `
      <div class="alert" style="margin-bottom:10px">
        <span class="tag ${tagClass}">${tagText}</span>
        <div style="font-weight:800;margin:6px 0 4px 0;font-size:13px">${title}</div>
        <div class="muted">${body}</div>
      </div>
    `;
  }).join('') || '<div class="muted">暂无</div>';
}

export function renderAlertsStrip(el, alerts){
  if (!alerts || alerts.length === 0) {
    el.innerHTML = '<div class="muted" style="padding:4px 0">暂无告警</div>';
    return;
  }
  el.innerHTML = alerts.map(a=>{
    const sev = a.type || a.severity || 'info';
    const tagClass = sev === 'risk' || sev === 'crit' ? 'risk' : (sev === 'warn' ? 'warn' : 'data');
    const tagText = sev === 'risk' || sev === 'crit' ? '风控' : (sev === 'warn' ? '关注' : '数据');
    const title = a.title || a.message || '';
    return `<div class="alertItem"><span class="tag ${tagClass}">${tagText}</span><span>${title}</span></div>`;
  }).join('');
}
