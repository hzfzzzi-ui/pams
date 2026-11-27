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
  const on = Boolean(enabled);
  document.documentElement.dataset.privacy = on ? '1' : '0';
  if (privacyToggle) {
    privacyToggle.setAttribute('aria-pressed', String(on));
    privacyToggle.setAttribute('aria-label', on ? '显示金额' : '隐藏金额');
    privacyToggle.title = on ? '隐私模式：已隐藏金额' : '隐私模式：点击隐藏金额';
    const openIcon = privacyToggle.querySelector('.icon-eye-open');
    const closedIcon = privacyToggle.querySelector('.icon-eye-closed');
    if (openIcon) openIcon.toggleAttribute('hidden', on);
    if (closedIcon) closedIcon.toggleAttribute('hidden', !on);
  }
  try {
    const childDoc = frame.contentDocument;
    if (childDoc?.documentElement) {
      if (on) childDoc.documentElement.dataset.privacy = '1';
      else delete childDoc.documentElement.dataset.privacy;
      childDoc.body?.classList.toggle('is-privacy-masked', on);
    }
    frame.contentWindow?.postMessage({ type: 'pams-privacy', enabled: on }, location.origin);
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
  let reminderState = {
    pendingReviews: 0,
    ledgerTodo: null,
    ledgerTodoPeriod: 'ytd',
    staleItems: [],
    thsAdjustments: [],
    refreshing: false,
    note: '',
  };
  const escHtml = v => String(v ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  const fmtQty = v => {
    const n = Number(v);
    if (!Number.isFinite(n)) return '--';
    return Math.abs(n - Math.round(n)) < 1e-4 ? Math.round(n).toLocaleString('zh-CN') : n.toFixed(2);
  };
  const buildLedgerTodo = (period, periodLabel = '本年至今') => {
    if (!period || typeof period !== 'object') return null;
    const unclassified = Number(period.unclassified_count) || 0;
    const subcategoryPending = Number(period.subcategory_pending_count) || 0;
    const otherPending = Math.max(0, (Number(period.pending_count) || 0) - unclassified - subcategoryPending);
    const parts = [];
    if (unclassified > 0) parts.push(`${unclassified} 笔待分类`);
    if (otherPending > 0) parts.push(`${otherPending} 笔待核对`);
    if (subcategoryPending > 0 && !unclassified) parts.push(`${subcategoryPending} 笔子类待补`);
    if (!parts.length) return null;
    return { periodLabel, unclassified, otherPending, subcategoryPending, summary: parts.join(' · ') };
  };

  function renderReminders() {
    const alertCount = (reminderState.pendingReviews > 0 ? 1 : 0)
      + (reminderState.ledgerTodo ? 1 : 0)
      + reminderState.staleItems.length;
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
    if (reminderState.ledgerTodo) {
      const todo = reminderState.ledgerTodo;
      const urgent = todo.unclassified > 0 || todo.otherPending > 0;
      cards.push(`
        <button type="button" class="pams-reminder-item ${urgent ? 'is-alert' : 'is-info'}" data-reminder-action="ledger-todo">
          <span class="pams-reminder-dot ${urgent ? 'is-red' : 'is-blue'}" aria-hidden="true"></span>
          <div class="pams-reminder-copy">
            <strong>收支待处理 · ${escHtml(todo.summary)}</strong>
            <small>${escHtml(todo.periodLabel || '本年至今')}流水，点击前往收支流水核对补录</small>
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

  window.addEventListener('message', event => {
    if (event.origin !== location.origin || event.source !== frame.contentWindow) return;
    if (event.data?.type !== 'pams-ledger-todo') return;
    reminderState.ledgerTodoPeriod = event.data.period === 'month' ? 'month' : 'ytd';
    const summary = String(event.data.summary || '').trim();
    reminderState.ledgerTodo = summary ? {
      periodLabel: String(event.data.periodLabel || (reminderState.ledgerTodoPeriod === 'month' ? '本月至今' : '本年至今')),
      unclassified: Number(event.data.unclassified) || 0,
      otherPending: Number(event.data.otherPending) || 0,
      subcategoryPending: Number(event.data.subcategoryPending) || 0,
      summary,
    } : null;
    renderReminders();
  });

  async function loadReminders() {
    try {
      const [queueRes, freshRes, ledgerRes] = await Promise.all([
        fetch('/api/investment-transactions/auto-review/queue?kind=needs_human_review&limit=1').then(r => r.ok ? r.json() : null).catch(() => null),
        fetch('/api/prices/freshness').then(r => r.ok ? r.json() : null).catch(() => null),
        fetch('/api/ledger/workbench?view=overview').then(r => r.ok ? r.json() : null).catch(() => null),
      ]);
      const counts = queueRes?.counts || {};
      reminderState.pendingReviews = Number(counts.needs_human_review ?? counts.needs_review ?? (queueRes?.items?.length || 0));
      reminderState.staleItems = Array.isArray(freshRes?.stale_items) ? freshRes.stale_items : [];
      reminderState.thsAdjustments = Array.isArray(freshRes?.ths_projected_adjustments) ? freshRes.ths_projected_adjustments : [];
      if (ledgerRes?.periods) {
        const key = reminderState.ledgerTodoPeriod === 'month' && ledgerRes.periods.month ? 'month' : 'ytd';
        const periodData = ledgerRes.periods[key] || ledgerRes.periods.ytd || ledgerRes.periods.month;
        reminderState.ledgerTodo = buildLedgerTodo(periodData, key === 'month' ? '本月至今' : '本年至今');
      }
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
      const openCampaignReview = () => {
        try {
          const childDoc = frame.contentDocument;
          childDoc?.querySelector('[data-invest-tab="transactions"]')?.click();
          window.setTimeout(() => childDoc?.querySelector('[data-trade-view-mode="campaigns"]')?.click(), 160);
        } catch { /* ignore */ }
      };
      let currentPage = '';
      try { currentPage = frame.contentWindow.location.pathname.split('/').pop(); } catch { /* ignore */ }
      if (currentPage === 'investments.html') {
        openCampaignReview();
      } else {
        frame.addEventListener('load', () => window.setTimeout(openCampaignReview, 80), { once: true });
        navigate(new URL('investments.html#investment-ledger', location.href));
      }
      return;
    }
    if (action === 'ledger-todo') {
      closeReminders();
      let currentPage = '';
      try { currentPage = frame.contentWindow.location.pathname.split('/').pop(); } catch { /* ignore */ }
      if (currentPage === 'ledger.html') {
        try {
          const childDoc = frame.contentDocument;
          const todoBtn = childDoc?.getElementById('ledgerTodoPill');
          if (todoBtn) todoBtn.click();
          else childDoc?.querySelector('[data-ledger-tab="recent"]')?.click();
        } catch { /* ignore */ }
      } else {
        navigate(new URL('ledger.html#recent-flow-section', location.href));
      }
      return;
    }
    if (action === 'refresh-prices' && !reminderState.refreshing) {
      reminderState.refreshing = true;
      reminderState.note = '正在刷新全市场行情…';
      renderReminders();
      try {
        const priceResp = await fetch('/api/prices/refresh/investments', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ scopes: ['a_stock', 'us_stock', 'a_fund', 'precious_metal'] }),
        });
        const res = await priceResp.json().catch(() => null);
        if (!priceResp.ok) {
          throw new Error(res?.detail || `HTTP ${priceResp.status}`);
        }
        reminderState.note = `已更新 ${Number(res?.total_updated || 0)} 项，正在重算持仓估值…`;
        renderReminders();
        const projResp = await fetch('/api/investment/page-projection/refresh', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: '{}',
        });
        if (!projResp.ok) {
          const errBody = await projResp.json().catch(() => null);
          throw new Error(errBody?.detail || `估值刷新失败 (HTTP ${projResp.status})`);
        }
        try {
          for (let i = sessionStorage.length - 1; i >= 0; i -= 1) {
            const key = sessionStorage.key(i);
            if (key?.startsWith('pams.api.cache.') || key?.startsWith('pams:page-memory:')) {
              sessionStorage.removeItem(key);
            }
          }
        } catch { /* ignore storage errors */ }
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
