import { jget } from './api.js?v=20261031-gray-v1';
import { setActiveNav } from './ui.js?v=20261031-ui-v1';
import { mount as mountSyncCenter } from './sync_center.js?v=20261031-feature-v1';
import { mount as mountPage, dispose as disposePage } from './features/data_sources/controller.js?v=20261031-feature-v1';
import { renderQuality as renderQualityInto } from './features/data_sources/view.js?v=20261031-feature-v1';

export { issuesHtml } from './features/data_sources/view.js?v=20261031-feature-v1';

// Preserve the historical public renderer while controllers pass an explicit target.
export function renderQuality(portfolio, el = document.getElementById('dataQuality')) {
  return renderQualityInto(portfolio, el);
}

export function mount(root = document.body, context = {}) {
  return mountPage(root, {get: jget, mountSyncCenter, bootstrap: setActiveNav, ...context});
}

export function dispose(root = document.body) {
  disposePage(root);
}

if (typeof window !== 'undefined') {
  mount();
  window.addEventListener('pageshow', event => {
    if (event.persisted) mount();
  });
}
