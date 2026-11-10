const pending = new WeakMap();
const source = 'https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js';

// A slow or unavailable chart library must not block the rest of the page.
export function loadChart(host = window) {
  if (typeof host.Chart === 'function') return Promise.resolve(host.Chart);
  const doc = host.document;
  if (!doc?.head) return Promise.resolve(null);
  if (!pending.has(doc)) {
    const script = doc.createElement('script');
    const request = new Promise(resolve => {
      function finish() {
        host.clearTimeout(timeout);
        script.onload = script.onerror = null;
        const ChartClass = typeof host.Chart === 'function' ? host.Chart : null;
        if (!ChartClass) script.remove();
        resolve(ChartClass);
      }
      const timeout = host.setTimeout(finish, 4000);
      script.onload = script.onerror = finish;
      script.async = true;
      script.src = source;
      doc.head.append(script);
    }).finally(() => pending.delete(doc));
    pending.set(doc, request);
  }
  return pending.get(doc);
}
