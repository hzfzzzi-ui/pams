import { issuesHtml, renderQuality } from './view.js?v=20261031-feature-v1';

const mounted = new WeakMap();

export function mount(root, context) {
  if (!root?.querySelector) throw new TypeError('Data sources page root is required');
  const current = mounted.get(root);
  if (current) return current;
  const {get, mountSyncCenter, bootstrap = () => {}, win = root.ownerDocument?.defaultView} = context;
  const find = id => root.querySelector(`#${id}`);
  const issuesMode = new URLSearchParams((context.location || win?.location)?.search || '').get('view') === 'issues';
  const issuesView = find('issuesView');
  const issuesContent = find('issuesContent');
  const originalIssuesHidden = issuesView?.hidden;
  const ownedIssuesClass = issuesMode && !root.classList.contains('issues-only');
  const statusLine = find('statusLine');
  const ownedSummaryClass = !issuesMode && statusLine && !statusLine.classList.contains('syncSummaryStrip');
  let alive = true;
  let syncCenter;

  function showError(error) {
    if (!alive) return;
    const el = issuesMode ? issuesContent : find('err');
    if (!el) return;
    el.style.display = 'block';
    el.textContent = String(error?.message || error);
  }

  async function load() {
    if (issuesMode) {
      root.classList.add('issues-only');
      if (issuesView) issuesView.hidden = false;
      const [portfolio, overview] = await Promise.all([
        get('/api/portfolio/data-quality?baseCurrency=CNY').catch(() => null),
        get('/api/accounts/overview').catch(() => null),
      ]);
      if (alive && issuesContent) issuesContent.innerHTML = issuesHtml(portfolio?.data_quality, overview?.accounts);
      return;
    }
    statusLine?.classList.add('syncSummaryStrip');
    syncCenter = mountSyncCenter(root, {targets: {
      summaryEl: statusLine,
      workflowEl: find('todayActions'),
      rowsEl: find('sourceList'),
      tableBodyEl: find('sourceGrid'),
      statusEl: find('manualSyncStatus'),
    }});
    const [portfolio] = await Promise.all([
      get('/api/portfolio/data-quality?baseCurrency=CNY').catch(() => ({data_quality: null})),
      syncCenter.reload(),
    ]);
    if (!alive) return;
    const asof = find('asof');
    if (asof) asof.textContent = portfolio.as_of || new Date().toISOString().slice(0, 10);
    renderQuality(portfolio, find('dataQuality'));
  }

  function dispose() {
    if (!alive) return;
    alive = false;
    syncCenter?.dispose();
    win?.removeEventListener('pagehide', dispose);
    if (ownedIssuesClass) root.classList.remove('issues-only');
    if (issuesMode && issuesView) issuesView.hidden = originalIssuesHidden;
    if (ownedSummaryClass) statusLine.classList.remove('syncSummaryStrip');
    if (mounted.get(root) === controller) mounted.delete(root);
  }

  const controller = {ready: null, dispose, get disposed() { return !alive; }};
  mounted.set(root, controller);
  bootstrap();
  win?.addEventListener('pagehide', dispose);
  controller.ready = load().catch(showError);
  return controller;
}

export function dispose(root) {
  mounted.get(root)?.dispose();
}
