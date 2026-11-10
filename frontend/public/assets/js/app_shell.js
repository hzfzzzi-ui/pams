// Keep one navigation DOM node; existing page controllers stay in their own document.
import { CONTENT_PAGES, canonicalContent, contentHref, primaryPageFor, resolveContentRoute, withReturnRoute } from './app/routes.js';
import { installPageMemory } from './app/page_memory.js';
const pages = new Set(CONTENT_PAGES);
const frame = document.getElementById('page-content');
const links = [...document.querySelectorAll('[data-page]')];
let loadedDocument = null;
let replaceOnLoad = true;

function isEditingText() {
  const active = document.activeElement === frame
    ? frame.contentDocument?.activeElement
    : document.activeElement;
  if (active?.isContentEditable) return true;
  if (active?.readOnly || active?.inputMode === 'none') return false;
  return Boolean(active?.matches('textarea, input:is([type=text], [type=search], [type=tel], [type=email], [type=url], [type=password], [type=number], :not([type]))'));
}

function fitVisibleViewport() {
  const iosStandalone = navigator.standalone === true;
  document.body.toggleAttribute('data-ios-standalone', iosStandalone);
  const viewport = window.visualViewport;
  if (!viewport) return () => {};
  const standalone = window.matchMedia('(display-mode: standalone)');

  const update = () => {
    const bottom = viewport.offsetTop + viewport.height;
    // An installed app has no retractable Safari toolbar. Ignore its visual
    // viewport after keyboard dismissal, even if WebKit still reports it short.
    const installed = iosStandalone || standalone.matches;
    const keyboard = installed && document.documentElement.clientHeight - viewport.height >= 120 && isEditingText();
    if (viewport.scale !== 1 || !Number.isFinite(bottom) || viewport.height <= 0 || (installed && !keyboard)) {
      document.body.style.removeProperty('--pams-visible-bottom');
      return;
    }
    document.body.style.setProperty('--pams-visible-bottom', `${bottom}px`);
  };
  viewport.addEventListener('resize', update);
  viewport.addEventListener('scroll', update);
  window.addEventListener('resize', update);
  window.addEventListener('pageshow', update);
  update();
  return () => window.requestAnimationFrame(update);
}

function contentUrl() {
  const url = new URL(location.href);
  const page = url.searchParams.get('view') || 'investments.html';
  // `view` is also a business filter (data_sources.html?view=issues).
  // The shell stores that filter separately; old redirects may supply it as a
  // second view parameter, which must survive direct links as well.
  const contentViews = url.searchParams.has('content_view')
    ? url.searchParams.getAll('content_view')
    : url.searchParams.getAll('view').filter(value => !pages.has(value));
  url.pathname = new URL(pages.has(page) ? page : 'investments.html', location.href).pathname;
  url.searchParams.delete('view');
  url.searchParams.delete('content_view');
  for (const value of contentViews) url.searchParams.append('view', value);
  const target = canonicalContent(url);
  target.searchParams.set('content', '1');
  return target;
}

function navigate(url, replace = false) {
  if (!resolveContentRoute(url, location.href)) return;
  url = canonicalContent(url);
  url.searchParams.set('content', '1');
  // replace avoids adding a second frame-history entry for a shell navigation.
  // Native beforeunload protection in the import editor remains effective.
  replaceOnLoad = replace;
  frame.contentWindow.location.replace(url.href);
}

function syncAddress(replace = false) {
  const child = new URL(frame.contentWindow.location.href);
  const page = child.pathname.split('/').pop();
  if (!resolveContentRoute(child, location.href)) return;
  const outer = new URL('app.html', location.href);
  outer.search = child.search;
  outer.searchParams.delete('content');
  outer.searchParams.delete('content_view');
  for (const value of child.searchParams.getAll('view')) outer.searchParams.append('content_view', value);
  outer.searchParams.set('view', page);
  outer.hash = child.hash;
  if (outer.href !== location.href) {
    if (replace) history.replaceState(history.state, '', outer);
    else history.pushState({from: contentHref(contentUrl())}, '', outer);
  }
  const active = primaryPageFor(page);
  document.body.dataset.contentPage = page;
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
const updateVisibleViewport = fitVisibleViewport();
const pageMemory = installPageMemory(window, frame);
window.addEventListener('message', event => {
  if (event.origin !== location.origin || event.source !== frame.contentWindow) return;
  try {
    if (event.data?.type === 'pams-shell-route') navigate(new URL(event.data.href));
    if (event.data?.type === 'pams-content-back') {
      const route = resolveContentRoute(event.data.href, location.href);
      if (!route) return;
      if (history.state?.from === contentHref(route.url)) history.back();
      else navigate(route.url, true);
    }
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
  if (current === link.dataset.page) return;
  if (event.isTrusted && event.detail > 0 && !link.hasAttribute('aria-current')) {
    navigator.vibrate?.(10);
  }
  const target = new URL(link.dataset.page, location.href);
  if (link.dataset.page === 'ledger.html') {
    const view = pageMemory.read('ledger.html:view', '?content=1');
    if (view?.hash) target.hash = view.hash;
  }
  navigate(target);
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
  syncAddress(replaceOnLoad);
  replaceOnLoad = false;
  loadedDocument = doc;
  doc.addEventListener('focusin', updateVisibleViewport);
  doc.addEventListener('focusout', updateVisibleViewport);
  updateVisibleViewport();
  frame.contentWindow.dispatchEvent(new Event('pams:frame-ready'));
  doc.addEventListener('click', event => {
    if (event.defaultPrevented || event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const anchor = event.target.closest('a[href]');
    if (!anchor || anchor.target || anchor.hasAttribute('download')) return;
    const url = new URL(anchor.href);
    if (!resolveContentRoute(url, location.href)) return;
    if (url.pathname === doc.location.pathname && url.search === doc.location.search) return;
    event.preventDefault();
    if (anchor.hasAttribute('data-content-back')) {
      if (history.state?.from === contentHref(url)) history.back();
      else navigate(url, true);
      return;
    }
    if (anchor.hasAttribute('data-return-route') && !url.searchParams.has('return')) {
      navigate(new URL(withReturnRoute(url, doc.location.href), doc.location.href));
      return;
    }
    navigate(url);
  });
  frame.contentWindow.addEventListener('hashchange', () => syncAddress(true));
});
window.addEventListener('popstate', () => navigate(contentUrl(), true));
const initialContent = contentUrl();
document.body.dataset.contentPage = initialContent.pathname.split('/').pop();
frame.src = initialContent.href;

}
