import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import vm from 'node:vm';

const jsRoot = fileURLToPath(new URL('../../frontend/public/assets/js/', import.meta.url));
const settle = () => new Promise(resolve => setImmediate(resolve));
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => {resolve = yes; reject = no;});
  return {promise, resolve, reject};
}
class Events {
  listeners = new Map();
  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type).add(listener);
  }
  removeEventListener(type, listener) {this.listeners.get(type)?.delete(listener);}
  dispatchEvent(event) {for (const listener of [...(this.listeners.get(event.type) || [])]) listener(event);}
  listenerCount() {return [...this.listeners.values()].reduce((sum, set) => sum + set.size, 0);}
}
function element() {return {innerHTML: '', textContent: '', className: '', dataset: {}, classList: {toggle() {}}, setAttribute() {}};}
const payload = {source: 'holdings_page', as_of: '2026-10-30', total_pnl_cny: 123.456,
  dimensions: {holding: [{symbol: 'TEST', name: 'Synthetic asset', pnl_cny: 123.456, market_value_cny: 1000, daily_price_pnl_cny: null, currency: 'CNY'}]},
};
const summary = {children: [{symbol: 'TEST', daily_pnl: 55.25}]};

async function setup(tab = 'analysis') {
  const root = new Events(), host = new Events(), observers = new Set(), queued = [], sheets = [];
  root.body = {dataset: {investTab: tab}};
  root.elements = new Map(['return-attribution', 'returnAttributionSummary', 'returnAttributionTabs', 'returnAttributionList', 'returnAttributionBadge'].map(id => [id, element()]));
  root.getElementById = id => root.elements.get(id) || null;
  root.querySelector = () => null;
  root.querySelectorAll = () => [];
  root.contains = () => true;
  host.location = {hash: ''}; host.localStorage = {getItem() {return null;}, setItem() {}};
  host.CustomEvent = class {constructor(type) {this.type = type;}};
  host.MutationObserver = class {
    constructor(callback) {this.callback = callback;}
    observe() {observers.add(this);}
    disconnect() {observers.delete(this);}
  };
  const context = vm.createContext({document: root, window: host, AbortController, console});
  const modules = new Map();
  const loader = (specifier, ref) => {
    const filename = path.resolve(path.dirname(ref.identifier), specifier.split('?')[0]);
    if (modules.has(filename)) return modules.get(filename);
    let exports;
    if (filename.endsWith('api.js')) exports = {jget() {throw new Error('Uninjected read');}};
    if (filename.endsWith('investment_requests.js')) exports = {loadInvestmentSummaryFresh() {throw new Error('Uninjected summary');}};
    if (filename.endsWith('investment_startup.js')) exports = {afterInvestmentNav: task => {queued.push(task); return Promise.resolve();}};
    if (filename.endsWith('bottom_sheet.js')) exports = {createBottomSheet() {
      const sheet = {closed: 0, open() {}, dispose() {this.closed++;}}; sheets.push(sheet); return sheet;
    }};
    const result = exports ? new vm.SyntheticModule(Object.keys(exports), function () {
      for (const [key, value] of Object.entries(exports)) this.setExport(key, value);
    }, {context, identifier: filename}) : new vm.SourceTextModule(readFileSync(filename, 'utf8'), {context, identifier: filename});
    modules.set(filename, result);
    return result;
  };
  const entry = new vm.SourceTextModule(`
    export {createAnalysisController} from './features/analysis/controller.js';
    export {mountAnalysisPage} from './features/analysis/page.js';
    export {createAnalysisRequests} from './features/analysis/requests.js';
  `, {context, identifier: path.join(jsRoot, 'analysis-test.js')});
  await entry.link(loader); await entry.evaluate();
  const requests = {lookthrough: () => Promise.resolve({exposures: {}}), attribution: () => Promise.resolve(payload), holdingDaily: () => Promise.resolve(summary)};
  return {root, host, observers, queued, sheets, requests, api: entry.namespace,
    flushNav: async () => {for (const task of queued.splice(0)) await task(); await settle();},
    tab(name) {root.body.dataset.investTab = name; for (const observer of [...observers]) observer.callback();},
    click(selector, dataset) {root.dispatchEvent({type: 'click', target: {dataset, closest: input => input === selector ? {dataset} : null}});},
  };
}

{
  const test = await setup(); const paths = [];
  const reads = test.api.createAnalysisRequests({get: url => {paths.push(url); return Promise.resolve({});}, summary: () => {paths.push('shared-summary'); return Promise.resolve({});}});
  await reads.lookthrough(); await reads.attribution(); await reads.holdingDaily();
  assert.deepEqual(paths, ['/api/analysis/lookthrough?baseCurrency=CNY', '/api/analysis/return-attribution?baseCurrency=CNY', 'shared-summary']);
}
{
  const test = await setup('holdings'); let reads = 0; const events = [];
  test.host.addEventListener('pams:load-trade-attribution', event => events.push(event.type));
  const controller = test.api.createAnalysisController({requests: {...test.requests, attribution: () => {reads++; return Promise.resolve(payload);}}});
  controller.init(); controller.init();
  assert.equal(test.root.listenerCount(), 3); assert.equal(test.observers.size, 1); assert.equal(reads, 0);
  test.tab('analysis'); assert.equal(test.queued.length, 1); assert.equal(reads, 0);
  test.click('[data-analysis-panel-switch]', {analysisPanelSwitch: 'trade_attribution'});
  await test.flushNav(); assert.deepEqual(events, ['pams:load-trade-attribution']); assert.equal(reads, 0);
  controller.dispose(); controller.dispose();
  assert.equal(test.root.listenerCount(), 0); assert.equal(test.observers.size, 0); assert.equal(test.sheets[0].closed, 1);
}
{
  const test = await setup(); const oldAttribution = deferred(), oldSummary = deferred();
  const old = test.api.createAnalysisController({requests: {...test.requests, attribution: () => oldAttribution.promise, holdingDaily: () => oldSummary.promise}});
  old.init(); const oldLoad = old.loadReturnAttributionData();
  const replacement = test.api.createAnalysisController({requests: test.requests}); replacement.init();
  await replacement.loadReturnAttributionData();
  const html = test.root.getElementById('returnAttributionSummary').innerHTML;
  assert.match(html, /55\.3/);
  oldAttribution.resolve({...payload, total_pnl_cny: 999999}); oldSummary.resolve({children: [{symbol: 'TEST', daily_pnl: 999999}]});
  await assert.rejects(oldLoad, error => error.name === 'AbortError'); await test.flushNav();
  assert.equal(old.state.returnAttributionData, null); assert.equal(old.state.returnAttributionHoldingDailySummary, null);
  assert.equal(test.root.getElementById('returnAttributionSummary').innerHTML, html);
  replacement.dispose(); assert.equal(test.observers.size, 0);
}
{
  const test = await setup(); const pending = deferred();
  const controller = test.api.createAnalysisController({requests: {...test.requests, attribution: () => pending.promise}});
  controller.init(); const load = controller.loadReturnAttributionData();
  test.tab('risk'); pending.resolve(payload); await load;
  assert.equal(test.root.getElementById('returnAttributionSummary').innerHTML, '', 'hidden panel receives no late paint');
  test.tab('analysis'); await test.flushNav();
  assert.match(test.root.getElementById('returnAttributionSummary').innerHTML, /55\.3/);
  controller.dispose();
}
{
  const test = await setup(); let attempts = 0;
  const controller = test.api.createAnalysisController({requests: {...test.requests, attribution: () => ++attempts === 1 ? Promise.reject(new Error('synthetic unavailable')) : Promise.resolve(payload)}});
  await assert.rejects(controller.loadReturnAttributionData(), /synthetic unavailable/);
  assert.match(test.root.getElementById('returnAttributionList').innerHTML, /synthetic unavailable/);
  await controller.loadReturnAttributionData();
  assert.match(test.root.getElementById('returnAttributionSummary').innerHTML, /123\.46/); assert.equal(attempts, 2);
  controller.dispose();
}
{
  const test = await setup(); const pending = deferred();
  const old = test.api.createAnalysisController({requests: {...test.requests, attribution: () => pending.promise}});
  const loading = old.loadReturnAttributionData(); old.dispose();
  const replacement = test.api.createAnalysisController({requests: test.requests}); await replacement.loadReturnAttributionData();
  const html = test.root.getElementById('returnAttributionList').innerHTML;
  pending.reject(new Error('old unavailable')); await assert.rejects(loading, error => error.name === 'AbortError');
  assert.equal(test.root.getElementById('returnAttributionList').innerHTML, html);
  replacement.dispose();
}
{
  const test = await setup(); let reads = 0;
  const page = test.api.mountAnalysisPage({requests: {...test.requests, attribution: () => {reads++; return Promise.resolve(payload);}}});
  assert.equal(test.api.mountAnalysisPage(), page, 'same root reuses its mounted page');
  await test.flushNav(); assert.equal(reads, 1);
  const old = page.controller;
  Object.assign(old.state, {activeDimension: 'currency', activeThemeFamily: '科技成长', underlyingSort: 'source', activeReturnAttributionDimension: 'underlying', returnAttributionShowAll: true});
  test.host.dispatchEvent({type: 'pagehide', persisted: true});
  assert.equal(test.root.listenerCount(), 0); assert.equal(test.observers.size, 0);
  test.host.dispatchEvent({type: 'pageshow', persisted: true});
  const restoredController = page.controller;
  test.host.dispatchEvent({type: 'pageshow', persisted: true});
  assert.equal(page.controller, restoredController, 'duplicate restore must not replace the active controller');
  assert.notEqual(page.controller, old); await test.flushNav(); assert.equal(reads, 2);
  const restored = page.controller.state;
  assert.deepEqual([restored.activeDimension, restored.activeThemeFamily, restored.underlyingSort, restored.activeReturnAttributionDimension, restored.returnAttributionShowAll], ['currency', '科技成长', 'source', 'underlying', true]);
  test.host.dispatchEvent({type: 'pagehide', persisted: false});
  assert.equal(test.host.listenerCount(), 0); assert.equal(test.root.listenerCount(), 0); assert.equal(test.observers.size, 0);
}
assert.equal(payload.total_pnl_cny, 123.456);
console.log('Analysis modules: exact request paths, NAV/trade ordering, idempotent root lifecycle, stale success/failure, hidden panels, retry and BFCache view restoration OK');
