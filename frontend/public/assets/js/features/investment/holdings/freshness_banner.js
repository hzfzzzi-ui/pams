import { jget, jpost } from '../../../api.js';

function esc(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function fmtQty(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return '--';
  return Math.abs(num - Math.round(num)) < 1e-4
    ? Math.round(num).toLocaleString('zh-CN')
    : num.toFixed(2);
}

export function mountFreshnessBanner(root = document.body) {
  const el = root.querySelector?.('#holdingsFreshnessBanner');
  if (!el) return;

  let refreshing = false;
  let lastData = null;

  function render(data, statusNote = '') {
    lastData = data;
    const stale = data?.stale_items || [];
    const ths = data?.ths_projected_adjustments || [];
    if (!stale.length && !ths.length && !statusNote) {
      el.hidden = true;
      el.innerHTML = '';
      return;
    }
    const parts = [];
    if (stale.length) {
      const summary = stale.map((item) => `${item.label}（${item.as_of}，落后${item.trading_days_stale}个交易日）`).join(' · ');
      parts.push(`
        <div class="freshnessCard is-warn">
          <div class="freshnessCardBody">
            <strong>行情 / 日结落后提醒</strong>
            <small>${esc(summary)}</small>
            ${statusNote ? `<em class="freshnessStatusNote">${esc(statusNote)}</em>` : ''}
          </div>
          <button type="button" class="freshnessRefreshBtn" data-freshness-refresh ${refreshing ? 'disabled' : ''}>
            ${refreshing ? '刷新中…' : '一键刷新行情'}
          </button>
        </div>
      `);
    }
    if (ths.length) {
      const lines = ths.slice(0, 3).map((item) => {
        const sign = Number(item.delta_qty) >= 0 ? '+' : '';
        return `${item.name} ${sign}${fmtQty(item.delta_qty)} 股 → 推演 ${fmtQty(item.projected_qty)} 股（含 ${item.latest_trade_date} 成交）`;
      }).join('；');
      parts.push(`
        <div class="freshnessCard is-info">
          <div class="freshnessCardBody">
            <strong>同花顺成交推演（较快照 ${esc(ths[0].snapshot_as_of || '--')}）</strong>
            <small>${esc(lines)}</small>
          </div>
        </div>
      `);
    }
    el.innerHTML = parts.join('');
    el.hidden = false;
  }

  el.addEventListener('click', async (event) => {
    const btn = event.target?.closest?.('[data-freshness-refresh]');
    if (!btn || refreshing) return;
    refreshing = true;
    render(lastData, '正在触发行情刷新…');
    try {
      const res = await jpost('/api/prices/refresh/investments', {
        scopes: ['a_stock', 'us_stock', 'a_fund', 'precious_metal'],
      });
      render(lastData, `行情已更新 ${Number(res?.total_updated || 0)} 项，正在刷新持仓估值…`);
      try {
        await jpost('/api/investment/page-projection/refresh', {});
      } catch { /* fallback to normal reload */ }
      refreshing = false;
      const fresh = await jget('/api/prices/freshness');
      render(fresh, '行情与持仓估值已更新，正在刷新页面…');
      window.setTimeout(() => window.location.reload(), 600);
    } catch (err) {
      refreshing = false;
      render(lastData, `刷新失败：${err?.message || err}`);
    }
  });

  jget('/api/prices/freshness')
    .then((data) => render(data))
    .catch(() => {});
}
