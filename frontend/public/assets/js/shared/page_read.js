import {jget} from '../api.js';

// Share one initial read across the document's controllers. The document
// owns cancellation so disposing either consumer cannot cancel the other.
export function createPageRead(path) {
  let pending;
  return () => {
    if (!pending) {
      const controller = new AbortController();
      window.addEventListener('pagehide', () => {
        pending = undefined;
        controller.abort();
      }, {once: true});
      pending = jget(path(), {cache: false, signal: controller.signal}).catch(error => {
        if (error.status === 404) return null; // Compatibility with older API releases.
        throw error;
      });
    }
    return pending;
  };
}
