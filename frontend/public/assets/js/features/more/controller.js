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
  const assetDetails = find('moreAssetManagement');
  const assetPanel = find('moreAssetReview');
  const assetCount = find('assetPendingCount');
  const assetRefresh = find('refreshAssetReview');
  let alive = true;
  let navGrowthLoaded = false;
  let navGrowthPending;
  let assetLoaded = false;
  let assetLoading = false;

  async function loadAssetReview() {
    if (!alive || !assetPanel || assetLoaded || assetLoading) return;
    assetLoading = true;
    assetPanel.textContent = '正在加载待核对资产…';
    try {
      const data = await reads.get('asset-review', '/api/analysis/asset-import-review', {cache: false});
      if (!alive) return;
      if (!Array.isArray(data.rows)) throw new Error('资产数据格式异常');
      assetLoaded = true;
      if (assetCount) assetCount.textContent = data.rows.length ? `· 待补充 ${data.rows.length}` : '';
      assetPanel.replaceChildren();
      if (!data.rows.length) { assetPanel.textContent = '没有待补充的导入资产。'; return; }
      const doc = root.ownerDocument;
      for (const asset of data.rows) {
        const item = doc.createElement('div'), title = doc.createElement('strong');
        title.textContent = `${asset.symbol || '未知代码'} · ${asset.name || '未知名称'}`;
        const evidence = doc.createElement('p'); evidence.textContent = `市场：${asset.market || '未知'} · 交易所：${asset.exchange || '待补充'} · 原件日期：${asset.source_date || '待补充'}`;
        const hint = doc.createElement('p'); hint.textContent = '请核对资产类型及币种（当前流程支持人民币）。';
        const label = doc.createElement('label'); label.textContent = '资产类型 ';
        const select = doc.createElement('select'); select.setAttribute('aria-label', `${asset.symbol} 资产类型`);
        for (const value of ['A股个股', 'ETF / 基金']) {
          const option = doc.createElement('option'); option.value = value; option.textContent = value;
          select.append(option);
        }
        if (['A股个股', 'ETF / 基金'].includes(asset.asset_type)) select.value = asset.asset_type;
        label.append(select);
        const confirm = doc.createElement('button'); confirm.type = 'button'; confirm.textContent = '确认类型与人民币';
        confirm.addEventListener('click', async () => {
          confirm.disabled = true; hint.textContent = '正在保存…';
          try {
            const response = await win.fetch(`/api/analysis/asset-import-review/${encodeURIComponent(asset.asset_id)}`, {
              method: 'POST', headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({expected_updated_at: asset.updated_at, asset_type: select.value, currency: 'CNY'}),
            });
            if (!response.ok) throw new Error('保存失败或资产已变化，请刷新后重试');
            assetLoaded = false; await loadAssetReview();
          } catch (error) { if (alive) { hint.textContent = error.message; confirm.disabled = false; } }
        });
        item.append(title, evidence, hint, label, confirm); assetPanel.append(item);
      }
    } catch (error) {
      if (alive && !isReadCancelled(error)) {
        if (assetCount) assetCount.textContent = '· 状态不可用';
        assetPanel.textContent = `资产待核对信息暂不可用：${error?.message || String(error)}`;
      }
    } finally { assetLoading = false; }
  }

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

  function onAssetToggle() { if (assetDetails.open) void loadAssetReview(); }
  function onAssetRefresh() { assetLoaded = false; void loadAssetReview(); }

  function dispose() {
    if (!alive) return;
    alive = false;
    reads.dispose();
    details?.removeEventListener('toggle', onToggle);
    assetDetails?.removeEventListener('toggle', onAssetToggle);
    assetRefresh?.removeEventListener('click', onAssetRefresh);
    win?.removeEventListener('pagehide', dispose);
    if (mounted.get(root) === controller) mounted.delete(root);
  }

  const controller = {ready: null, dispose, get disposed() { return !alive; }};
  mounted.set(root, controller);
  bootstrap();
  const origin = find('moreOrigin');
  if (origin) origin.textContent = (context.location || win?.location)?.origin || '';
  details?.addEventListener('toggle', onToggle);
  assetDetails?.addEventListener('toggle', onAssetToggle);
  assetRefresh?.addEventListener('click', onAssetRefresh);
  win?.addEventListener('pagehide', dispose);
  if (details?.open) void loadNavGrowthExplanationOnce();
  if (assetDetails?.open) void loadAssetReview();
  else if (assetDetails) void loadAssetReview();
  controller.ready = loadIssues();
  return controller;
}

export function dispose(root) {
  mounted.get(root)?.dispose();
}
