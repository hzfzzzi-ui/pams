import { PRIMARY_PAGES, resolveContentRoute } from './routes.js?v=20261031-modules-v1';

// Read the versions already selected by the page, rather than duplicating an
// asset/version manifest here. Only same-origin static assets are eligible.
export function collectPrefetchResources(doc) {
  const base = new URL(doc.baseURI);
  const resources = new Map();
  const assetsPath = new URL('assets/', base).pathname;
  for (const page of PRIMARY_PAGES) {
    const route = resolveContentRoute(page, base);
    if (!route || route.url.pathname === base.pathname) continue;
    route.url.searchParams.set('content', '1');
    resources.set(route.url.href, 'document');
  }
  for (const node of doc.querySelectorAll('link[rel="stylesheet"][href], script[type="module"][src]')) {
    const value = node.getAttribute('href') || node.getAttribute('src');
    let url;
    try { url = new URL(value, base); } catch { continue; }
    if (url.origin !== base.origin || !url.pathname.startsWith(assetsPath)) continue;
    resources.set(url.href, node.tagName === 'LINK' ? 'style' : 'script');
  }
  return [...resources].map(([href, as]) => ({href, as}));
}

function warmupPaths(now) {
  const month = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;
  // This is the pre-existing read-only warmup scope; import/preflight data stays out.
  return [
    '/api/accounts/overview',
    '/api/sync/center',
    '/api/portfolio/data-quality?baseCurrency=CNY',
    `/api/ledger/workbench?month=${month}`,
  ];
}

export function createPageBootstrap({
  doc = document, win = window, afterStartup, warmCache, now = () => new Date(),
  AbortController: Controller = win.AbortController || globalThis.AbortController,
}) {
  let started = false;
  let disposed = false;
  const timers = new Set();
  const idleCallbacks = new Set();
  const prefetched = new Set();
  const links = new Set();
  const warmup = new Controller();

  function prefetchResources() {
    for (const {href, as} of collectPrefetchResources(doc)) {
      if (prefetched.has(href)) continue;
      prefetched.add(href);
      const link = doc.createElement('link');
      link.rel = 'prefetch';
      link.href = href;
      link.as = as;
      link.dataset.appPrefetch = href;
      doc.head.append(link);
      links.add(link);
    }
  }

  function schedule(callback, delay) {
    const run = () => {
      if (disposed) return;
      // Waiting for investment startup must also remain cancellable from our
      // side: a late resolution after disposal must not start background work.
      Promise.resolve().then(() => afterStartup(() => {
        if (!disposed) return callback();
      })).catch(() => {});
    };
    const timer = win.setTimeout(() => {
      timers.delete(timer);
      if (disposed) return;
      if (typeof win.requestIdleCallback === 'function') {
        const idle = win.requestIdleCallback(() => {
          idleCallbacks.delete(idle);
          run();
        }, {timeout: 3000});
        idleCallbacks.add(idle);
      } else run();
    }, delay);
    timers.add(timer);
  }

  function start() {
    if (started || disposed) return;
    started = true;
    doc.body.classList.add('app-shell');
    win.addEventListener('pagehide', dispose, {once: true});
    const connection = win.navigator.connection || win.navigator.mozConnection || win.navigator.webkitConnection;
    if (connection?.saveData) return;
    const paths = warmupPaths(now());
    schedule(prefetchResources, 250);
    schedule(() => warmCache(paths, {signal: warmup.signal}), 900);
  }

  function dispose() {
    if (disposed) return;
    disposed = true;
    warmup.abort();
    for (const id of timers) win.clearTimeout(id);
    for (const id of idleCallbacks) win.cancelIdleCallback?.(id);
    timers.clear();
    idleCallbacks.clear();
    for (const link of links) link.remove();
    links.clear();
    prefetched.clear();
    win.removeEventListener('pagehide', dispose);
  }

  return {start, dispose, get disposed() { return disposed; }};
}
