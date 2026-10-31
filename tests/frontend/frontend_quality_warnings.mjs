import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { loadFeatureModule } from './feature_module_loader.mjs';

const panel = { innerHTML: '' };
const module = await loadFeatureModule(
  fileURLToPath(new URL('../../frontend/public', import.meta.url)), 'data_sources.js',
  { document: { getElementById: (id) => id === 'dataQuality' ? panel : null } },
);
const render = module.renderQuality;
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

const issues = module.issuesHtml;
const summary = {status:'warning',warning_count:2,error_count:0};
const data = {summary,checks:[
  {id:'account_balance_coverage',status:'attention',details:{missing_accounts:['a','b']}},
  {id:'valuation_series_freshness',status:'attention',details:{valuation_series_latest_as_of:'2026-06-18',investment_positions_latest_as_of:'2026-10-29'}}
]};
let html = issues(data,[{account_code:'a',display_name:'支付宝'},{account_code:'b',display_name:'<测试>'}]);
assert.match(html,/支付宝、&lt;测试&gt;/); assert.match(html,/2026-06-18/); assert.match(html,/2026-10-29/);
assert.match(html,/accounts.html#overview/); assert.doesNotMatch(html,/同步矩阵|覆盖范围/);
assert.match(issues(null),/暂不可用/); assert.match(issues({summary,checks:[]}),/暂不可用/);
assert.match(issues({summary:{status:'ok',warning_count:0,error_count:0},checks:[]}),/没有待处理/);
assert.match(issues({summary:{status:'error',warning_count:0,error_count:1},checks:[{id:'future',status:'error',message:'<bad>'}]}),/&lt;bad&gt;/);
