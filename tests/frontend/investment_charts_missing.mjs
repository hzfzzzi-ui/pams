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
const alignedDates = ['2026-01-01', '2026-01-02', '2026-01-03', '2026-01-04'];
const alignedSections = new Map([
  ['a_stock', alignedDates.map((date, index) => ({date, display_return_rate: index / 4}))],
  ['us_stock', [
    {date: alignedDates[0], display_return_rate: '0'},
    {date: alignedDates[1], display_return_rate: '0'},
    {date: alignedDates[2], display_return_rate: '0.25'},
  ]],
]);
const aligned = model.buildInvestmentReturnWindowData([], alignedSections,
  [{key:'a_stock', label:'A股个股'}, {key:'us_stock', label:'美股'}], 'ytd', true);
assert.deepEqual(Array.from(aligned.labels), alignedDates);
assert.deepEqual(Array.from(aligned.sectionDataByKey.get('us_stock').data), [0, 0, 25, null]);

const nav = model.normalizeInvestmentNavRows(dates.map(date => ({date, netWorth:null})));
assert.equal(nav[0].market_value, null);
assert.equal(model.buildInvestmentNavChartData(nav, true).values.length, 0);
const knownZero = model.normalizeInvestmentNavRows([{date:'2026-01-01', netWorth:0}]);
assert.equal(model.buildInvestmentNavChartData(knownZero, true).latest, 0);
const partial = model.normalizeInvestmentNavRows([
  {date:'2026-01-01',netWorth:null,availableNetWorth:'100',available_accounts:['bank']},
  {date:'2026-01-02',netWorth:null},
  {date:'2026-01-03',netWorth:null,availableNetWorth:'160',available_accounts:['broker','bank']},
]);
const chart = model.buildInvestmentNavChartData(partial, true);
assert.deepEqual(Array.from(chart.values), [100,null,160]);
assert.deepEqual(Array.from(chart.scopes), ['partial:bank','complete','partial:bank,broker']);
assert.equal(chart.latest,160);
assert.equal(chart.availableAccountCount,2);
assert.equal(chart.change,null);
const estimate = new Map([['a_stock',{rows:[
  {date:'2026-01-01', display_return_rate:'0.1'},
  {date:'2026-01-03', display_return_rate:'0.2'},
]}]]);
const estimatedWindow = model.buildInvestmentReturnWindowData(missing, sections,
  [{key:'a_stock',label:'A股个股'}], 'ytd', true, [], estimate);
assert.deepEqual(Array.from(estimatedWindow.labels), ['2026-01-01','2026-01-02','2026-01-03']);
assert.deepEqual(Array.from(estimatedWindow.totalData), [null,null,null]);
assert.deepEqual(Array.from(estimatedWindow.sectionDataByKey.get('a_stock').data), [10,null,20]);
assert.equal(estimatedWindow.topContribution.unavailable, true);
const pnlRows = [
  {date:'2026-01-01', display_return_rate:'0.1', total_pnl:'-15.50'},
  {date:'2026-01-02', display_return_rate:'0.15', total_pnl:'-22.25'},
  {date:'2026-01-03', display_return_rate:'0.2', total_pnl:'-45.75'},
];
estimate.set('a_stock', {label:'同花顺估算', rows:pnlRows});
const partialTop = () => model.buildInvestmentReturnWindowData(missing, sections,
  [{key:'a_stock',label:'A股个股'}], 'ytd', true, [], estimate).topContribution;
assert.equal(partialTop().partial, true);
assert.equal(partialTop().unavailable, false);
assert.equal(partialTop().totalPnl, -30.25);
assert.equal(partialTop().losers[0].account, '同花顺估算');
assert.equal(partialTop().losers[0].weight, null);
assert.equal(partialTop().unattributed, null);
pnlRows[1].total_pnl = null;
assert.equal(partialTop().unavailable, true);
