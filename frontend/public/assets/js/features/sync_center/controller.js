import { createReadScope, isReadCancelled } from '../../shared/read_scope.js?v=20261031-modules-v1';
import { dataStateHtml } from '../../ui.js?v=20261031-modules-v1';
import { fmtSyncTime, renderSyncSummary, renderWorkflow, renderSyncRows, renderSyncTable, rowDetail, setStatus } from './view.js?v=20261031-modules-v1';

const mounted = new WeakMap();

// A write already accepted by the API is never cancelled or retried on disposal.
// Only this controller's subsequent rendering and read reload are suppressed.
export async function triggerSync(row, {statusEl, after, post, isActive = () => true} = {}) {
  if (!row?.trigger_endpoint || !isActive()) return;
  setStatus(statusEl, `${row.source} 刷新中...`, 'loading');
  try {
    const result = await post(row.trigger_endpoint, row.trigger_body || {});
    if (!isActive()) return;
    if (result?.success === false || ['blocked', 'failed', 'error'].includes(result?.status)) {
      if (after) {
        try { await after(); }
        catch (error) {
          // Replacing only the refresh read must not hide a known POST failure.
          if (!isReadCancelled(error)) throw error;
        }
      }
      if (!isActive()) return;
      throw new Error(result.reason || result.error || '未通过发布检查，保留上一有效值');
    }
    setStatus(statusEl, `${row.source} 已刷新`, 'ok');
    if (after) await after();
  } catch (err) {
    if (isActive() && !isReadCancelled(err)) setStatus(statusEl, `${row.source} 刷新失败：${String(err?.message || err)}`, 'error');
  }
}

export function mount(root, context) {
  if (!root?.querySelector || !root?.addEventListener) throw new TypeError('Sync center root is required');
  const current = mounted.get(root);
  if (current) {
    if (context.targets) current.setTargets(context.targets);
    return current;
  }
  const {get, post, openSheet, closeSheet, win = root.ownerDocument?.defaultView} = context;
  const reads = createReadScope({get, signal: context.signal, AbortController: win?.AbortController});
  let targets = {};
  let alive = true;
  let generation = 0;
  let byId = new Map();
  let releaseSheet = () => {};

  function setTargets(value) {
    const next = {};
    for (const name of ['summaryEl', 'workflowEl', 'rowsEl', 'tableBodyEl', 'statusEl']) {
      const element = value[name];
      if (element && !root.contains(element)) throw new TypeError(`Sync target ${name} is outside its root`);
      next[name] = element;
    }
    targets = next;
  }
  setTargets(context.targets || {});

  async function reload() {
    if (!alive) return null;
    const request = ++generation;
    let data;
    try {
      data = await reads.get('center', '/api/sync/center');
    } catch (error) {
      if (isReadCancelled(error)) throw error;
      const message = String(error?.message || error);
      const failed = {rows: [], summary: {}, workflows: [], error: message};
      if (!alive || request !== generation) return failed;
      byId = new Map();
      const stateHtml = dataStateHtml({
        tone: 'error', title: '同步中心读取失败', detail: message, compact: true,
      });
      if (targets.summaryEl) targets.summaryEl.innerHTML = stateHtml;
      if (targets.workflowEl) targets.workflowEl.innerHTML = stateHtml;
      if (targets.rowsEl) targets.rowsEl.innerHTML = stateHtml;
      if (targets.tableBodyEl) targets.tableBodyEl.innerHTML = `<tr><td colspan="6">${stateHtml}</td></tr>`;
      setStatus(targets.statusEl, `同步中心读取失败：${message}`, 'error');
      return failed;
    }
    if (!alive || request !== generation) return data;
    renderSyncSummary(targets.summaryEl, data);
    renderWorkflow(targets.workflowEl, data);
    renderSyncRows(targets.rowsEl, data);
    renderSyncTable(targets.tableBodyEl, data);
    if (targets.statusEl && !targets.statusEl.textContent.trim()) {
      setStatus(targets.statusEl, `同步中心已更新：${fmtSyncTime(data.updated_at)}`, 'ok');
    }
    byId = new Map((data.rows || []).map(row => [String(row.id), row]));
    return data;
  }

  function trigger(row) {
    return triggerSync(row, {statusEl: targets.statusEl, after: reload, post, isActive: () => alive});
  }

  function showDetail(row) {
    releaseSheet();
    const body = openSheet(row.source, rowDetail(row));
    if (!body) return;
    const anchor = body.firstElementChild;
    const button = body.querySelector('[data-sync-trigger]');
    const onTrigger = () => { if (alive) void trigger(row); };
    button?.addEventListener('click', onTrigger);
    releaseSheet = () => {
      button?.removeEventListener('click', onTrigger);
      // Do not dismiss a sheet whose content has since been replaced by another feature.
      if (body.isConnected && anchor && body.contains(anchor)) closeSheet?.();
      releaseSheet = () => {};
    };
  }

  function onClick(event) {
    if (!alive) return;
    const target = event.target?.closest ? event.target : event.target?.parentElement;
    if (!target || !root.contains(target)) return;
    const scopeButton = target.closest('[data-sync-scope]');
    if (scopeButton && root.contains(scopeButton)) {
      const scope = scopeButton.dataset.syncScope;
      const scopeMap = {
        all: ['a_stock', 'us_stock', 'a_fund', 'precious_metal'],
        a_stock: ['a_stock'], us_stock: ['us_stock'], a_fund: ['a_fund'], precious_metal: ['precious_metal'],
      };
      void trigger({
        id: `manual-${scope}`,
        source: scopeButton.textContent.trim() || '行情刷新',
        trigger_endpoint: '/api/prices/refresh/investments',
        trigger_body: {scopes: scopeMap[scope] || scopeMap.all},
      });
      return;
    }
    const node = target.closest('[data-sync-row]');
    if (!node || !root.contains(node)) return;
    const row = byId.get(String(node.dataset.syncRow));
    if (!row) return;
    if (target.closest('[data-sync-inline]')) {
      event.stopPropagation();
      void trigger(row);
    } else {
      showDetail(row);
    }
  }

  function dispose() {
    if (!alive) return;
    alive = false;
    reads.dispose();
    generation++;
    root.removeEventListener('click', onClick);
    win?.removeEventListener('pagehide', dispose);
    releaseSheet();
    byId.clear();
    if (mounted.get(root) === controller) mounted.delete(root);
  }

  const controller = {reload, setTargets, dispose, get disposed() { return !alive; }};
  mounted.set(root, controller);
  root.addEventListener('click', onClick);
  win?.addEventListener('pagehide', dispose);
  return controller;
}

export function dispose(root) {
  mounted.get(root)?.dispose();
}
