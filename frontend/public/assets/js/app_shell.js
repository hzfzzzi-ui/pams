// Keep one navigation DOM node; existing page controllers stay in their own document.
import { CONTENT_PAGES, canonicalContent, contentHref, primaryPageFor, resolveContentRoute, withReturnRoute } from './app/routes.js';
import { installPageMemory } from './app/page_memory.js';
const pages = new Set(CONTENT_PAGES);
const frame = document.getElementById('page-content');
const links = [...document.querySelectorAll('[data-page]')];
const toolsLink = document.querySelector('[data-shell-tools]');
const privacyToggle = document.querySelector('[data-privacy-toggle]');
const PRIVACY_KEY = 'pams:privacy-mode';
let loadedDocument = null;
let replaceOnLoad = true;

function readPrivacyMode() {
  try { return localStorage.getItem(PRIVACY_KEY) === '1'; }
  catch { return false; }
}

function applyPrivacyMode(enabled) {
  document.documentElement.dataset.privacy = enabled ? '1' : '0';
  if (privacyToggle) {
    privacyToggle.setAttribute('aria-pressed', String(enabled));
    privacyToggle.setAttribute('aria-label', enabled ? '显示金额' : '隐藏金额');
    privacyToggle.title = enabled ? '隐私模式：已隐藏金额' : '隐私模式：点击隐藏金额';
    const openIcon = privacyToggle.querySelector('.icon-eye-open');
    const closedIcon = privacyToggle.querySelector('.icon-eye-closed');
    if (openIcon) openIcon.hidden = enabled;
    if (closedIcon) closedIcon.hidden = !enabled;
  }
  try {
    const childDoc = frame.contentDocument;
    if (childDoc?.documentElement) {
      if (enabled) childDoc.documentElement.dataset.privacy = '1';
      else delete childDoc.documentElement.dataset.privacy;
    }
  } catch { /* Cross-origin or unloaded frame. */ }
}

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
  const page = url.searchParams.get('view') || 'more.html';
  url.pathname = new URL(pages.has(page) ? page : 'more.html', location.href).pathname;
  url.searchParams.delete('view');
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
applyPrivacyMode(readPrivacyMode());
privacyToggle?.addEventListener('click', () => {
  const next = !readPrivacyMode();
  try { localStorage.setItem(PRIVACY_KEY, next ? '1' : '0'); } catch { /* Ignore storage quota. */ }
  applyPrivacyMode(next);
});
toolsLink?.addEventListener('click', event => {
  if (event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  const target = new URL('more.html', location.href);
  target.hash = 'moreAdvanced';
  navigate(target);
});
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
  applyPrivacyMode(readPrivacyMode());
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

// Top-right Reminder Center (red badge bell + bottom sheet)
const reminderBtn = document.querySelector('[data-reminders-toggle]');
const reminderBadge = document.querySelector('[data-reminders-badge]');
const reminderBackdrop = document.getElementById('pamsRemindersBackdrop');
const reminderBody = document.getElementById('pamsRemindersBody');
const reminderClose = document.querySelector('[data-reminders-close]');

if (reminderBtn && reminderBackdrop && reminderBody) {
  let reminderState = { pendingReviews: 0, staleItems: [], thsAdjustments: [], refreshing: false, note: '' };
  const escHtml = v => String(v ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  const fmtQty = v => {
    const n = Number(v);
    if (!Number.isFinite(n)) return '--';
    return Math.abs(n - Math.round(n)) < 1e-4 ? Math.round(n).toLocaleString('zh-CN') : n.toFixed(2);
  };

  function renderReminders() {
    const alertCount = (reminderState.pendingReviews > 0 ? 1 : 0) + reminderState.staleItems.length;
    if (reminderBadge) {
      if (alertCount > 0) {
        reminderBadge.textContent = alertCount > 9 ? '9+' : String(alertCount);
        reminderBadge.hidden = false;
        reminderBtn.classList.add('has-alerts');
      } else {
        reminderBadge.hidden = true;
        reminderBtn.classList.remove('has-alerts');
      }
    }
    const cards = [];
    if (reminderState.pendingReviews > 0) {
      cards.push(`
        <button type="button" class="pams-reminder-item is-alert" data-reminder-action="review">
          <span class="pams-reminder-dot is-red" aria-hidden="true"></span>
          <div class="pams-reminder-copy">
            <strong>待复盘交易 · ${reminderState.pendingReviews} 笔</strong>
            <small>趁记忆清晰补一句买入/卖出核心逻辑，沉淀交易复盘</small>
          </div>
          <span class="pams-reminder-arrow" aria-hidden="true">›</span>
        </button>
      `);
    }
    if (reminderState.staleItems.length) {
      const desc = reminderState.staleItems.map(item => `${item.label}（${item.as_of}，落后${item.trading_days_stale}个交易日）`).join(' · ');
      cards.push(`
        <div class="pams-reminder-item is-alert">
          <span class="pams-reminder-dot is-red" aria-hidden="true"></span>
          <div class="pams-reminder-copy">
            <strong>行情 / 净值落后提醒</strong>
            <small>${escHtml(desc)}</small>
            ${reminderState.note ? `<em class="pams-reminder-note">${escHtml(reminderState.note)}</em>` : ''}
          </div>
          <button type="button" class="pams-reminder-actbtn" data-reminder-action="refresh-prices" ${reminderState.refreshing ? 'disabled' : ''}>
            ${reminderState.refreshing ? '刷新中…' : '一键刷新'}
          </button>
        </div>
      `);
    }
    if (reminderState.thsAdjustments.length) {
      const lines = reminderState.thsAdjustments.slice(0, 3).map(item => {
        const sign = Number(item.delta_qty) >= 0 ? '+' : '';
        return `${item.name} ${sign}${fmtQty(item.delta_qty)} 股 → 推演 ${fmtQty(item.projected_qty)} 股（含 ${item.latest_trade_date} 成交）`;
      }).join('；');
      cards.push(`
        <div class="pams-reminder-item is-info">
          <span class="pams-reminder-dot is-blue" aria-hidden="true"></span>
          <div class="pams-reminder-copy">
            <strong>同花顺成交推演（较快照 ${escHtml(reminderState.thsAdjustments[0].snapshot_as_of || '--')}）</strong>
            <small>${escHtml(lines)}</small>
          </div>
        </div>
      `);
    }
    reminderBody.innerHTML = cards.length
      ? cards.join('')
      : '<div class="pams-reminders-empty">当前没有待处理的提醒</div>';
  }

  async function loadReminders() {
    try {
      const [queueRes, freshRes] = await Promise.all([
        fetch('/api/investment-transactions/auto-review/queue?kind=needs_human_review&limit=1').then(r => r.ok ? r.json() : null).catch(() => null),
        fetch('/api/prices/freshness').then(r => r.ok ? r.json() : null).catch(() => null),
      ]);
      const counts = queueRes?.counts || {};
      reminderState.pendingReviews = Number(counts.needs_human_review ?? counts.needs_review ?? (queueRes?.items?.length || 0));
      reminderState.staleItems = Array.isArray(freshRes?.stale_items) ? freshRes.stale_items : [];
      reminderState.thsAdjustments = Array.isArray(freshRes?.ths_projected_adjustments) ? freshRes.ths_projected_adjustments : [];
      renderReminders();
    } catch { /* non-blocking */ }
  }

  function closeReminders() {
    reminderBackdrop.hidden = true;
    reminderBtn.setAttribute('aria-expanded', 'false');
  }

  reminderBtn.addEventListener('click', () => {
    const opening = reminderBackdrop.hidden;
    reminderBackdrop.hidden = !opening;
    reminderBtn.setAttribute('aria-expanded', String(opening));
    if (opening) loadReminders();
  });
  reminderClose?.addEventListener('click', closeReminders);
  reminderBackdrop.addEventListener('click', event => {
    if (event.target === reminderBackdrop) closeReminders();
  });
  reminderBody.addEventListener('click', async event => {
    const trigger = event.target?.closest?.('[data-reminder-action]');
    if (!trigger) return;
    const action = trigger.dataset.reminderAction;
    if (action === 'review') {
      closeReminders();
      const target = new URL('investments.html#investment-ledger', location.href);
      navigate(target);
      window.setTimeout(() => {
        try {
          const childDoc = frame.contentDocument;
          childDoc?.querySelector('[data-invest-tab="transactions"]')?.click();
          window.setTimeout(() => childDoc?.querySelector('[data-trade-view-mode="campaigns"]')?.click(), 160);
        } catch { /* ignore */ }
      }, 280);
      return;
    }
    if (action === 'refresh-prices' && !reminderState.refreshing) {
      reminderState.refreshing = true;
      reminderState.note = '正在刷新全市场行情…';
      renderReminders();
      try {
        const res = await fetch('/api/prices/refresh/investments', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ scopes: ['a_stock', 'us_stock', 'a_fund', 'precious_metal'] }),
        }).then(r => r.json());
        reminderState.note = `已更新 ${Number(res?.total_updated || 0)} 项，正在重算持仓估值…`;
        renderReminders();
        await fetch('/api/investment/page-projection/refresh', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: '{}',
        }).catch(() => {});
        reminderState.refreshing = false;
        reminderState.note = '刷新完成，已同步最新估值。';
        await loadReminders();
        try { frame.contentWindow?.location?.reload(); } catch { /* ignore */ }
      } catch (err) {
        reminderState.refreshing = false;
        reminderState.note = `刷新失败：${err?.message || err}`;
        renderReminders();
      }
    }
  });

  loadReminders();
}

}
