import { esc } from './list_legacy_model.js?v=20261031-modules-v1';

// This is a presentation of the server's read-only trade reconstruction, not
// a holding snapshot or a source of portfolio value.
export function renderTradeDerivedPositions(section, list, payload) {
  const rows = Array.isArray(payload?.campaigns) ? payload.campaigns : [];
  if (!rows.length) { section.hidden = true; list.textContent = ''; return; }
  section.hidden = false;
  list.innerHTML = rows.map(row => {
    const quantity = row.open_quantity == null ? '待核实' : String(row.open_quantity);
    const caution = row.warnings?.length || Number(row.unmatched_sell_quantity) > 0
      ? ' · 记录存在缺口，数量待核实' : '';
    return `<div class="mobileHoldingRow"><strong>${esc(row.symbol || '—')} · ${esc(row.name || '未命名')}</strong><br><span>交易推算数量：${esc(quantity)} 股${esc(caution)}</span><br><span class="muted">现价、市值及盈亏待更新</span></div>`;
  }).join('');
}
