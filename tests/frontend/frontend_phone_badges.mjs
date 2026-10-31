import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

let sheetHtml = '';
function element() {
  return { innerHTML: '', children: [], dataset: {}, listeners: {}, classList: { toggle() {} },
    appendChild(node) { this.children.push(node); },
    addEventListener(name, listener) { this.listeners[name] = listener; } };
}
const table = element();
const mobile = element();
const button = element();
button.dataset.mobileHoldingIndex = '0';
mobile.querySelectorAll = () => [button];
const context = vm.createContext({
  URLSearchParams,
  window: { location: { search: '' }, matchMedia: () => ({ matches: false }) },
  document: { querySelectorAll: () => [], createElement: element,
    getElementById: (id) => ({ rows: table, mobileHoldingList: mobile })[id] || null },
});
const {createRequire} = await import('node:module');
const require = createRequire(import.meta.url);
Object.assign(context, {typeLabel: value => value, accountLabel: value => value, pnlPct: () => 0,
  openBottomSheet: (_title, html) => {sheetHtml = html;}});
vm.runInContext(require('./holdings_renderer_fixture.cjs').sourceFor('holdings.js'), context);
const module = {namespace: context};
const badge = module.namespace.holdingDailyStatusBadge;
const row = { daily_pnl_status: 'pending', daily_pnl_status_label: '未刷新', cost_cny: '38000', holding_pnl_cny: '13520.67' };
const before = JSON.stringify(row);
assert.equal(badge(row), '');
assert.equal(JSON.stringify(row), before);
assert.match(badge({ daily_pnl_status: 'estimated', daily_pnl_status_label: '预估收益' }), /预估收益/);
assert.match(badge({ daily_pnl_status: 'confirmed', daily_pnl_status_label: '今日收益已确认' }), /今日收益已确认/);

for (const status of [
  { daily_pnl_status: 'confirmed', daily_pnl_status_label: '今日收益已确认' },
  { daily_pnl_status: 'estimated', daily_pnl_status_label: '预估收益' },
  { daily_pnl_status: 'pending', daily_pnl_status_label: '未刷新' },
  {},
  { daily_return_status: 'confirmed' },
  { daily_pnl_status: 'estimated', daily_pnl_status_label: '<img src=x onerror="bad()">' },
]) {
  const holding = { symbol: 'TEST', name: 'Test holding', account: 'tonghuashun', major_category: 'A股',
    currency: 'CNY', market_value_cny: 1200, cost_cny: 1000, holding_pnl_cny: 250,
    realized_trade_pnl_cny: 50, daily_pnl_cny: 20, daily_total_pnl_cny: 30,
    quantity: 100, current_price: 12, previous_price: 11, buy_price: 10, ...status };
  const inputBefore = JSON.stringify(holding);
  table.children = [];
  module.namespace.renderRows([holding]);
  const detail = table.children[1];
  assert.equal(detail.hidden, true);
  table.children[0].listeners.click();
  assert.equal(detail.hidden, false);
  table.children[0].listeners.click();
  assert.equal(detail.hidden, true);
  button.listeners.click();
  const expected = `<strong>收益状态</strong><span>${badge(holding) || '<span class="muted">—</span>'}</span>`;
  for (const html of [detail.innerHTML, sheetHtml]) {
    assert.equal(html.split('<strong>收益状态</strong>').length - 1, 1);
    assert.ok(html.includes(expected));
    assert.ok(html.indexOf('<strong>今日盈亏</strong>') < html.indexOf('<strong>收益状态</strong>'));
    for (const value of ['¥1,200.00', '¥1,000.00', '+¥200.00', '+¥50.00', '+¥250.00', '+¥30.00']) {
      assert.ok(html.includes(value), `Missing unchanged amount: ${value}`);
    }
    assert.doesNotMatch(html, /<img|未刷新/);
  }
  assert.equal(JSON.stringify(holding), inputBefore);
  assert.doesNotMatch(mobile.innerHTML, /<strong>收益状态<\/strong>|未刷新/);
}
console.log('Phone badges OK');
