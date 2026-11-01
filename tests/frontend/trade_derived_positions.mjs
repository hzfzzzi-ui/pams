import assert from 'node:assert/strict';
import { renderTradeDerivedPositions } from '../../frontend/public/assets/js/features/investment/holdings/trade_derived_positions.js';

const section = {hidden: true};
const list = {innerHTML: '', textContent: ''};
renderTradeDerivedPositions(section, list, {campaigns: [{
  symbol: '600001', name: '<测试>', open_quantity: '100', warnings: [], unmatched_sell_quantity: '0'
}]});
assert.equal(section.hidden, false);
assert.match(list.innerHTML, /交易推算数量：100 股/);
assert.match(list.innerHTML, /&lt;测试&gt;/);
assert.match(list.innerHTML, /现价、市值及盈亏待更新/);
assert.doesNotMatch(list.innerHTML, /¥0/);
renderTradeDerivedPositions(section, list, {campaigns: [{symbol: '600002', warnings: ['missing'], unmatched_sell_quantity: '2'}]});
assert.match(list.innerHTML, /记录存在缺口，数量待核实/);
renderTradeDerivedPositions(section, list, {campaigns: []});
assert.equal(section.hidden, true);
