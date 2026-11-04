import { jget } from './api.js';
import { renderNavGrowthExplanationPanel } from './nav_growth_explanation.js';
import { setActiveNav } from './ui.js';
import { mount as mountPage, dispose as disposePage } from './features/more/controller.js';

export { issueSummary } from './features/more/controller.js';

export function mount(root = document.body, context = {}) {
  return mountPage(root, {
    get: jget,
    renderExplanation: renderNavGrowthExplanationPanel,
    bootstrap: setActiveNav,
    ...context,
  });
}

export function dispose(root = document.body) {
  disposePage(root);
}

if (typeof document !== 'undefined') {
  mount();
  window.addEventListener('pageshow', event => {
    if (event.persisted) mount();
  });
}
