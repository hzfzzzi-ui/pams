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
  // Freshness & trade projection reminders are surfaced in the top-right header reminder center.
  const el = root.querySelector?.('#holdingsFreshnessBanner');
  if (el) {
    el.hidden = true;
    el.innerHTML = '';
  }
}
