import { jget } from './api.js';
import { setActiveNav } from './ui.js';
import { mount as mountSyncCenter } from './sync_center.js';
import { mount as mountPage, dispose as disposePage } from './features/data_sources/controller.js';
import { renderQuality as renderQualityInto } from './features/data_sources/view.js';

export { issuesHtml } from './features/data_sources/view.js';

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
