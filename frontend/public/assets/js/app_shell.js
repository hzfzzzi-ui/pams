// Keep one navigation DOM node; existing page controllers stay in their own document.
import { CONTENT_PAGES, primaryPageFor, resolveContentRoute } from './app/routes.js?v=20261031-ui-v1';
const pages = new Set(CONTENT_PAGES);
const frame = document.getElementById('page-content');
const links = [...document.querySelectorAll('[data-page]')];
let loadedDocument = null;

function contentUrl() {
  const url = new URL(location.href);
  const page = url.searchParams.get('view') || 'investments.html';
  url.pathname = new URL(pages.has(page) ? page : 'investments.html', location.href).pathname;
  url.searchParams.delete('view');
  url.searchParams.set('content', '1');
  return url;
}

function navigate(url) {
  if (!resolveContentRoute(url, location.href)) return;
  url.searchParams.set('content', '1');
  // replace avoids adding a second frame-history entry for a shell navigation.
  // Native beforeunload protection in the import editor remains effective.
  frame.contentWindow.location.replace(url.href);
}

function syncAddress(replace = false) {
  const child = new URL(frame.contentWindow.location.href);
  const page = child.pathname.split('/').pop();
  if (!resolveContentRoute(child, location.href)) return;
  const outer = new URL('app.html', location.href);
  outer.search = child.search;
  outer.searchParams.delete('content');
  outer.searchParams.set('view', page);
  outer.hash = child.hash;
  if (outer.href !== location.href) history[replace ? 'replaceState' : 'pushState']({}, '', outer);
  const active = primaryPageFor(page);
  for (const link of links) {
    if (link.dataset.page === active) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  }
  document.title = frame.contentDocument.title;
  frame.title = document.title;
}

if (window.parent !== window) {
  // Legacy programmatic page navigation may reach the shell inside the frame.
  window.parent.postMessage({type: 'pams-shell-route', href: contentUrl().href}, location.origin);
} else {
window.addEventListener('message', event => {
  if (event.origin !== location.origin || event.source !== frame.contentWindow) return;
  try {
    if (event.data?.type === 'pams-shell-route') navigate(new URL(event.data.href));
    // Initialization can normalize a tab before this document's load event.
    // Its first route must still push the destination, preserving the source.
    if (event.data?.type === 'pams-content-route' && loadedDocument === frame.contentDocument
      && event.data.href === frame.contentWindow.location.href) syncAddress(true);
  } catch { /* Ignore malformed routes and unavailable content documents. */ }
});
for (const link of links) link.addEventListener('click', event => {
  if (event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  let current = '';
  try { current = frame.contentWindow.location.pathname.split('/').pop(); }
  catch { /* A failed or external document must not disable the main navigation. */ }
  if (current !== link.dataset.page) navigate(new URL(link.dataset.page, location.href));
});

frame.addEventListener('load', () => {
  try {
    if (frame.contentWindow.location.origin !== location.origin) return;
  } catch {
    frame.title = '页面加载失败，请刷新重试';
    return;
  }
  const doc = frame.contentDocument;
  doc.documentElement.classList.add('pams-content');
  doc.querySelector('.nav')?.remove();
  syncAddress();
  loadedDocument = doc;
  doc.addEventListener('click', event => {
    if (event.defaultPrevented || event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const anchor = event.target.closest('a[href]');
    if (!anchor || anchor.target || anchor.hasAttribute('download')) return;
    const url = new URL(anchor.href);
    if (!resolveContentRoute(url, location.href)) return;
    if (url.pathname === doc.location.pathname && url.search === doc.location.search) return;
    event.preventDefault();
    navigate(url);
  });
  frame.contentWindow.addEventListener('hashchange', () => syncAddress(true));
});
window.addEventListener('popstate', () => navigate(contentUrl()));
frame.src = contentUrl().href;

}
