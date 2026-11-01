// Compatibility entry. The private import protocol remains separate from api.js.
export { decimalString, fileFormat, previewFields, accessState, displayAssetType, recordEditable, publishable } from './features/account_import/contract.js?v=20261031-modules-v1';
export { createImportClient } from './features/account_import/client.js?v=20261031-modules-v1';
export { mount, dispose } from './features/account_import/controller.js?v=20261101-empty-draft';
import { mount, dispose } from './features/account_import/controller.js?v=20261101-empty-draft';

if (typeof document !== 'undefined') {
  mount(document.body);
  let snapshot;
  window.addEventListener('pagehide', () => { snapshot = dispose(document.body) || snapshot; });
  window.addEventListener('pageshow', event => {
    if (event.persisted) mount(document.body, { snapshot });
  });
}

