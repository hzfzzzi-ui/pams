import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const root = new URL('../../frontend/public/assets/js/', import.meta.url);
const source = readFileSync(new URL('holdings_categories.js', root), 'utf8');
const settle = () => new Promise(resolve => setImmediate(resolve));
const plain = value => JSON.parse(JSON.stringify(value));
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

async function chartModule(fallback = false) {
  const requests = [], charts = [], storage = new Map();
  const drawing = new Proxy({}, { get: () => () => ({ addColorStop() {} }) });
  const elements = Object.fromEntries(['investmentReturnChart', 'investmentNavChart'].map(id => [id, {
    id, getBoundingClientRect: () => ({ width: 320, height: 220 }), getContext: () => drawing,
  }]));
  const window = { addEventListener() {}, matchMedia: () => ({ matches: true }), devicePixelRatio: 1 };
  class Chart {
    constructor(canvas, config) {
      this.canvas = canvas; this.data = config.data; this.options = config.options;
      this.destroyed = false; charts.push(this);
    }
    destroy() { this.destroyed = true; }
    update() {}
  }
  const jget = (url) => { const request = { url, ...deferred() }; requests.push(request); return request.promise; };
  const context = vm.createContext({ window, console, ...(fallback ? {} : { Chart }),
    document: { body: { dataset: {} }, getElementById: id => elements[id] || null },
    localStorage: { getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value) },
  });
  // Do not run page bootstrap; execute the actual chart functions without changing their bodies.
  const module = new vm.SourceTextModule(source.slice(0, source.lastIndexOf('main().finally(')) + `
    export { loadInvestmentNavChartOnce, renderInvestmentReturnChart, getInvestmentChartState,
      applyInvestmentReturnWindow, normalizeInvestmentNavRows, buildInvestmentNavChartData, filterReturnWindowRows };
  `, { context });
  await module.link(specifier => {
    if (specifier.includes('chart_theme.js')) {
      return new vm.SourceTextModule(readFileSync(new URL('shared/chart_theme.js', root), 'utf8'), { context });
    }
    if (specifier.includes('app/routes.js')) {
      return new vm.SourceTextModule(readFileSync(new URL('app/routes.js', root), 'utf8'), { context });
    }
    const values = specifier.includes('api.js') ? { jget, fmt2: String, accountLabel: String }
      : specifier.includes('utils.js') ? { safeNum: value => Number.isFinite(Number(value)) ? Number(value) : 0,
        pnlPct: () => 0, createReturnRateChart() {} }
        : specifier.includes('investment_startup.js') ? { trackInvestmentNav: value => value,
          trackInvestmentHoldings: value => value, afterInvestmentNav: task => Promise.resolve().then(task) }
        : specifier.includes('investment_requests.js') ? { loadInvestmentSummaryFresh() {} }
          : specifier.includes('account_cash.js') ? { accountCashBreakdown() {} }
            : { openBottomSheet() {}, setActiveNav() {} };
    return new vm.SyntheticModule(Object.keys(values), function () {
      for (const [key, value] of Object.entries(values)) this.setExport(key, value);
    }, { context });
  });
  await module.evaluate();
  return { api: module.namespace, requests, charts, window, storage };
}

const nav = { rows: [
  { date: '2026-01-01', netWorth: '1000.125', confirmation_status: 'ok' },
  { date: '2026-09-30', netWorth: '1030.25', confirmation_status: 'last_valid' },
  { date: '2026-10-26', netWorth: '1040.5', confirmation_status: 'unconfirmed' },
] };
const returns = { rows: nav.rows.map((row, index) => ({ date: row.date, market_value: 1000 + index * 20,
  total_pnl: index * 20, return_rate: index * 0.02, cumulative_capital: 1000 })) };

for (const fallback of [false, true]) {
  for (const navFirst of [false, true]) {
    const test = await chartModule(fallback), { api, requests, window } = test;
    const inputBefore = JSON.stringify([nav, returns]);
    const loading = api.renderInvestmentReturnChart();
    const navPromise = api.loadInvestmentNavChartOnce();
    assert.equal(navPromise, api.loadInvestmentNavChartOnce());
    assert.equal(requests.length, 6);
    assert.match(requests[0].url, /^\/nav\/weekly/);
    const completeReturns = () => requests.slice(1).forEach(request => request.resolve(structuredClone(returns)));
    if (navFirst) {
      requests[0].resolve(structuredClone(nav)); await navPromise;
      assert.ok(window.__investmentNavChart);
      assert.equal(window.__investmentReturnChart, undefined);
      api.applyInvestmentReturnWindow(null, api.getInvestmentChartState(), '1m');
      completeReturns(); await loading;
    } else {
      completeReturns(); await loading;
      assert.equal(window.__investmentNavChart, undefined);
      api.applyInvestmentReturnWindow(window.__investmentReturnChart, api.getInvestmentChartState(), '1m');
      requests[0].resolve(structuredClone(nav)); await navPromise;
    }
    const state = api.getInvestmentChartState();
    assert.equal(state.activeWindow, '1m');
    assert.equal(test.storage.get('pams.investmentReturn.window'), '1m');
    const expected = api.buildInvestmentNavChartData(api.filterReturnWindowRows(api.normalizeInvestmentNavRows(nav.rows), '1m'), true);
    assert.deepEqual(plain(window.__investmentNavChart.data.labels), plain(expected.labels));
    assert.deepEqual(plain(window.__investmentNavChart.data.datasets[0].data), plain(expected.values));
    assert.equal(plain(state.navRows)[2].confirmation_status, 'unconfirmed');
    assert.equal(plain(state.navRows)[1].confirmation_status, 'last_valid');
    assert.equal(requests.length, 6);
    if (!fallback) assert.equal(test.charts.filter(chart => chart.canvas.id === 'investmentNavChart').length, 1);
    api.applyInvestmentReturnWindow(window.__investmentReturnChart, state, '1w');
    assert.deepEqual(plain(window.__investmentNavChart.data.labels), ['2026-10-26']);
    assert.deepEqual(plain(window.__investmentReturnChart.data.labels), ['2026-10-26']);
    assert.equal(JSON.stringify([nav, returns]), inputBefore);
  }
}

{
  const { api, requests, window } = await chartModule();
  const loading = api.renderInvestmentReturnChart();
  requests[0].resolve(structuredClone(nav)); await api.loadInvestmentNavChartOnce();
  requests.slice(1).forEach(request => request.reject(new Error('unavailable return series')));
  assert.equal(await loading, null);
  assert.deepEqual(plain(window.__investmentNavChart.data.datasets[0].data), [1000.125, 1030.25, 1040.5]);
}
{
  const { api, requests, window } = await chartModule();
  const loading = api.renderInvestmentReturnChart();
  requests[0].reject(new Error('unavailable NAV'));
  requests.slice(1).forEach(request => request.resolve(structuredClone(returns)));
  await loading; await api.loadInvestmentNavChartOnce();
  assert.deepEqual(plain(window.__investmentNavChart.data.datasets[0].data), []);
  assert.ok(window.__investmentReturnChart.data.labels.length);
}

{
  const requests = [], context = vm.createContext({});
  const module = new vm.SourceTextModule(readFileSync(new URL('investment_requests.js', root), 'utf8'), { context });
  await module.link(() => new vm.SyntheticModule(['jget'], function () {
    this.setExport('jget', (url, options) => {
      const request = { url, options, ...deferred() }; requests.push(request); return request.promise;
    });
  }, { context }));
  await module.evaluate();
  const load = module.namespace.loadInvestmentSummaryFresh;
  const a = load(), b = load();
  assert.equal(a, b); assert.equal(requests.length, 1);
  assert.equal(requests[0].options.cache, false);
  requests[0].resolve(nav); assert.equal(await a, nav); assert.equal(await b, nav);
  const c = load(); assert.notEqual(c, a); assert.equal(requests.length, 2);
  requests[1].reject(new Error('failed read'));
  await assert.rejects(c, /failed read/);
  const d = load(); assert.equal(requests.length, 3);
  requests[2].resolve(returns); assert.equal(await d, returns);
}
await settle();
console.log('Investment loading: both completion orders, Chart/fallback, windows, failures and fresh in-flight summary OK');
