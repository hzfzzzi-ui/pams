// Presentation routes only. These names never grant access to API operations.
export const CONTENT_PAGES = Object.freeze([
  'investments.html', 'ledger.html', 'more.html',
  'accounts.html', 'data_sources.html', 'account_import.html',
]);
export const PRIMARY_PAGES = Object.freeze(CONTENT_PAGES.slice(0, 3));
export const ACCOUNT_TASKS = Object.freeze({balance: '余额核准', transfers: '转账匹配', sources: '导入配置'});

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

export function contentHref(url) {
  const target = new URL(url);
  target.searchParams.delete('content');
  return target.pathname.split('/').pop() + target.search + target.hash;
}

export function accountCenterHref(account = '', review = false) {
  const params = new URLSearchParams();
  if (account) params.set('account', account);
  if (account && review) params.set('review', 'balance');
  return 'more.html' + (params.size ? '?' + params : '') + '#account-center';
}

export function canonicalContent(url) {
  if (!url.pathname.endsWith('/accounts.html') || Object.hasOwn(ACCOUNT_TASKS, url.searchParams.get('task'))) return url;
  return new URL(accountCenterHref(url.searchParams.get('account')), url);
}

export function withReturnRoute(target, origin) {
  const route = resolveContentRoute(target, origin);
  const source = resolveContentRoute(origin, origin);
  if (!route || !source) return target;
  route.url.searchParams.set('return', contentHref(source.url));
  return contentHref(route.url);
}

export function returnRoute(location, fallback) {
  const query = new URLSearchParams(location.search);
  const route = resolveContentRoute(query.get('return') || fallback, location.href);
  if (!route || route.url.pathname === new URL(location.href).pathname) return fallback;
  return contentHref(canonicalContent(route.url));
}

export function openContent(win, target, back = false) {
  const href = new URL(target, win.location.href);
  if (win.parent !== win && new URLSearchParams(win.location.search).get('content') === '1') {
    win.parent.postMessage({type: back ? 'pams-content-back' : 'pams-shell-route', href: href.href}, href.origin);
  } else {
    win.location.assign(href.href);
  }
}

export function leaveContent(win, fallback) {
  openContent(win, returnRoute(win.location, fallback), true);
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
