import { jget } from './api.js?v=20261031-gray-v1';
import { renderNavGrowthExplanationPanel } from './nav_growth_explanation.js?v=20261031-gray-v1';
import { setActiveNav } from './ui.js?v=20261031-ui-v1';
import { mount as mountPage, dispose as disposePage } from './features/more/controller.js?v=20261031-feature-v1';

export { issueSummary } from './features/more/controller.js?v=20261031-feature-v1';

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
