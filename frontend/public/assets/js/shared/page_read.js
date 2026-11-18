import {jget} from '../api.js';

// Share one initial read across the document's controllers. The document
// owns cancellation so disposing either consumer cannot cancel the other.
export function createPageRead(path, {maxAge = 5000, read = jget, host = globalThis.window, now = Date.now} = {}) {
  let pending, request, completedAt = null, requestedPath;
  host?.addEventListener('pagehide', () => {
    request?.abort();
    request = pending = undefined;
    completedAt = null;
  });
  return () => {
    const url = path();
    if (!pending || requestedPath !== url || (completedAt !== null && now() - completedAt >= maxAge)) {
      const controller = new AbortController();
      request = controller;
      requestedPath = url;
      completedAt = null;
      pending = read(url, {cache: false, signal: controller.signal}).then(value => {
        if (request === controller) completedAt = now();
        return value;
      }).catch(error => {
        if (request === controller) {
          pending = undefined;
          completedAt = null;
        }
        if (error.status === 404) return null; // Compatibility with older API releases.
        throw error;
      });
    }
    return pending;
  };
}

// Refresh published results, never trigger market collection or financial writes.
// Both controllers retain their selection and share the same short-lived read.
export function watchPageRead(scope, host, load, {interval = 60000} = {}) {
  let loading = false;
  const visible = () => host.document?.visibilityState !== 'hidden';
  const refresh = async () => {
    if (!visible() || loading || !scope.active) return;
    loading = true;
    try { await load(); } finally { loading = false; }
  };
  scope.listen(host.document, 'visibilitychange', refresh);
  const tick = async () => {
    await refresh();
    scope.timeout(tick, interval);
  };
  scope.timeout(tick, interval);
}
