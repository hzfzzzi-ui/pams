import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const context = vm.createContext({
  URLSearchParams,
  window: { location: { search: '' } },
  document: { querySelectorAll: () => [], getElementById: () => null },
});
const source = readFileSync(new URL('../frontend/public/assets/js/holdings.js', import.meta.url), 'utf8');
const module = new vm.SourceTextModule(source + '\nexport { holdingDailyStatusBadge };', { context });
await module.link((specifier) => {
  const exports = specifier.includes('api.js') ? ['jget', 'typeLabel', 'accountLabel']
    : specifier.includes('ui.js') ? ['openBottomSheet', 'setActiveNav']
      : specifier.includes('account_cash.js') ? ['makeAccountCashHoldingRows'] : ['pnlPct'];
  return new vm.SyntheticModule(exports, function () {
    for (const name of exports) this.setExport(name, () => new Promise(() => {}));
  }, { context });
});
await module.evaluate();
const badge = module.namespace.holdingDailyStatusBadge;
const row = { daily_pnl_status: 'pending', daily_pnl_status_label: '未刷新', cost_cny: '38000', holding_pnl_cny: '13520.67' };
const before = JSON.stringify(row);
assert.equal(badge(row), '');
assert.equal(JSON.stringify(row), before);
assert.match(badge({ daily_pnl_status: 'estimated', daily_pnl_status_label: '预估收益' }), /预估收益/);
assert.match(badge({ daily_pnl_status: 'confirmed', daily_pnl_status_label: '今日收益已确认' }), /今日收益已确认/);
console.log('Phone badges OK');
