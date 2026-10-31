import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const root = new URL('../../frontend/public/assets/js/', import.meta.url);
const settle = () => new Promise(resolve => setImmediate(resolve));
function deferred() { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; }
class Target {
  constructor() { this.events = new Map(); this.style = {}; this.dataset = {}; this.textContent = ''; this.classList = { toggle() {}, add() {}, remove() {} }; }
  addEventListener(type, fn) { if (!this.events.has(type)) this.events.set(type, new Set()); this.events.get(type).add(fn); }
  removeEventListener(type, fn) { this.events.get(type)?.delete(fn); }
  emit(type, event = {}) { return Promise.all([...(this.events.get(type) || [])].map(fn => fn({ preventDefault() {}, stopPropagation() {}, ...event }))); }
  count(type) { return this.events.get(type)?.size || 0; }
}
class Form extends Target {
  constructor() {
    super(); this.submit = new Target(); this.cancel = new Target();
    this.fields = Object.fromEntries(Object.entries({ cashflow_type: 'operating_expense', category: '餐饮', subcategory: '工作餐', counter_account_code: '', counterparty: '  测试  ' }).map(([key, value]) => [key, Object.assign(new Target(), { value })]));
  }
  querySelector(selector) {
    if (selector === 'button[type="submit"]') return this.submit;
    if (selector === '[data-sheet-close]') return this.cancel;
    return this.fields[selector.match(/data-edit-field="([^"]+)"/)?.[1]] || null;
  }
}

async function fixture(width = 390) {
  const reads = [], writes = [], charts = [], errors = [], cache = new Map();
  let viewportWidth = width;
  const elements = Object.fromEntries(['err', 'overviewCategoryPieChart', 'cashflowTrendChart'].map(id => [id, Object.assign(new Target(), { id, parentElement: { querySelector() { return null; } } })]));
  const document = Object.assign(new Target(), { body: { dataset: {} }, getElementById: id => elements[id] || null, querySelectorAll: () => [] });
  const window = Object.assign(new Target(), { location: { hash: '', pathname: '/ledger.html', search: '' }, scrollTo() {} });
  window.matchMedia = query => ({ matches: query === '(max-width: 389px)' && viewportWidth <= 389 });
  class Chart { constructor(canvas, config) { this.canvas = canvas; this.data = config.data; this.config = config; this.destroyCount = 0; charts.push(this); } destroy() { this.destroyCount += 1; } }
  window.Chart = Chart;
  const context = vm.createContext({ document, window, AbortController, URLSearchParams, console });
  function moduleFor(url) {
    url = new URL(url); url.search = '';
    if (cache.has(url.href)) return cache.get(url.href);
    const name = url.pathname.split('/').pop();
    let values = null;
    if (name === 'api.js') values = { accountLabel: String, jget: (url, options) => { const item = { url, options, ...deferred() }; reads.push(item); return item.promise; }, jpost: (url, body) => { const item = { url, body, ...deferred() }; writes.push(item); return item.promise; } };
    if (name === 'utils.js') values = { escapeHtml: String, formatCurrency: String, formatMoneyAxis: String, formatMoneyFixed2: String, formatPercent: String, formatSignedMoneyFixed2: String, toFiniteNumber: value => Number.isFinite(Number(value)) ? Number(value) : 0 };
    if (name === 'routes.js') values = { replaceContentAddress() {} };
    if (name === 'ui.js') values = { setActiveNav() {} };
    if (name === 'bottom_sheet.js') values = { createBottomSheet: () => ({ opened: false, close() { this.opened = false; }, dispose() { this.close(); } }) };
    let module;
    if (values) module = new vm.SyntheticModule(Object.keys(values), function () { for (const [key, value] of Object.entries(values)) this.setExport(key, value); }, { context, identifier: url.href });
    else module = new vm.SourceTextModule(readFileSync(url, 'utf8'), { context, identifier: url.href });
    cache.set(url.href, module); return module;
  }
  async function api(file) {
    const module = moduleFor(new URL(`features/ledger/${file}.js`, root));
    if (module.status === 'unlinked') await module.link((specifier, parent) => moduleFor(new URL(specifier, parent.identifier)));
    if (module.status !== 'evaluated') await module.evaluate();
    return module.namespace;
  }
  const { createLedgerController } = await api('controller');
  const create = () => createLedgerController({ root: document, host: window });
  const resolveRead = (index, value) => { reads[index].resolve(value); reads[index + 1].resolve({ accounts: [] }); };
  return { reads, writes, charts, errors, document, window, elements, api, create, resolveRead, setWidth: width => { viewportWidth = width; } };
}
const data = income => ({ periods: { ytd: { income } }, income_categories_ytd: [{ category: '工资薪酬', amount: 10 }], monthly_trend: [{ period: '2026-10', income: 10, expense: 1, net: 9 }] });

{
  const test = await fixture(320), controller = test.create(), ready = controller.init();
  test.resolveRead(0, data(10)); await ready;
  const trend = test.charts.find(chart => chart.config.options?.scales);
  assert.equal(trend.config.options.scales.y.ticks.font().size, 10);
  assert.equal(trend.config.options.scales.net.ticks.font().size, 10);
  test.setWidth(390);
  assert.equal(trend.config.options.scales.y.ticks.font().size, 12);
  assert.equal(trend.config.options.scales.net.ticks.font().size, 12);
  test.setWidth(360);
  assert.equal(trend.config.options.scales.y.ticks.font().size, 10);
  assert.equal(trend.config.options.scales.net.ticks.font().size, 10);
  test.setWidth(389);
  assert.equal(trend.config.options.scales.y.ticks.font().size, 10);
  assert.equal(trend.config.options.scales.net.ticks.font().size, 10);
  test.setWidth(320);
  assert.equal(trend.config.options.scales.y.ticks.font().size, 10);
  assert.equal(JSON.stringify(trend.data.datasets.map(dataset => dataset.data)), JSON.stringify([[10], [1], [9]]));
  controller.dispose();
}
{
  const test = await fixture(390), controller = test.create(), ready = controller.init();
  test.resolveRead(0, data(10)); await ready;
  const trend = test.charts.find(chart => chart.config.options?.scales);
  assert.equal(trend.config.options.scales.y.ticks.font().size, 12);
  assert.equal(trend.config.options.scales.net.ticks.font().size, 12);
  test.setWidth(360);
  assert.equal(trend.config.options.scales.y.ticks.font().size, 10);
  assert.equal(trend.config.options.scales.net.ticks.font().size, 10);
  controller.dispose();
}

{
  const test = await fixture();
  const {mountLedgerPage} = await test.api('page');
  const options = {root: test.document, host: test.window};
  const page = mountLedgerPage(options);
  assert.equal(mountLedgerPage(options), page);
  assert.equal(test.reads.length, 2);
  test.resolveRead(0, data(10)); await page.ready;
  await test.window.emit('pagehide', {persisted: true});
  await test.window.emit('pageshow', {persisted: true});
  await test.window.emit('pageshow', {persisted: true});
  assert.equal(test.reads.length, 4, 'A restored page starts only one read sequence');
  test.resolveRead(2, data(11)); await page.ready;
  page.dispose();
  assert.equal(test.window.count('pagehide'), 0);
  assert.equal(test.window.count('pageshow'), 0);
}

{
  const test = await fixture(), controller = test.create();
  const initial = controller.init();
  assert.equal(initial, controller.init());
  assert.deepEqual(test.reads.map(({ url, options }) => [url, options?.cache]), [['/api/ledger/workbench?baseCurrency=CNY&recentDays=30', false], ['/api/accounts/overview', undefined]]);
  test.resolveRead(0, data(10)); await initial;
  assert.equal(test.charts.length, 2);
  const older = controller.loadWorkbench(), newer = controller.loadWorkbench();
  test.resolveRead(4, data(20)); await newer;
  test.resolveRead(2, data(15)); await assert.rejects(older, error => error.name === 'AbortError');
  assert.equal(controller.state.data.periods.ytd.income, 20);
  assert.equal(test.charts.length, 4);
  const pending = controller.loadWorkbench();
  controller.dispose(); controller.dispose();
  test.resolveRead(6, data(99)); await assert.rejects(pending, error => error.name === 'AbortError');
  assert.ok(test.charts.every(chart => chart.destroyCount === 1));
  assert.equal(test.document.count('click'), 0);
  assert.equal(test.window.count('hashchange'), 0);
  assert.equal(test.window.count('pagehide'), 0);
  assert.equal(await controller.loadWorkbench(), null);
  assert.equal(test.reads.length, 8);
}
{
  const test = await fixture(), old = test.create(), pending = old.init();
  const replacement = test.create(), ready = replacement.init();
  assert.equal(old.disposed, true);
  test.resolveRead(2, data(30)); await ready;
  test.resolveRead(0, data(1)); await pending;
  assert.equal(replacement.state.data.periods.ytd.income, 30);
  assert.equal(test.charts.length, 2);
  replacement.dispose();
}
{
  const test = await fixture();
  const { createLedgerState, snapshotLedgerViewState, restoreLedgerViewState } = await test.api('state');
  const state = createLedgerState();
  Object.assign(state, { recentSearch: '测试', recentAmountSort: 'amount_desc', recentAccountFilter: 'ccb_7332', data: data(10), accounts: { private: true } });
  state.expandedAccountGroups.add('ccb');
  const snapshot = snapshotLedgerViewState(state);
  assert.equal('data' in snapshot, false); assert.equal('accounts' in snapshot, false);
  const restored = createLedgerState(); restoreLedgerViewState(restored, snapshot);
  assert.equal(restored.recentSearch, '测试'); assert.equal(restored.recentAmountSort, 'amount_desc');
  assert.equal(restored.expandedAccountGroups.has('ccb'), true);
  restored.expandedAccountGroups.add('wechat'); assert.equal(state.expandedAccountGroups.has('wechat'), false);
}
{
  const test = await fixture();
  const { createLedgerEditor } = await test.api('editor');
  const { createLedgerLegacyModel } = await test.api('legacy_model');
  const row = { id: '1', needs_review: true, flow_type: 'expense', cashflow_type: 'operating_expense' };
  const model = createLedgerLegacyModel({ data: { review_rows: [row] } });
  let current = true, session, reloads = 0;
  const sheets = { open() {
    const form = new Form(); let active = true;
    session = { form, body: { querySelector: () => form }, active: () => active && current,
      listen(target, type, callback) { target?.addEventListener(type, event => { if (active && current) return callback(event); }); },
      close() { active = false; },
    }; return session;
  } };
  const editor = createLedgerEditor({ model, templates: { optionHtml: () => '', ledgerEditSheetHtml: () => '' }, sheets,
    dom: { showError: error => test.errors.push(error.message) }, current: () => current, reload: async () => { reloads += 1; } });
  editor.openLedgerEdit(row);
  const submission = session.form.emit('submit'); await session.form.emit('submit');
  assert.equal(test.writes.length, 1);
  assert.equal(test.writes[0].url, '/api/ledger/rows/1/update');
  assert.deepEqual(JSON.parse(JSON.stringify(test.writes[0].body)), { cashflow_type: 'operating_expense', category: '餐饮', subcategory: '工作餐', counter_account_code: '', counterparty: '测试' });
  test.writes[0].reject(new Error('retryable')); await submission;
  assert.equal(session.form.submit.disabled, false);
  const retry = session.form.emit('submit'); assert.equal(test.writes.length, 2);
  test.writes[1].resolve({ ok: true }); await retry;
  assert.equal(reloads, 1); assert.equal(session.active(), false);
  editor.openLedgerEdit(row); await session.form.cancel.emit('click'); await session.form.emit('submit');
  assert.equal(test.writes.length, 2);
  editor.openLedgerEdit(row); const cancelled = session.form.emit('submit'); session.close(); editor.openLedgerEdit(row);
  test.writes[2].reject(new Error('stale cancelled form')); await cancelled;
  assert.deepEqual(test.errors, ['retryable']); assert.equal(session.active(), true);
  const late = session.form.emit('submit'); current = false; test.writes[3].resolve({ ok: true }); await late;
  assert.equal(reloads, 1); assert.deepEqual(test.errors, ['retryable']);
}
await settle();
console.log('Ledger modules: actual controller/editor/requests; read generations, replacement, two Chart disposal, listeners, view state, one POST, retry and stale edit isolation OK');
