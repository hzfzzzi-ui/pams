import { jget } from './api.js';
import { openBottomSheet, closeBottomSheet, setActiveNav } from './ui.js';
import { mount as mountPage, dispose as disposePage } from './features/investment/holdings/list_controller.js';
import { loadAccountView, loadHoldingView } from './features/investment/holdings/view_request.js';

export function mount(root = document.body, context = {}) {
  return mountPage(root, {get: jget, showDetails: openBottomSheet, closeDetails: closeBottomSheet,
    bootstrap: setActiveNav, loadHoldingView, loadAccountView, ...context});
}
export function dispose(root = document.body) { return disposePage(root); }

if (typeof document !== 'undefined') {
  mount();
  let state;
  window.addEventListener('pagehide', () => { state = dispose() || state; });
  window.addEventListener('pageshow', event => { if (event.persisted) mount(document.body, {state}); });
}
