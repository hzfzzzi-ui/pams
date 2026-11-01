import { jget } from './api.js?v=20261031-modules-v1';
import { openBottomSheet, closeBottomSheet, setActiveNav } from './ui.js?v=20261031-modules-v1';
import { mount as mountPage, dispose as disposePage } from './features/investment/holdings/list_controller.js?v=20261102-verified-trade-snapshot-v1';

export function mount(root = document.body, context = {}) {
  return mountPage(root, {get: jget, showDetails: openBottomSheet, closeDetails: closeBottomSheet,
    bootstrap: setActiveNav,  ...context});
}
export function dispose(root = document.body) { return disposePage(root); }

if (typeof document !== 'undefined') {
  mount();
  let state;
  window.addEventListener('pagehide', () => { state = dispose() || state; });
  window.addEventListener('pageshow', event => { if (event.persisted) mount(document.body, {state}); });
}
