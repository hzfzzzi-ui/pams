import { createReadScope, isReadCancelled } from '../../shared/read_scope.js?v=20261031-modules-v1';
export function issueSummary(payload) {
  const summary = payload?.data_quality?.summary;
  if (!summary || !['ok', 'warning', 'error'].includes(summary.status) ||
      !Number.isInteger(summary.error_count) || !Number.isInteger(summary.warning_count) ||
      summary.error_count < 0 || summary.warning_count < 0) throw new Error('数据状态暂不可用');
  return summary.status === 'ok' ? null : `错误 ${summary.error_count} · 警告 ${summary.warning_count}`;
}


const mounted = new WeakMap();

// Each page instance owns its queries, listeners and pending response lifetime.
// Each GET is independently cancellable; late-response guards remain in place.
export function mount(root, context) {
  if (!root?.querySelector) throw new TypeError('More page root is required');
  const current = mounted.get(root);
  if (current) return current;
  const {get, renderExplanation, bootstrap = () => {}, win = root.ownerDocument?.defaultView} = context;
  const reads = createReadScope({get, signal: context.signal, AbortController: win?.AbortController});
  const find = id => root.querySelector(`#${id}`);
  const row = find('moreIssues');
  const label = find('moreIssueText');
  const panel = find('moreNavGrowthExplanationPanel');
  const details = find('moreNavGrowthDetails');
  let alive = true;
  let navGrowthLoaded = false;
  let navGrowthPending;

  async function loadIssues() {
    try {
      const payload = await reads.get('issues', '/api/portfolio/data-quality?baseCurrency=CNY', {cache: false});
      if (!alive) return;
      const text = issueSummary(payload);
      if (row) row.hidden = text === null;
      const title = row?.querySelector('strong');
      if (title) title.textContent = '待处理';
      if (label && text !== null) label.textContent = text;
    } catch (error) {
      if (!alive || isReadCancelled(error)) return;
      if (row) row.hidden = false;
      const title = row?.querySelector('strong');
      if (title) title.textContent = '数据状态暂不可用';
      if (label) label.textContent = '';
    }
  }

  function loadNavGrowthExplanationOnce() {
    if (!alive || !panel || navGrowthLoaded) return Promise.resolve();
    if (navGrowthPending) return navGrowthPending;
    panel.innerHTML = '<div class="muted">正在加载NAV增值解释...</div>';
    navGrowthPending = (async () => {
      try {
        const payload = await reads.get('explanation', '/api/portfolio/equity-bridge?start=2026-01-01&baseCurrency=CNY', {cache: false});
        if (!alive) return;
        renderExplanation(payload, panel);
        navGrowthLoaded = true;
      } catch (error) {
        if (!alive || isReadCancelled(error)) return;
        panel.innerHTML = '<div class="muted"></div>';
        panel.firstElementChild.textContent = `NAV增值解释加载失败：${error?.message || String(error)}`;
      } finally {
        navGrowthPending = null;
      }
    })();
    return navGrowthPending;
  }

  function onToggle() {
    if (details.open) void loadNavGrowthExplanationOnce();
  }

  function dispose() {
    if (!alive) return;
    alive = false;
    reads.dispose();
    details?.removeEventListener('toggle', onToggle);
    win?.removeEventListener('pagehide', dispose);
    if (mounted.get(root) === controller) mounted.delete(root);
  }

  const controller = {ready: null, dispose, get disposed() { return !alive; }};
  mounted.set(root, controller);
  bootstrap();
  const origin = find('moreOrigin');
  if (origin) origin.textContent = (context.location || win?.location)?.origin || '';
  details?.addEventListener('toggle', onToggle);
  win?.addEventListener('pagehide', dispose);
  if (details?.open) void loadNavGrowthExplanationOnce();
  controller.ready = loadIssues();
  return controller;
}

export function dispose(root) {
  mounted.get(root)?.dispose();
}
