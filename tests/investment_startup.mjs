import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../frontend/public/assets/js/investment_startup.js', import.meta.url), 'utf8');
const settle = () => new Promise(resolve => setImmediate(resolve));
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

async function setup(investment = true) {
  const timers = new Map(), observers = new Set();
  const document = { body: { dataset: { investTab: 'analysis' } }, querySelector: () => investment ? {} : null };
  let nextTimer = 0;
  class MutationObserver {
    constructor(callback) { this.callback = callback; }
    observe() { observers.add(this); }
    disconnect() { observers.delete(this); }
  }
  const context = vm.createContext({ document, MutationObserver,
    setTimeout: (callback, ms) => { const id = ++nextTimer; timers.set(id, { callback, ms }); return id; },
    clearTimeout: id => timers.delete(id),
  });
  const module = new vm.SourceTextModule(source, { context });
  await module.link(() => { throw new Error('Startup scheduling must not import APIs or financial helpers'); });
  await module.evaluate();
  return { api: module.namespace, timers, observers, document,
    tab(name) { document.body.dataset.investTab = name; for (const o of [...observers]) o.callback(); },
    expire() { for (const timer of [...timers.values()]) timer.callback(); },
  };
}

{
  const { api, timers } = await setup();
  const value = { amount: '123.456', rate: '-0.0123', status: 'last_valid' };
  assert.equal(await api.afterInvestmentNav(() => value), value);
  assert.equal(await api.afterInvestmentStartup(() => value), value);
  assert.equal(timers.size, 0);
}
{
  const { api, timers, observers } = await setup();
  const nav = deferred(), holdings = deferred(), calls = [];
  assert.equal(api.trackInvestmentNav(nav.promise), nav.promise);
  const initial = api.afterInvestmentNav(() => { calls.push('holdings'); return holdings.promise; });
  api.trackInvestmentHoldings(initial);
  const background = api.afterInvestmentStartup(() => calls.push('background'), 'risk');
  const returns = api.afterInvestmentNav(() => calls.push('returns'));
  await settle(); assert.deepEqual(calls, []);
  nav.resolve(); await settle();
  assert.deepEqual(calls, ['holdings', 'returns']);
  assert.equal(observers.size, 1);
  holdings.resolve(); await initial; await background; await returns;
  assert.deepEqual(calls, ['holdings', 'returns', 'background']);
  assert.equal(timers.size, 0); assert.equal(observers.size, 0);
}
for (const type of ['resolve', 'reject']) {
  const { api, timers, observers } = await setup();
  const nav = deferred(); api.trackInvestmentNav(nav.promise);
  let calls = 0;
  const background = api.afterInvestmentStartup(() => ++calls, 'risk');
  nav[type](new Error('read unavailable')); await background;
  assert.equal(calls, 1); assert.equal(timers.size, 0); assert.equal(observers.size, 0);
}
{
  const { api } = await setup();
  const old = deferred(), retry = deferred();
  api.trackInvestmentNav(old.promise); api.trackInvestmentNav(retry.promise);
  let calls = 0; const loading = api.afterInvestmentNav(() => ++calls);
  old.resolve(); await settle(); assert.equal(calls, 0);
  retry.resolve(); await loading; assert.equal(calls, 1);
}
for (const tab of ['risk', 'allocation']) {
  const test = await setup(), nav = deferred(); test.api.trackInvestmentNav(nav.promise);
  let calls = 0;
  const loading = test.api.afterInvestmentStartup(() => ++calls, tab);
  await settle(); assert.equal(calls, 0);
  test.tab(tab); await loading; assert.equal(calls, 1);
  assert.equal(test.timers.size, 0); assert.equal(test.observers.size, 0);
  nav.resolve(); test.expire(); await settle(); assert.equal(calls, 1);
}
{
  const test = await setup(), nav = deferred(); test.api.trackInvestmentNav(nav.promise);
  let calls = 0;
  const loading = test.api.afterInvestmentNav(() => ++calls, 'holdings');
  await settle(); assert.equal(calls, 0);
  test.tab('holdings'); await loading; assert.equal(calls, 1);
  assert.equal(test.timers.size, 0); assert.equal(test.observers.size, 0);
  nav.resolve(); test.expire(); await settle(); assert.equal(calls, 1);
}
{
  const test = await setup(), nav = deferred(); test.api.trackInvestmentNav(nav.promise);
  test.tab('risk'); let calls = 0;
  await test.api.afterInvestmentStartup(() => ++calls, 'risk');
  assert.equal(calls, 1); assert.equal(test.observers.size, 0); assert.equal(test.timers.size, 0);
  nav.resolve();
}
for (const navOnly of [false, true]) {
  const test = await setup(), nav = deferred(); test.api.trackInvestmentNav(nav.promise);
  let calls = 0;
  const loading = navOnly ? test.api.afterInvestmentNav(() => ++calls) : test.api.afterInvestmentStartup(() => ++calls, 'risk');
  assert.equal([...test.timers.values()][0].ms, 10000);
  test.expire(); await loading;
  assert.equal(calls, 1); assert.equal(test.timers.size, 0); assert.equal(test.observers.size, 0);
  nav.resolve(); await settle(); assert.equal(calls, 1);
}
{
  const { api } = await setup(false), nav = deferred(); api.trackInvestmentNav(nav.promise);
  assert.equal(await api.afterInvestmentStartup(() => 'unchanged non-investment page'), 'unchanged non-investment page');
  nav.resolve();
}
{
  const { api, timers } = await setup(), nav = deferred(); api.trackInvestmentNav(nav.promise);
  const loading = api.afterInvestmentNav(() => { throw new Error('callback failed'); });
  nav.resolve(); await assert.rejects(loading, /callback failed/); assert.equal(timers.size, 0);
}
for (const directTrade of [true, false]) {
  const events = [], queued = [];
  const window = { location: { hash: directTrade ? '#trade-attribution-analysis' : '#return-trend' },
    dispatchEvent: event => events.push(event.type) };
  const context = vm.createContext({ window, CustomEvent: class { constructor(type) { this.type = type; } },
    document: { body: { dataset: { investTab: 'analysis' } },
      getElementById: id => id === 'return-attribution' ? {} : null,
      querySelectorAll: () => [] },
  });
  const text = readFileSync(new URL('../frontend/public/assets/js/lookthrough.js', import.meta.url), 'utf8');
  const module = new vm.SourceTextModule(text.slice(0, text.lastIndexOf('bindEvents();')) + `
    export { scheduleVisibleAnalysisLoad };
    export function selectTradePanel() { activeAnalysisPanel = 'trade_attribution'; return loadActiveAnalysisPanel(); }
  `, { context });
  await module.link(specifier => {
    const exports = specifier.includes('investment_startup.js') ? {
      afterInvestmentNav: task => { queued.push(task); return Promise.resolve(); },
    } : specifier.includes('investment_requests.js') ? { loadInvestmentSummaryFresh() {} }
      : specifier.includes('api.js') ? { jget() { throw new Error('Unexpected data read'); } }
        : { openBottomSheet() {} };
    return new vm.SyntheticModule(Object.keys(exports), function () {
      for (const [key, value] of Object.entries(exports)) this.setExport(key, value);
    }, { context });
  });
  await module.evaluate();
  module.namespace.scheduleVisibleAnalysisLoad();
  if (!directTrade) await module.namespace.selectTradePanel();
  assert.deepEqual(events, ['pams:load-trade-attribution']);
  for (const task of queued) await task();
  assert.deepEqual(events, ['pams:load-trade-attribution'], 'NAV completion must not repeat the independent transaction loader');
}
await settle();
console.log('Investment startup: dependency ordering, tab bypass, failures, timeout, retries and cleanup OK');
