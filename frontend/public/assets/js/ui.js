import { warmApiCache } from './api.js';
import { afterInvestmentStartup } from './investment_startup.js';
import { createPageBootstrap } from './app/page_bootstrap.js';

export { openBottomSheet, closeBottomSheet } from './components/bottom_sheet.js';
export { dataStateHtml, renderDataState } from './components/data_state.js';

let bootstrap;

// Compatibility name for existing page controllers. Primary navigation belongs
// exclusively to app_shell.js; this entry only initializes page-local behavior.
export function setActiveNav() {
  if (!bootstrap || bootstrap.disposed) {
    bootstrap = createPageBootstrap({afterStartup: afterInvestmentStartup, warmCache: warmApiCache});
  }
  bootstrap.start();
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
