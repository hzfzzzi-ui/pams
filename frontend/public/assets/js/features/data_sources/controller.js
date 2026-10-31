import { createReadScope, isReadCancelled, readFallback } from '../../shared/read_scope.js?v=20261031-modules-v1';
import { issuesHtml, renderQuality } from './view.js?v=20261031-modules-v1';

const mounted = new WeakMap();

export function mount(root, context) {
  if (!root?.querySelector) throw new TypeError('Data sources page root is required');
  const current = mounted.get(root);
  if (current) return current;
  const {get, mountSyncCenter, bootstrap = () => {}, win = root.ownerDocument?.defaultView} = context;
  const reads = createReadScope({get, signal: context.signal, AbortController: win?.AbortController});
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
    if (!alive || isReadCancelled(error)) return;
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
        reads.get('quality', '/api/portfolio/data-quality?baseCurrency=CNY').catch(error => readFallback(error, null)),
        reads.get('overview', '/api/accounts/overview').catch(error => readFallback(error, null)),
      ]);
      if (alive && issuesContent) issuesContent.innerHTML = issuesHtml(portfolio?.data_quality, overview?.accounts);
      return;
    }
    statusLine?.classList.add('syncSummaryStrip');
    syncCenter = mountSyncCenter(root, {signal: context.signal, targets: {
      summaryEl: statusLine,
      workflowEl: find('todayActions'),
      rowsEl: find('sourceList'),
      tableBodyEl: find('sourceGrid'),
      statusEl: find('manualSyncStatus'),
    }});
    const [portfolio, syncResult] = await Promise.all([
      reads.get('quality', '/api/portfolio/data-quality?baseCurrency=CNY').catch(error => readFallback(error, {data_quality: null})),
      syncCenter.reload().then(
        () => ({status: 'fulfilled'}),
        reason => ({status: 'rejected', reason}),
      ),
    ]);
    if (!alive) return;
    if (syncResult.status === 'rejected' && !isReadCancelled(syncResult.reason)) throw syncResult.reason;
    // A child reload may be replaced independently of the quality read. Keep
    // that valid quality result, then preserve the child's rejected outcome.
    const asof = find('asof');
    if (asof) asof.textContent = portfolio.as_of || new Date().toISOString().slice(0, 10);
    renderQuality(portfolio, find('dataQuality'));
    if (syncResult.status === 'rejected') throw syncResult.reason;
  }

  function dispose() {
    if (!alive) return;
    alive = false;
    reads.dispose();
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
