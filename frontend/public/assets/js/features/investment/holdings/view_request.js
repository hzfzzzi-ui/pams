import { jget } from '../../../api.js';

let pending;

export function loadHoldingView() {
  if (!pending) {
    const controller = new AbortController();
    const closed = new URLSearchParams(location.search).get('show_closed') === '1';
    const stop = () => {
      pending = undefined;
      controller.abort();
    };
    window.addEventListener('pagehide', stop, { once: true });
    pending = jget(`/api/investment/holdings-view?show_closed=${closed}`, {
      cache: false, signal: controller.signal,
    }).catch(error => {
      if (error.status === 404) return null; // Older API releases still support the separate reads.
      throw error;
    });
  }
  return pending;
}
