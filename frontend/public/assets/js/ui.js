import { warmApiCache } from './api.js?v=20260619-investment-hardening-v1';

export function setActiveNav() {
  const rawPath = location.pathname || '/';
  const path = rawPath === '/' ? 'investments.html' : (rawPath.split('/').pop() || 'investments.html');
  const aliases = {
    index: 'investments.html',
    'index.html': 'investments.html',
    accounts: 'more.html',
    'accounts.html': 'more.html',
    data_sources: 'more.html',
    'data_sources.html': 'more.html',
    research: 'more.html',
    'research.html': 'more.html',
    risk: 'investments.html',
    'risk.html': 'investments.html',
  };
  const activePath = aliases[path] || path;
  enhanceMobileAppShell(activePath, path);
  document.querySelectorAll('[data-nav]')?.forEach((a) => {
    a.classList.remove('active');
    const href = a.getAttribute('href') || '';
    const key = href.split('/').pop();
    if (key === activePath) a.classList.add('active');
  });
}

export function openBottomSheet(title, html) {
  let sheet = document.getElementById('appBottomSheet');
  if (!sheet) {
    sheet = document.createElement('div');
    sheet.id = 'appBottomSheet';
    sheet.className = 'appBottomSheet';
    sheet.innerHTML = `
      <div class="appBottomSheetScrim" data-sheet-close></div>
      <section class="appBottomSheetPanel" role="dialog" aria-modal="true">
        <div class="appBottomSheetHandle"></div>
        <div class="appBottomSheetHead">
          <h3 id="appBottomSheetTitle"></h3>
          <button type="button" class="appBottomSheetClose" data-sheet-close>关闭</button>
        </div>
        <div class="appBottomSheetBody" id="appBottomSheetBody"></div>
      </section>
    `;
    document.body.append(sheet);
    sheet.addEventListener('click', (event) => {
      if (event.target?.hasAttribute('data-sheet-close')) closeBottomSheet();
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') closeBottomSheet();
    });
  }
  document.getElementById('appBottomSheetTitle').textContent = title || '详情';
  document.getElementById('appBottomSheetBody').innerHTML = html || '';
  sheet.classList.add('open');
  document.body.classList.add('sheet-open');
}

export function closeBottomSheet() {
  const sheet = document.getElementById('appBottomSheet');
  if (!sheet) return;
  sheet.classList.remove('open');
  document.body.classList.remove('sheet-open');
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[ch]));
}

export function dataStateHtml(options = {}) {
  const tone = options.tone || 'empty';
  const title = options.title || '暂无数据';
  const detail = options.detail || '';
  const badge = options.badge || {
    error: '失败',
    loading: '加载中',
    stale: '待更新',
    empty: '空',
  }[tone] || '状态';
  const badgeTone = {
    error: 'warn',
    loading: 'pending',
    stale: 'warn',
    empty: 'data',
  }[tone] || 'data';
  const compact = options.compact ? ' compact' : '';
  const action = options.actionHref && options.actionLabel
    ? `<a class="miniBadge data dataStateAction" href="${escapeHtml(options.actionHref)}">${escapeHtml(options.actionLabel)}</a>`
    : '';
  return `
    <div class="emptyState dataState dataState-${escapeHtml(tone)}${compact}">
      <div class="dataStateTop">
        <strong class="dataStateTitle">${escapeHtml(title)}</strong>
        <span class="miniBadge ${escapeHtml(badgeTone)}">${escapeHtml(badge)}</span>
      </div>
      ${detail ? `<p class="dataStateDetail">${escapeHtml(detail)}</p>` : ''}
      ${action}
    </div>
  `;
}

export function renderDataState(target, options = {}) {
  const el = typeof target === 'string' ? document.getElementById(target) : target;
  if (!el) return;
  el.innerHTML = dataStateHtml(options);
}

const MOBILE_TABS = {
  'investments.html': { key: 'invest', label: '投资' },
  'ledger.html': { key: 'ledger', label: '收支' },
  'more.html': { key: 'more', label: '更多' },
};

function enhanceMobileAppShell(activePath, path) {
  if (document.body.dataset.mobileShellReady === '1') return;
  document.body.dataset.mobileShellReady = '1';
  document.body.classList.add('app-shell');
  setupAppPreload(activePath, path);

  const nav = document.querySelector('.nav');
  if (nav) {
    const primary = new Set(['investments.html', 'ledger.html', 'more.html']);
    const moreActive = new Set(['more.html', 'accounts.html', 'data_sources.html', 'research.html']);
    const investActive = new Set(['investments.html']);
    const ledgerActive = new Set(['ledger.html']);
    nav.querySelectorAll('[data-nav]').forEach((a) => {
      const key = (a.getAttribute('href') || '').split('/').pop();
      a.dataset.mobileNav = primary.has(key) ? 'primary' : 'overflow';
    });
    const bottomMore = nav.querySelector('a[href$="more.html"]') || nav.querySelector('a[href$="data_sources.html"]') || nav.querySelector('[data-mobile-nav="overflow"]');
    if (bottomMore) {
      bottomMore.id = 'mobileBottomMoreButton';
      bottomMore.classList.add('mobileBottomMoreButton');
      bottomMore.classList.toggle('active', moreActive.has(path) || moreActive.has(activePath));
      bottomMore.href = 'more.html';
      bottomMore.dataset.mobileNav = 'primary';
      bottomMore.textContent = '更多';
    }
    const bottomInvest = nav.querySelector('a[href$="investments.html"]');
    if (bottomInvest) {
      bottomInvest.classList.toggle('active', investActive.has(path) || investActive.has(activePath));
    }
    const bottomLedger = nav.querySelector('a[href$="ledger.html"]');
    if (bottomLedger) {
      bottomLedger.classList.toggle('active', ledgerActive.has(path) || ledgerActive.has(activePath));
      bottomLedger.textContent = '收支';
    }
    nav.querySelectorAll('[data-nav]').forEach((a) => {
      const key = (a.getAttribute('href') || '').split('/').pop();
      const meta = MOBILE_TABS[key];
      if (!meta) return;
      a.dataset.mobileTab = meta.key;
      if (!a.querySelector('.mobileNavText')) {
        a.textContent = '';
        const icon = document.createElement('span');
        icon.className = 'mobileNavIcon';
        icon.setAttribute('aria-hidden', 'true');
        const text = document.createElement('span');
        text.className = 'mobileNavText';
        text.textContent = meta.label;
        a.append(icon, text);
      }
    });
  }

  const statusbar = document.querySelector('.statusbar');
  if (!statusbar) return;
  const brand = statusbar.querySelector('.brand');
  const pageTitle = document.querySelector('.pageHeader h1')?.textContent?.trim() || 'PAMS';
  if (brand && !document.getElementById('mobilePageTitle')) {
    const title = document.createElement('span');
    title.id = 'mobilePageTitle';
    title.className = 'mobilePageTitle';
    title.textContent = pageTitle;
    brand.after(title);
  }

  const metaSource = [...statusbar.querySelectorAll('.status-item')]
    .map((node) => node.textContent?.trim())
    .filter(Boolean)
    .find((text) => /\d{4}-\d{2}-\d{2}|CNY|USD|MXN|LIVE|本地|只读|手动/.test(text));
  if (metaSource && !document.getElementById('mobileHeaderMeta')) {
    const meta = document.createElement('span');
    meta.id = 'mobileHeaderMeta';
    meta.className = 'mobileHeaderMeta';
    meta.textContent = metaSource;
    statusbar.append(meta);
  }
}

function scheduleIdle(callback, delayMs = 0) {
  const run = () => {
    if (typeof requestIdleCallback === 'function') {
      requestIdleCallback(callback, { timeout: 3000 });
    } else {
      setTimeout(callback, 0);
    }
  };
  if (delayMs > 0) setTimeout(run, delayMs);
  else run();
}

function addPrefetch(href, as) {
  if (!href || [...document.querySelectorAll('link[data-app-prefetch]')].some((node) => node.dataset.appPrefetch === href)) return;
  const link = document.createElement('link');
  link.rel = 'prefetch';
  link.href = href;
  link.dataset.appPrefetch = href;
  if (as) link.as = as;
  document.head.append(link);
}

function currentMonthParam() {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;
}

function setupAppPreload(activePath, path) {
  if (document.body.dataset.appPreloadReady === '1') return;
  document.body.dataset.appPreloadReady = '1';
  const connection = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
  if (connection?.saveData) return;

  scheduleIdle(() => {
    [
      ['investments.html', 'document'],
      ['ledger.html', 'document'],
      ['more.html', 'document'],
      ['assets/css/style.css?v=20260620-mobile-density-v1', 'style'],
      ['assets/css/design-system.css?v=20260620-visual-cohesion-v1', 'style'],
      ['assets/js/api.js?v=20260619-investment-hardening-v1', 'script'],
      ['assets/js/ui.js?v=20260620-data-state-v1', 'script'],
      ['assets/js/account_cash.js?v=20260606-cash-active-v1', 'script'],
      ['assets/js/holdings.js?v=20260620-data-state-v1', 'script'],
      ['assets/js/holdings_categories.js?v=20260620-category-detail-hidden-v1', 'script'],
      ['assets/js/lookthrough.js?v=20260620-data-state-v1', 'script'],
      ['assets/js/ledger_cashflow.js?v=20260621-ledger-fx-transfer-v1', 'script'],
      ['assets/js/transactions.js?v=20260620-data-state-v1', 'script'],
      ['assets/js/portfolio_risk.js?v=20260620-data-state-v1', 'script'],
      ['assets/js/portfolio_config.js?v=20260620-data-state-v1', 'script'],
      ['assets/js/more.js?v=20260620-data-state-v1', 'script'],
    ].forEach(([href, as]) => addPrefetch(href, as));
  }, 250);

  const month = currentMonthParam();
  scheduleIdle(() => {
    warmApiCache([
      '/api/accounts/overview',
      '/api/sync/center',
      '/api/portfolio/data-quality?baseCurrency=CNY',
      `/api/ledger/workbench?month=${month}`,
    ]).catch(() => {});
  }, 900);
}

export function kpi(label, value, deltaText, deltaDir) {
  const dirClass = deltaDir === 'up' ? 'up' : 'down';
  const delta = deltaText ? `<span class="${dirClass}">${deltaText}</span>` : '<span class="muted">--</span>';
  return `
    <div class="card kpi">
      <div class="label">${label}</div>
      <div class="value mono">${value}</div>
      <div class="delta">${delta}</div>
    </div>
  `;
}

export function renderAlerts(el, alerts) {
  el.innerHTML = (alerts || []).map((a) => {
    const sev = a.type || a.severity || 'info';
    const tagClass = sev === 'risk' || sev === 'crit' ? 'risk' : (sev === 'warn' ? 'warn' : 'data');
    const tagText = sev === 'risk' || sev === 'crit' ? '风控' : (sev === 'warn' ? '关注' : '数据');
    const title = a.title || a.message || '';
    const body = a.body || a.details || a.data || '';
    return `
      <div class="alert" style="margin-bottom:10px">
        <span class="tag ${tagClass}">${tagText}</span>
        <div style="font-weight:800;margin:6px 0 4px 0;font-size:13px">${title}</div>
        <div class="muted">${body}</div>
      </div>
    `;
  }).join('') || '<div class="muted">暂无</div>';
}

export function renderAlertsStrip(el, alerts) {
  if (!alerts || alerts.length === 0) {
    el.innerHTML = '<div class="muted" style="padding:4px 0">暂无告警</div>';
    return;
  }
  el.innerHTML = alerts.map((a) => {
    const sev = a.type || a.severity || 'info';
    const tagClass = sev === 'risk' || sev === 'crit' ? 'risk' : (sev === 'warn' ? 'warn' : 'data');
    const tagText = sev === 'risk' || sev === 'crit' ? '风控' : (sev === 'warn' ? '关注' : '数据');
    const title = a.title || a.message || '';
    return `<div class="alertItem"><span class="tag ${tagClass}">${tagText}</span><span>${title}</span></div>`;
  }).join('');
}
