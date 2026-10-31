import { createAnalysisController } from './controller.js?v=20261031-modules-v1';
import { snapshotAnalysisViewState } from './state.js?v=20261031-modules-v1';

const pages = new WeakMap();

export function mountAnalysisPage({ root = document, host = window, ...options } = {}) {
  const existing = pages.get(root);
  if (existing) return existing;
  let controller = null;
  let viewState = null;
  let disposed = false;
  const start = () => {
    if (disposed) return null;
    controller = createAnalysisController({ ...options, root, host, viewState });
    return controller.init();
  };
  function onPageHide(event) {
    if (disposed) return;
    // Restore ordinary UI choices only: responses are reread, and detail sheets stay closed.
    if (event.persisted && controller) viewState = snapshotAnalysisViewState(controller.state);
    controller?.dispose();
    if (!event.persisted) page.dispose();
  }
  function onPageShow(event) {
    if (!disposed && event.persisted && controller?.disposed) start();
  }
  const page = {
    get controller() { return controller; },
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
  start();
  return page;
}
