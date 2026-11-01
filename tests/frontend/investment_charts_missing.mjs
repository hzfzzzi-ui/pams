import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source = readFileSync('frontend/public/assets/js/features/investment/charts/legacy_model.js', 'utf8')
  .replace(/^import .*;\s*$/gm, '').replace(/^export \{.*\};\s*$/gm, '');
const context = vm.createContext({safeNum: value => Number.isFinite(Number(value)) ? Number(value) : 0});
vm.runInContext(source + '\nglobalThis.model = {buildInvestmentReturnWindowData, normalizeInvestmentNavRows, buildInvestmentNavChartData};', context);
const model = context.model;
const dates = ['2026-01-01', '2026-01-02'];
const missing = dates.map(date => ({date, market_value: null, total_pnl: null, return_rate: null}));
const sections = new Map([['a_stock', missing.map(row => ({...row, market_value: -100, display_return_rate: null}))]]);
const result = model.buildInvestmentReturnWindowData(missing, sections, [{key:'a_stock', label:'A股个股'}], 'ytd', true);
assert.deepEqual(Array.from(result.totalData), [null, null]);
assert.deepEqual(Array.from(result.sectionDataByKey.get('a_stock').data), [null, null]);
assert.equal(result.topContribution.unavailable, true);
assert.equal(result.topContribution.totalPnl, null);
const nav = model.normalizeInvestmentNavRows(dates.map(date => ({date, netWorth:null})));
assert.equal(nav[0].market_value, null);
assert.equal(model.buildInvestmentNavChartData(nav, true).values.length, 0);
const knownZero = model.normalizeInvestmentNavRows([{date:'2026-01-01', netWorth:0}]);
assert.equal(model.buildInvestmentNavChartData(knownZero, true).latest, 0);
