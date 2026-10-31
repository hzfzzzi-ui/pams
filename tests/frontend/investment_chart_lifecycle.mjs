import {chartHarnessSource} from './investment_chart_fixture.mjs';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const root = new URL('../../frontend/public/assets/js/', import.meta.url);
const settle = () => new Promise(resolve => setImmediate(resolve));
const plain = value => JSON.parse(JSON.stringify(value));

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

class Target {
  constructor() { this.listeners = new Map(); }
  addEventListener(type, callback) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type).add(callback);
  }
  removeEventListener(type, callback) { this.listeners.get(type)?.delete(callback); }
  fire(type, event = {}) {
    for (const callback of this.listeners.get(type) || []) callback({ preventDefault() {}, stopPropagation() {}, ...event });
  }
  count(type) { return this.listeners.get(type)?.size || 0; }
}

class Element extends Target {
  constructor(id) {
    super(); this.id = id; this.dataset = {}; this.style = {}; this.attrs = {};
    this.buttons = []; this.children = []; this.draws = 0; this.classList = { toggle() {} };
    this.drawing = new Proxy({}, { get: (_, key) => (...args) => {
      if (key === 'clearRect') this.draws += 1;
      return { addColorStop() {} };
    } });
  }
  getBoundingClientRect() { return { width: 320, height: 220 }; }
  getContext() { return this.drawing; }
  setAttribute(key, value) { this.attrs[key] = value; }
  get innerHTML() { return this.html || ''; }
  set innerHTML(value) {
    this.html = value;
    this.buttons = [...value.matchAll(/<button\b([^>]*)>/g)].map((match, index) => {
      const button = new Element(`${this.id}-${index}`);
      for (const [, raw, data] of match[1].matchAll(/data-([a-z-]+)="([^"]*)"/g)) {
        const key = raw.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
        button.dataset[key] = data;
      }
      return button;
    });
    this.children.push(...this.buttons);
  }
  querySelectorAll(selector) {
    const key = selector.slice(6, -1).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
    return this.buttons.filter(button => key in button.dataset);
  }
}

const nav = { rows: [
  { date: '2026-01-01', netWorth: '1000.125', confirmation_status: 'ok' },
  { date: '2026-09-30', netWorth: '1030.25', confirmation_status: 'last_valid' },
  { date: '2026-10-26', netWorth: '1040.5', confirmation_status: 'unconfirmed' },
] };
const returns = { rows: nav.rows.map((row, index) => ({ date: row.date, market_value: 1000 + index * 20,
  total_pnl: index * 20, return_rate: index * 0.02, cumulative_capital: 1000 })) };

async function fixture({ fallback = false } = {}) {
  const requests = [], charts = [], storage = new Map();
  const ids = ['investmentReturnChart', 'investmentNavChart', 'investmentReturnToggle', 'investmentReturnWindowToggle',
    'returnEventHotspots', 'returnEventPopover', 'investmentReturnNote'];
  const elements = Object.fromEntries(ids.map(id => [id, new Element(id)]));
  const document = { body: { dataset: {} }, getElementById: id => elements[id] || null };
  const window = Object.assign(new Target(), { matchMedia: () => ({ matches: true }), devicePixelRatio: 1 });
  class Chart {
    constructor(canvas, config) {
      this.canvas = canvas; this.data = config.data; this.options = config.options;
      this.plugins = config.plugins; this.scales = {}; this.destroyCount = 0; this.updateCount = 0; charts.push(this);
    }
    destroy() { this.destroyCount += 1; }
    update() { this.updateCount += 1; }
    getDatasetMeta() { return { data: [] }; }
    getElementsAtEventForMode() { return []; }
  }
  const jget = (url, options) => { const req = { url, options, ...deferred() }; requests.push(req); return req.promise; };
  const context = vm.createContext({ window, document, console, ...(fallback ? {} : { Chart }),
    localStorage: { getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value) } });
  const entryURL = new URL('holdings_categories.js', root);
  const source = readFileSync(entryURL, 'utf8');
  const commonExports = 'loadInvestmentNavChartOnce, renderInvestmentReturnChart, getInvestmentChartState, applyInvestmentReturnWindow';
  const extraExports = ', createInvestmentChartController, disposeInvestmentCharts';
  const module = new vm.SourceTextModule(chartHarnessSource, { context, identifier: entryURL.href });
  const cache = new Map([[entryURL.href, module]]);
  await module.link((specifier, parent) => {
    const url = new URL(specifier, parent.identifier); url.search = '';
    if (cache.has(url.href)) return cache.get(url.href);
    const name = url.pathname.split('/').pop();
    let dependency;
    if (url.pathname.includes('/features/investment/charts/') || url.pathname.includes('/features/investment/holdings/') || url.pathname.endsWith('/shared/chart_theme.js') || url.pathname.endsWith('/app/routes.js')) {
      dependency = new vm.SourceTextModule(readFileSync(url, 'utf8'), { context, identifier: url.href });
    } else {
      const values = name === 'api.js' ? { jget, fmt2: String, accountLabel: String }
        : name === 'utils.js' ? { safeNum: value => Number.isFinite(Number(value)) ? Number(value) : 0, pnlPct: () => 0, createReturnRateChart() {} }
          : name === 'investment_startup.js' ? { trackInvestmentNav: value => value, trackInvestmentHoldings: value => value, afterInvestmentNav: task => Promise.resolve().then(task) }
            : name === 'investment_requests.js' ? { loadInvestmentSummaryFresh() {} }
              : name === 'account_cash.js' ? { accountCashBreakdown() {} }
                : { openBottomSheet() {}, setActiveNav() {} };
      dependency = new vm.SyntheticModule(Object.keys(values), function () {
        for (const [key, value] of Object.entries(values)) this.setExport(key, value);
      }, { context, identifier: url.href });
    }
    cache.set(url.href, dependency); return dependency;
  });
  await module.evaluate();
  const presenters = { esc: String, statusBadge: String, formatNumber: String, formatPercentRate: String,
    fmtCny: String, fmtSignedCny: String, fmtSignedPctFromRate: String, TopContributionPanel: () => '' };
  const create = () => module.namespace.createInvestmentChartController({ root: document, host: window, presenters, ChartClass: fallback ? undefined : Chart });
  const resolve = (list = requests, nextNav = nav, nextReturn = returns) => list.forEach(req => req.resolve(structuredClone(req.url.startsWith('/nav/') ? nextNav : nextReturn)));
  return { api: module.namespace, create, document, window, elements, requests, charts, storage, resolve };
}

for (const fallback of [false, true]) {
  const test = await fixture({ fallback });
  const controller = test.create();
  const loading = controller.loadReturnsOnce();
  assert.equal(loading, controller.loadReturnsOnce());
  const navLoading = controller.loadNavOnce();
  assert.equal(navLoading, controller.loadNavOnce());
  test.resolve(); await loading; await navLoading;
  assert.equal(test.window.count('resize'), 1);
  assert.equal(test.window.count('pagehide'), 1);
  const returnChart = test.window.__investmentReturnChart;
  const navChart = test.window.__investmentNavChart;
  const buttons = [...test.elements.investmentReturnToggle.buttons, ...test.elements.investmentReturnWindowToggle.buttons];
  assert.equal(buttons.length, 8);
  assert.ok(buttons.every(button => button.count('click') === 1));
  const week = test.elements.investmentReturnWindowToggle.buttons.find(button => button.dataset.returnWindow === '1w');
  week.fire('click');
  assert.equal(controller.state.activeWindow, '1w');
  const chartBefore = plain(returnChart.data);
  test.window.fire('pagehide', { persisted: true });
  assert.equal(controller.disposed, false);
  test.window.fire('resize');
  const drawCount = test.elements.investmentReturnChart.draws;
  const click = returnChart.options.onClick;
  test.window.fire('pagehide', { persisted: false });
  controller.dispose();
  assert.equal(controller.disposed, true);
  assert.equal(test.window.count('resize'), 0);
  assert.equal(test.window.count('pagehide'), 0);
  assert.ok(buttons.every(button => button.count('click') === 0));
  assert.equal(test.window.__investmentReturnChart, undefined);
  assert.equal(test.window.__investmentNavChart, undefined);
  if (fallback) { assert.equal(returnChart.destroyed, true); assert.equal(navChart.destroyed, true); }
  else { assert.equal(returnChart.destroyCount, 1); assert.equal(navChart.destroyCount, 1); }
  buttons.forEach(button => button.fire('click')); click({}); test.window.fire('resize');
  assert.equal(test.elements.investmentReturnChart.draws, drawCount);
  assert.deepEqual(plain(returnChart.data), chartBefore);
  assert.equal(await controller.loadNavOnce(), null);
  assert.equal(await controller.loadReturnsOnce(), null);
  assert.equal(await controller.renderReturns(), null);
  assert.equal(test.requests.length, 6);
}

// Old reads settle after a new owner is already visible: they must be inert.
for (const oldFirst of [false, true]) {
  const test = await fixture();
  const old = test.create(), oldLoading = old.loadReturnsOnce();
  const oldRequests = test.requests.slice();
  const replacement = test.create(), currentLoading = replacement.loadReturnsOnce();
  assert.equal(old.disposed, true);
  const freshRequests = test.requests.slice(6);
  if (oldFirst) { test.resolve(oldRequests); assert.equal(await oldLoading, null); assert.equal(test.charts.length, 0); }
  test.resolve(freshRequests); await currentLoading; await replacement.loadNavOnce();
  const currentChart = test.window.__investmentReturnChart;
  if (!oldFirst) { test.resolve(oldRequests); assert.equal(await oldLoading, null); }
  assert.equal(test.window.__investmentReturnChart, currentChart);
  assert.equal(test.charts.length, 2);
  assert.ok(test.charts.every(chart => chart.destroyCount === 0));
  assert.equal(old.state.navRows.length, 0);
  assert.equal(test.window.count('resize'), 1);
  replacement.dispose();
}

// A new render within the same owner supersedes its older return request.
{
  const test = await fixture(), controller = test.create();
  const older = controller.renderReturns();
  const oldReturnRequests = test.requests.slice(1);
  const newer = controller.renderReturns();
  const newReturnRequests = test.requests.slice(6);
  assert.equal(test.requests.length, 11);
  test.requests[0].resolve(nav);
  test.resolve(newReturnRequests, nav, { rows: [{ date: '2026-10-30', total_pnl: 10, market_value: 1010, return_rate: 0.01 }] });
  await newer;
  const chart = test.window.__investmentReturnChart;
  test.resolve(oldReturnRequests); assert.equal(await older, null);
  assert.equal(test.window.__investmentReturnChart, chart);
  assert.deepEqual(plain(chart.data.labels), ['2026-10-30']);
  controller.dispose();
}

// An owner disposed before any response creates no charts or listeners on completion.
{
  const test = await fixture(), controller = test.create();
  const loading = controller.loadReturnsOnce();
  controller.dispose(); test.resolve();
  assert.equal(await loading, null); await settle();
  assert.equal(test.charts.length, 0);
  assert.equal(test.window.count('resize'), 0);
  assert.equal(test.window.count('pagehide'), 0);
  assert.equal(test.document.body.dataset.investmentReturnChartLoaded, undefined);
}

// A legacy entry disposed before lazy mount never starts a fresh controller later.
{
  const test = await fixture();
  test.api.disposeInvestmentCharts();
  assert.equal(await test.api.loadInvestmentNavChartOnce(), null);
  assert.equal(await test.api.renderInvestmentReturnChart(), null);
  assert.equal(test.requests.length, 0);
  assert.equal(test.window.count('resize'), 0);
}

console.log('Investment chart lifecycle: Chart/fallback cleanup, controls, BFCache, replacement, generations and late reads OK');
