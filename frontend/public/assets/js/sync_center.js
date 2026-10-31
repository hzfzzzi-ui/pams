import { jget, jpost } from './api.js?v=20261031-gray-v1';
import { openBottomSheet, closeBottomSheet } from './ui.js?v=20261031-ui-v1';
import { mount as mountCenter, dispose as disposeCenter, triggerSync as runSync } from './features/sync_center/controller.js?v=20261031-feature-v1';

export { fmtSyncTime, renderSyncSummary, renderWorkflow, renderSyncRows, renderSyncTable } from './features/sync_center/view.js?v=20261031-feature-v1';

export function mount(root, context = {}) {
  return mountCenter(root, {get: jget, post: jpost, openSheet: openBottomSheet, closeSheet: closeBottomSheet, ...context});
}

export function dispose(root = document.body) {
  disposeCenter(root);
}

export function triggerSync(row, options = {}) {
  return runSync(row, {...options, post: jpost});
}

export function loadSyncCenter(targets = {}) {
  const first = Object.values(targets).find(value => value?.ownerDocument);
  const root = targets.root || first?.ownerDocument.body || document.body;
  return mount(root, {targets}).reload();
}
