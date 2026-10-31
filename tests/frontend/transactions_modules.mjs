import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import vm from 'node:vm';
import {options, rows, ledger, attribution, tradeAttribution} from './fixtures/transactions_lifecycle.mjs';

const publicRoot = path.resolve(process.argv[2] || fileURLToPath(new URL('../../frontend/public', import.meta.url)));
const context = vm.createContext({AbortController, URLSearchParams});
const modules = new Map();
function moduleAt(file) {
  if (!modules.has(file)) modules.set(file, new vm.SourceTextModule(readFileSync(file, 'utf8'), {context, identifier: file}));
  return modules.get(file);
}
async function load(name) {
  const module = moduleAt(path.join(publicRoot, 'assets/js/features/transactions', `${name}.js`));
  if (module.status === 'unlinked') await module.link((specifier, parent) => moduleAt(path.resolve(path.dirname(parent.identifier), specifier.split('?')[0])));
  if (module.status === 'linked') await module.evaluate();
  return module.namespace;
}
const {createLegacyReview} = await load('legacy_review');
const {createListView} = await load('list_view');
const {createAttributionView} = await load('attribution_view');
const {createEditor} = await load('editor');
const {createCommands} = await load('commands');
const {createScope} = await load('scope');
const domModule = moduleAt(path.join(publicRoot, 'assets/js/shared/dom_view.js'));
await domModule.link(() => {throw new Error('DOM helper must have no dependencies');});
await domModule.evaluate();
const {createDomView} = domModule.namespace;
const leftError = {style: {}, textContent: ''}, rightError = {style: {}, textContent: ''};
const leftDom = createDomView({querySelector: selector => selector === '[id="err"]' ? leftError : null});
const rightDom = createDomView({querySelector: selector => selector === '[id="err"]' ? rightError : null});
leftDom.showError(new Error('左侧错误')); rightDom.setText('err', '右侧保留');
assert.equal(leftError.textContent, '左侧错误'); assert.equal(rightError.textContent, '右侧保留');
leftDom.clearError(); assert.equal(leftError.style.display, 'none'); assert.equal(rightError.textContent, '右侧保留');
const documentDom = createDomView({getElementById: id => id === 'err' ? leftError : null});
documentDom.showError('文档错误'); assert.equal(leftError.textContent, '文档错误');
documentDom.setText('err', 0); assert.equal(leftError.textContent, 0);
const emptyDom = createDomView(null);
assert.equal(emptyDom.byId('err'), null); assert.equal(emptyDom.query('#err'), null);
emptyDom.setText('err', '忽略'); emptyDom.showError('忽略'); emptyDom.clearError();
const nodes = new Map();
const $ = id => {if (!nodes.has(id)) nodes.set(id, {value: '', innerHTML: '', textContent: '', dataset: {}, style: {}, hidden: false}); return nodes.get(id);};
const state = {accounts: options.accounts, assets: options.assets, reviewOptions: options.review_options, rows, summary: ledger.summary, filters: {start: '', end: '', side: '', assetKey: '', search: ''}, showAll: false, campaigns: null};
const scope = {byId: $, alive: true, isCurrentForm: () => true, openSheet() {}, closeSheet() {}, frame() {}, root: {classList: {remove() {}}}, query() {return null;}};
const legacy = createLegacyReview({state, scope});
const list = createListView({state, scope, legacy});
const view = createAttributionView({state, scope, legacy});
const plain = value => JSON.parse(JSON.stringify(value));
assert.deepEqual(plain(legacy.defaultFilterState()), {start: '', end: legacy.todayIso(), side: '', assetKey: '', search: ''});
assert.equal(legacy.sideLabel('buy'), '买入'); assert.equal(legacy.sideLabel('sell'), '卖出');
assert.equal(legacy.sideLabel('fx_conversion'), '换汇');
assert.equal(legacy.identityRule('uncertain').maxPositionPct, '1.5');
assert.equal(legacy.identityRule('allocation').sellRuleType, 'allocation_rebalance');
assert.equal(legacy.identityRule('closed').sellRuleType, 'closed');
list.renderTransactionSummary(ledger.summary, rows);
let html = $('transactionSummaryPanel').innerHTML;
assert.equal((html.match(/class="recentTradeItem tradeLedgerItem"/g) || []).length, 10);
for (const value of ['展开更多（剩余 2 单）', '个人买入', '个人卖出', '个人税费', '合成账户', '合成资产 1', '买入', '卖出', '份数 2', '税费 CNY 0.50', '个人口径', 'data-trade-review="1"']) assert.ok(html.includes(value), value);
assert.doesNotMatch(html, /最近10单/);
state.showAll = true; list.renderTransactionSummary(ledger.summary, rows);
assert.equal(($('transactionSummaryPanel').innerHTML.match(/class="recentTradeItem tradeLedgerItem"/g) || []).length, 12);
assert.match($('transactionSummaryPanel').innerHTML, /收起/);
state.filters = {start: '2026-01-01', end: '2026-10-31', side: 'sell', assetKey: 'fixture-a', search: '合成 资产'};
assert.equal(list.buildQuery(), 'start=2026-01-01&end=2026-10-31&side=sell&asset_key=fixture-a&search=%E5%90%88%E6%88%90+%E8%B5%84%E4%BA%A7&limit=1000');
assert.match(list.TradeFilterSheet(), /全部标的/); assert.match(list.TradeFilterSheet(), /合成资产 A/); assert.match(list.TradeFilterSheet(), /换汇/);
html = view.TradeAttributionPanelV2(attribution);
for (const value of ['data-asset-heat-select="fixture-a"', 'data-asset-heat-select="fixture-b"', '合成资产 A', '合成资产 B', '合成买入贡献', '合成卖出贡献', 'data-direction-side="buy"', 'data-direction-side="sell"', 'data-trade-review="2"']) assert.ok(html.includes(value), value);
assert.match(view.TradeAutoAttributionPanelV3(tradeAttribution(1)), /合成结论 1/);

// The save payload is a projection of form values; validation/rules are the
// existing compatibility functions, not duplicated financial logic in tests.
const values = {
  reviewNote: '合成备注', reviewIdentityConfidence: 'high', reviewBuyLogic: '合成逻辑', reviewAddCondition: '合成加仓', reviewReduceCondition: '合成减仓', reviewSellCondition: '合成退出', reviewMaxLossPct: '5.5', reviewMaxPositionPct: '12', reviewNextReviewDate: '2026-12-01', reviewSellRuleType: 'value_thesis', reviewTransitionType: 'research_update', reviewTransitionEvidence: '合成新证据',
};
for (const [id, value] of Object.entries(values)) $(id).value = value;
$('identityTransitionSection').hidden = false;
const choices = {reviewBuyReason: 'research', reviewSellReason: 'reduce', reviewInitialIdentity: 'allocation', reviewCurrentIdentity: 'value', reviewHoldingPeriod: '6-12个月'};
const button = {disabled: false};
const form = {dataset: {tradeId: '2', side: 'sell'}, querySelector(selector) {
  if (selector === 'button[type="submit"]') return button;
  const name = selector.match(/name="([^"]+)"/)?.[1]; return choices[name] ? {value: choices[name]} : null;
}, querySelectorAll(selector) {
  if (selector.includes('name="reviewTag"')) return [{value: 'disciplined'}, {value: 'research_complete'}];
  if (selector.includes('name="identityTransitionTrigger"')) return [{value: 'new_evidence'}];
  return [];
}};
let resolveWrite; const writes = [], reads = [];
const commands = createCommands({scope, legacy,
  post: (url, payload) => {writes.push({url, payload: plain(payload)}); return new Promise(resolve => {resolveWrite = resolve;});},
  loadOptions: async () => reads.push('options'), loadLedger: async () => reads.push('ledger'),
});
const pending = commands.saveTradeReview(form); await commands.saveTradeReview(form);
assert.equal(writes.length, 1); assert.equal(button.disabled, true);
assert.deepEqual(writes[0], {url: '/api/investment-transactions/2/review', payload: {
  buy_reason: 'research', sell_reason: 'reduce', review_note: '合成备注', review_tags: ['disciplined', 'research_complete'], initial_position_identity: 'allocation', current_position_identity: 'value', identity_confidence: 'high', intended_holding_period: '6-12个月', buy_logic: '合成逻辑', add_condition: '合成加仓', reduce_condition: '合成减仓', sell_condition: '合成退出', max_acceptable_loss_pct: 5.5, max_position_pct: 12, next_review_date: '2026-12-01', sell_rule_type: 'value_thesis', identity_transition_type: 'research_update', identity_transition_triggers: ['new_evidence'], identity_transition_evidence: '合成新证据',
}});
scope.isCurrentForm = () => false; scope.alive = false; resolveWrite({}); await pending;
assert.deepEqual(reads, []);

// Editor requests may finish in any order. A stale open or field response must
// not replace a newer editor, including failure fallback rendering.
const editorReads = [], renders = [];
let activeForm = {dataset: {tradeId: '2'}}, version = 0;
const editorScope = {alive: true, get sheetVersion() {return version;}, byId: () => activeForm,
  isCurrentForm: form => form === activeForm, openSheet: () => {version++;}};
const editor = createEditor({state, scope: editorScope, legacy, attribution: {...view, renderTradeAutoAttribution: value => renders.push(value)},
  get: url => new Promise((resolve, reject) => editorReads.push({url, resolve, reject})),
});
const oldForm = activeForm;
const oldRead = editor.loadTradeAttribution(2, oldForm);
activeForm = {dataset: {tradeId: '1'}};
editorReads[0].resolve(tradeAttribution(2, '旧交易结论')); await oldRead;
assert.equal(renders.length, 0);
const currentRead = editor.loadTradeAttribution(1, activeForm);
editorReads[1].resolve(tradeAttribution(1, '当前交易结论')); await currentRead;
assert.equal(renders[0].auto_review.label, '当前交易结论');
const opening = editor.openTradeReviewById('not-in-list'); editorScope.alive = false;
editorReads[2].resolve(tradeAttribution('not-in-list')); await opening;
assert.equal(version, 0);

// Root listeners and delayed startup probes belong to one scope and are removed.
const listeners = new Map(), timers = new Map(), frames = new Map(); let next = 0;
const host = {contains: node => node === host, querySelector: () => null,
  addEventListener: (type, fn) => listeners.set(type, fn), removeEventListener: type => listeners.delete(type)};
const win = {addEventListener: (type, fn) => listeners.set('win:'+type, fn), removeEventListener: type => listeners.delete('win:'+type),
  setTimeout: fn => {const id = ++next; timers.set(id, fn); return id;}, clearTimeout: id => timers.delete(id),
  requestAnimationFrame: fn => {const id = ++next; frames.set(id, fn); return id;}, cancelAnimationFrame: id => frames.delete(id)};
const owned = createScope(host, {win, openSheet() {}, closeSheet() {}});
owned.on('click', () => {}); owned.onWindow('hashchange', () => {}); owned.delay(() => {}, 300); owned.frame(() => {});
owned.dispose(); assert.equal(listeners.size, 0); assert.equal(timers.size, 0); assert.equal(frames.size, 0);
console.log('Transactions modules: root/document/null DOM helpers, 10/expanded rows, Chinese directions, asset attribution fields, exact 19-field write, submit deduplication, stale editor protection and cleanup passed.');
