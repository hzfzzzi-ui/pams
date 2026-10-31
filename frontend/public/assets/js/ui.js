import { warmApiCache } from './api.js?v=20261031-gray-v1';
import { afterInvestmentStartup } from './investment_startup.js?v=20261031-gray-v1';
import { createPageBootstrap } from './app/page_bootstrap.js?v=20261031-ui-v1';
import { disposeBottomSheet } from './components/bottom_sheet.js?v=20261031-ui-v1';

export { openBottomSheet, closeBottomSheet } from './components/bottom_sheet.js?v=20261031-ui-v1';
export { dataStateHtml, renderDataState } from './components/data_state.js?v=20261031-ui-v1';

let bootstrap;

// Compatibility name for existing page controllers. Primary navigation belongs
// exclusively to app_shell.js; this entry only initializes page-local behavior.
export function setActiveNav() {
  if (!bootstrap || bootstrap.disposed) {
    bootstrap = createPageBootstrap({afterStartup: afterInvestmentStartup, warmCache: warmApiCache});
  }
  bootstrap.start();
}

export function disposePageUi() {
  bootstrap?.dispose();
  bootstrap = null;
  disposeBottomSheet();
}

export function kpi(label, value, deltaText, deltaDir) {
  const dirClass = deltaDir === 'up' ? 'up' : 'down';
  const delta = deltaText ? `<span class="${dirClass}">${deltaText}</span>` : '<span class="muted">--</span>';
  return `
    <div class="card kpi">
      <div class="label">${label}</div>
      <div class="value mono">${value}</div>
      <div class="delta">${delta}</div>
    </div>
  `;
}

export function renderAlerts(el, alerts) {
  el.innerHTML = (alerts || []).map((a) => {
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

export function renderAlertsStrip(el, alerts) {
  if (!alerts || alerts.length === 0) {
    el.innerHTML = '<div class="muted" style="padding:4px 0">暂无告警</div>';
    return;
  }
  el.innerHTML = alerts.map((a) => {
    const sev = a.type || a.severity || 'info';
    const tagClass = sev === 'risk' || sev === 'crit' ? 'risk' : (sev === 'warn' ? 'warn' : 'data');
    const tagText = sev === 'risk' || sev === 'crit' ? '风控' : (sev === 'warn' ? '关注' : '数据');
    const title = a.title || a.message || '';
    return `<div class="alertItem"><span class="tag ${tagClass}">${tagText}</span><span>${title}</span></div>`;
  }).join('');
}
