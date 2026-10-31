import { jget, jpost } from './api.js?v=20261031-modules-v1';
import { openBottomSheet, closeBottomSheet, setActiveNav } from './ui.js?v=20261031-modules-v1';
import { mount as mountPage, dispose as disposePage } from './features/transactions/controller.js?v=20261031-modules-v1';

let page;
let restoreState;
export function mount(root = document.body, context = {}) {
  const controller = mountPage(root, {get: jget, post: jpost, openSheet: openBottomSheet, closeSheet: closeBottomSheet, bootstrap: setActiveNav, ...context});
  if (typeof document !== 'undefined' && root === document.body) page = controller;
  return controller;
}
export function dispose(root = document.body) {
  disposePage(root);
  if (typeof document !== 'undefined' && root === document.body) restoreState = null;
}
if (typeof document !== 'undefined') {
  window.addEventListener('pagehide', event => {restoreState = event.persisted && page && !page.disposed ? page.captureUiState() : null;});
  window.addEventListener('pageshow', event => {
    if (event.persisted && restoreState) {
      const state = restoreState; restoreState = null;
      mount(document.body, {restoreState: state});
    }
  });
  mount();
}
