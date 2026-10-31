// Keep one navigation DOM node; existing page controllers stay in their own document.
const pages = new Set(['investments.html', 'ledger.html', 'more.html', 'accounts.html', 'data_sources.html', 'account_import.html']);
const frame = document.getElementById('page-content');
const links = [...document.querySelectorAll('[data-page]')];

function contentUrl() {
  const url = new URL(location.href);
  const page = url.searchParams.get('view') || 'investments.html';
  url.pathname = new URL(pages.has(page) ? page : 'investments.html', location.href).pathname;
  url.searchParams.delete('view');
  url.searchParams.set('content', '1');
  return url;
}

function navigate(url) {
  if (url.origin !== location.origin || !pages.has(url.pathname.split('/').pop())) return;
  url.searchParams.set('content', '1');
  // replace avoids adding a second frame-history entry for a shell navigation.
  // Native beforeunload protection in the import editor remains effective.
  frame.contentWindow.location.replace(url.href);
}

function syncAddress(replace = false) {
  const child = new URL(frame.contentWindow.location.href);
  const page = child.pathname.split('/').pop();
  if (!pages.has(page)) return;
  const outer = new URL('app.html', location.href);
  outer.search = child.search;
  outer.searchParams.delete('content');
  outer.searchParams.set('view', page);
  outer.hash = child.hash;
  if (outer.href !== location.href) history[replace ? 'replaceState' : 'pushState']({}, '', outer);
  const active = ['investments.html', 'ledger.html'].includes(page) ? page : 'more.html';
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
  if (event.origin !== location.origin || event.source !== frame.contentWindow || event.data?.type !== 'pams-shell-route') return;
  try { navigate(new URL(event.data.href)); } catch { /* Ignore malformed routes. */ }
});
for (const link of links) link.addEventListener('click', event => {
  if (event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  const current = frame.contentWindow.location.pathname.split('/').pop();
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
  doc.addEventListener('click', event => {
    if (event.defaultPrevented || event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const anchor = event.target.closest('a[href]');
    if (!anchor || anchor.target || anchor.hasAttribute('download')) return;
    const url = new URL(anchor.href);
    if (url.origin !== location.origin || !pages.has(url.pathname.split('/').pop())) return;
    if (url.pathname === doc.location.pathname && url.search === doc.location.search) return;
    event.preventDefault();
    navigate(url);
  });
  frame.contentWindow.addEventListener('hashchange', () => syncAddress(true));
});
window.addEventListener('popstate', () => navigate(contentUrl()));
frame.src = contentUrl().href;

}
