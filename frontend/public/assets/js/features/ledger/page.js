import { createLedgerController } from './controller.js?v=20261101-ledger-narrow';
import { snapshotLedgerViewState } from './state.js?v=20261031-modules-v1';

const pages = new WeakMap();

export function mountLedgerPage({ root = document, host = window, ...options } = {}) {
  const existing = pages.get(root);
  if (existing) return existing;
  let controller = null;
  let viewState = null;
  let disposed = false;
  const start = () => {
    if (disposed) return Promise.resolve(null);
    controller = createLedgerController({ ...options, root, host, viewState });
    return controller.init();
  };
  function onPageHide(event) {
    if (disposed) return;
    if (event.persisted && controller) viewState = snapshotLedgerViewState(controller.state);
    controller?.dispose();
    if (!event.persisted) page.dispose();
  }
  function onPageShow(event) {
    if (!disposed && event.persisted && controller?.disposed) page.ready = start();
  }
  const page = {
    get controller() { return controller; },
    ready: null,
    dispose() {
      if (disposed) return;
      disposed = true;
      controller?.dispose();
      host.removeEventListener('pagehide', onPageHide);
      host.removeEventListener('pageshow', onPageShow);
      if (pages.get(root) === page) pages.delete(root);
    },
  };
  pages.set(root, page);
  host.addEventListener('pagehide', onPageHide);
  host.addEventListener('pageshow', onPageShow);
  page.ready = start();
  return page;
}
