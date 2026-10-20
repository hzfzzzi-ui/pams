import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const panel = { innerHTML: '' };
const context = vm.createContext({
  document: { getElementById: (id) => id === 'dataQuality' ? panel : null },
});
const source = readFileSync(new URL('../frontend/public/assets/js/data_sources.js', import.meta.url), 'utf8');
const module = new vm.SourceTextModule(source, { context });
await module.link((specifier) => {
  const exports = specifier.includes('api.js') ? ['jget']
    : specifier.includes('sync_center.js') ? ['loadSyncCenter']
      : ['renderDataState', 'setActiveNav'];
  return new vm.SyntheticModule(exports, function () {
    for (const name of exports) this.setExport(name, () => new Promise(() => {}));
  }, { context });
});
await module.evaluate();
const render = module.namespace.renderQuality;
const coverage = Object.fromEntries([
  'account_balances', 'investment_positions', 'investment_transactions',
  'cash_transactions', 'transfer_matches', 'fx_rates', 'valuation_series',
].map((key) => [key, { rows: 1, has_data: true }]));
const quality = { coverage, freshness: {}, missing: {}, checks: [] };

render({ data_quality: quality });
assert.match(panel.innerHTML, /当前无阻塞动作/);
assert.match(panel.innerHTML, /行动提示<\/span><strong>0/);

quality.checks = [
  { id: 'account_balance_coverage', status: 'attention', severity: 'warning' },
  { id: 'valuation_series_freshness', status: 'attention', severity: 'warning' },
];
render({ data_quality: quality });
assert.doesNotMatch(panel.innerHTML, /当前无阻塞动作/);
assert.match(panel.innerHTML, /行动提示<\/span><strong>2/);
assert.match(panel.innerHTML, /部分账户缺少余额确认/);
assert.match(panel.innerHTML, /估值曲线早于当前持仓/);
assert.match(panel.innerHTML, /账户余额[\s\S]*?待核准/);
assert.match(panel.innerHTML, /估值曲线[\s\S]*?待核准/);

quality.checks = [{ id: 'future_check', status: 'attention', severity: 'error', message: '<script>bad</script>' }];
render({ data_quality: quality });
assert.doesNotMatch(panel.innerHTML, /<script>/);
assert.match(panel.innerHTML, /&lt;script&gt;/);
assert.match(panel.innerHTML, /异常/);

quality.checks = [];
quality.coverage.fx_rates = { rows: 0, has_data: false };
render({ data_quality: quality });
assert.match(panel.innerHTML, /缺数据/);
assert.match(panel.innerHTML, /检查汇率/);
console.log('Quality warning rendering OK');
