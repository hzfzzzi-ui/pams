// Presentation routes only. These names never grant access to API operations.
export const CONTENT_PAGES = Object.freeze([
  'investments.html', 'ledger.html', 'more.html',
  'accounts.html', 'data_sources.html', 'account_import.html',
]);
export const PRIMARY_PAGES = Object.freeze(CONTENT_PAGES.slice(0, 3));

export function resolveContentRoute(value, base) {
  try {
    const reference = new URL(base);
    const url = new URL(value, reference);
    const page = url.pathname.split('/').pop();
    if (url.origin !== reference.origin || !CONTENT_PAGES.includes(page)) return null;
    // A matching basename beneath an unrelated path is not an application route.
    if (url.pathname !== new URL(page, reference).pathname) return null;
    return {page, url};
  } catch {
    return null;
  }
}

export function primaryPageFor(page) {
  if (!CONTENT_PAGES.includes(page)) return null;
  return PRIMARY_PAGES.includes(page) ? page : 'more.html';
}

// Page-local tab/filter updates replace the current history entry, as before.
// Notify the persistent shell explicitly: replaceState does not emit hashchange.
export function replaceContentAddress(value, win = window) {
  const route = resolveContentRoute(value, win.location.href);
  if (!route || route.url.pathname !== win.location.pathname) return false;
  win.history.replaceState(null, '', route.url.href);
  if (win.parent !== win) {
    win.parent.postMessage({type: 'pams-content-route', href: route.url.href}, route.url.origin);
  }
  return true;
}
