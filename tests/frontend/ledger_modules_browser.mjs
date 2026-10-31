// Explicit synthetic ledger fixture. Every HTTP request is fulfilled locally or aborted.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const publicRoot = path.resolve(process.argv[2] || 'frontend/public');
const baselineEntry = process.argv[3] ? path.resolve(process.argv[3]) : null;
const runtime = process.env.PAMS_FRONTEND_PLAYWRIGHT_MODULE;
const { chromium } = runtime ? await import(pathToFileURL(runtime).href) : await import('playwright');
const fixture = {
  periods: { month: { income: 1250.25, expense: 24.5, disposable_net: 1225.75, internal_transfer: 300, pending_count: 1 }, ytd: { income: 2250.25, expense: 124.5, disposable_net: 2125.75, internal_transfer: 400, investment_transfer: 200, investment_transfer_net: -200, cash_net: 1925.75, pending_count: 1 } },
  monthly_trend: [{ period: '2026-01', income: 1000, expense: 100, net: 900 }, { period: '2026-10', income: 1250.25, expense: 24.5, net: 1225.75 }],
  weekly_trend: [{ period: '2026-W43', income: 1250.25, expense: 24.5, net: 1225.75 }],
  income_categories_ytd: [{ category: '工资薪酬', amount: 2250.25, count: 2, share: 1 }],
  income_categories_month: [{ category: '工资薪酬', amount: 1250.25, count: 1, share: 1 }],
  expense_categories_ytd: [{ category: '餐饮', amount: 124.5, count: 2, share: 1 }],
  expense_categories_month: [{ category: '餐饮', amount: 24.5, count: 1, share: 1 }],
  transfer_summary_ytd: [{ relation_code: 'ccb-to-ibkr', match_type: 'investment', count: 1, amount: 200 }, { relation_code: 'ccb-to-wechat', match_type: 'transfer', count: 1, amount: 200 }],
  transfer_summary_month: [{ relation_code: 'ccb-to-ibkr', match_type: 'investment', count: 1, amount: 200 }],
  classification_rules: [{ name: '固定测试规则', description: '仅测试，不产生财务事实' }],
  account_summary_ytd: [
    { account_code: 'ccb_7332', account_kind: 'cash', income: 2250.25, expense: 124.5, transfer: 400, net: 1725.75, row_count: 5, pending_count: 1, latest_sync_at: '2026-10-31' },
    { account_code: 'ibkr', account_kind: 'investment', investment_inflow: 200, investment_outflow: 0, investment_fee: 1.25, investment_return: 3, net: 200, row_count: 1, latest_sync_at: '2026-10-30' },
  ],
  recent_rows_ytd: [
    { id: '1', transaction_date: '2026-10-30', flow_type: 'expense', cashflow_type: 'operating_expense', amount_cny: -24.5, amount: '-24.50', currency: 'CNY', account_code: 'ccb_7332', summary: '午餐 测试商户', category: '餐饮', coarse_category: '餐饮', subcategory: '工作餐', counterparty: '测试商户', needs_review: true },
    { id: '2', transaction_date: '2026-10-29', flow_type: 'income', cashflow_type: 'operating_income', amount_cny: 1250.25, amount: '1250.25', currency: 'CNY', account_code: 'ccb_7332', summary: '工资薪酬', category: '工资薪酬', coarse_category: '工资薪酬', subcategory: '工资', needs_review: false },
    { id: '3', transaction_date: '2026-10-28', flow_type: 'investment_inflow', cashflow_type: 'investment_funding', amount_cny: 200, amount: '28.10', currency: 'USD', account_code: 'ibkr', counterparty: 'ccb_7332', summary: '测试投资入金', category: '内部划转', coarse_category: '内部划转', needs_review: false },
    { id: '4', transaction_date: '2026-09-28', flow_type: 'transfer', cashflow_type: 'internal_transfer', amount_cny: -200, amount: '-200', currency: 'CNY', account_code: 'ccb_7332', counter_account_code: 'wechat', summary: '内部转账', category: '内部划转', needs_review: false },
    { id: '5', transaction_date: '2026-01-28', flow_type: 'expense', cashflow_type: 'operating_expense', amount_cny: -100, amount: '-100', currency: 'CNY', account_code: 'ccb_7332', summary: '零散支出', category: '其他支出', needs_review: false },
  ],
};
fixture.recent_rows_month = fixture.recent_rows_ytd.slice(0, 3);
fixture.review_rows = [fixture.recent_rows_ytd[0]];
const accounts = { accounts: [
  { account_code: 'ccb_7332', display_name: '建行测试储蓄卡', primary_balance: { amount: '9876.54321', currency: 'CNY', as_of: '2026-10-30' } },
  { account_code: 'ibkr', display_name: 'IBKR测试', primary_balance: { amount: '1200.12', currency: 'USD', as_of: '2026-10-30' }, platform_balances: [{ amount: 1300.12, currency: 'USD', holding_pnl: 5 }, { amount: -100, currency: 'USD', asset_liability: '负债' }] },
] };
const origin = 'http://ledger-fixture.local';
const browser = await chromium.launch({ headless: true, channel: 'msedge' });
const errors = [];
const snapshots = new Map();

async function makePage(legacy = false) {
  const context = await browser.newContext({ viewport: { width: 390, height: 1084 }, locale: 'zh-CN', timezoneId: 'Asia/Shanghai', serviceWorkers: 'block' });
  const page = await context.newPage();
  page.on('pageerror', error => errors.push(String(error)));
  await page.addInitScript(({ fixture, accounts }) => {
    window.__ledgerTest = { reads: [], writes: [], pendingReads: [], pendingPosts: [], deferReads: false, fixture, accounts };
    window.__ledgerRead = (url, options) => {
      const test = window.__ledgerTest;
      test.reads.push({ url, options });
      if (url.startsWith('/api/ledger/workbench')) {
        if (test.deferReads) return new Promise((resolve, reject) => test.pendingReads.push({ resolve, reject }));
        return Promise.resolve(structuredClone(test.fixture));
      }
      if (url === '/api/accounts/overview') return Promise.resolve(structuredClone(test.accounts));
      return Promise.resolve({});
    };
    window.__ledgerWrite = (url, body) => {
      window.__ledgerTest.writes.push({ url, body });
      return new Promise((resolve, reject) => window.__ledgerTest.pendingPosts.push({ resolve, reject }));
    };
  }, { fixture, accounts });
  await context.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.href.includes('chart.js@')) {
      // Chart lifecycle test double; the separate sealed replay covers real Chart.js rendering.
      return route.fulfill({ contentType: 'application/javascript', body: `window.Chart = class Chart {
        static instances = {}; static nextId = 1;
        constructor(canvas, config) { this.canvas = canvas; this.data = config.data; this.options = config.options;
          this.id = Chart.nextId++; Chart.instances[this.id] = this; }
        destroy() { delete Chart.instances[this.id]; this.destroyed = true; }
        update() {}
      };` });
    }
    if (url.origin !== origin || route.request().method() !== 'GET') return route.abort();
    const target = path.resolve(publicRoot, '.' + decodeURIComponent(url.pathname));
    if (!target.startsWith(publicRoot + path.sep)) return route.abort();
    try {
      let body = await readFile(target);
      if (url.pathname.endsWith('/api.js')) {
        let text = body.toString('utf8').replace('export async function jget(', 'async function originalJget(').replace('export async function jpost(', 'async function originalJpost(');
        text += '\nexport function jget(path, options={}) { return window.__ledgerRead(path, options); }\nexport function jpost(path, body) { return window.__ledgerWrite(path, body); }\n';
        body = Buffer.from(text);
      }
      if (url.pathname.endsWith('/ledger_cashflow.js')) {
        const entry = legacy ? await readFile(baselineEntry, 'utf8') : body.toString('utf8');
        body = Buffer.from(entry + (legacy ? '\nwindow.__legacyLedger={state, loadWorkbench};\n' : '\nwindow.__ledgerPage=ledger;\n'));
      }
      const contentType = ({ '.js': 'application/javascript', '.html': 'text/html', '.css': 'text/css' })[path.extname(target)] || 'application/octet-stream';
      return route.fulfill({ contentType, body });
    } catch { return route.fulfill({ status: 404, body: '' }); }
  });
  await page.goto(origin + '/ledger.html?content=1');
  await page.waitForFunction(() => document.querySelector('#recentRows .ledgerListRow') && window.__cashflowTrendChart && window.__overviewCategoryPieChart);
  return { page, context };
}

async function capture(page, label, legacy) {
  const snapshot = await page.evaluate(() => {
    const ids = ['ledgerMobileHero', 'kpiMonthIncome', 'kpiMonthExpense', 'kpiDisposableNet', 'kpiInvestmentTransfer', 'kpiTransfer', 'kpiPending', 'categoryInsightTitle', 'overviewCategoryRank', 'cashflowBridge', 'transferSummary', 'classificationRules', 'accountFlowRows', 'recentAccountScope', 'recentRows'];
    return { markup: Object.fromEntries(ids.map(id => [id, document.getElementById(id)?.innerHTML])),
      charts: [window.__cashflowTrendChart?.data, window.__overviewCategoryPieChart?.data],
      panel: document.body.dataset.ledgerTab, hash: location.hash };
  });
  if (legacy) snapshots.set(label, snapshot);
  else if (baselineEntry) assert.deepEqual(snapshot, snapshots.get(label), label);
}

async function browse(legacy) {
  const { page, context } = await makePage(legacy);
  await capture(page, 'overview', legacy);
  await page.locator('[data-overview-period="month"]').click();
  await page.locator('[data-overview-flow="expense"]').click();
  await page.locator('#trendWeekly').click();
  await capture(page, 'month-expense-weekly', legacy);
  await page.locator('[data-ledger-tab="recent"]').click();
  await page.locator('[data-ledger-flow-filter="investment_transfer"]').click();
  await capture(page, 'recent-investment', legacy);
  await page.locator('[data-ledger-flow-filter=""]').click();
  await page.locator('#recentSearchToggle').click();
  await page.locator('[data-recent-search-input]').fill('测试商户');
  await page.locator('[data-recent-search-form] button[type="submit"]').click();
  await capture(page, 'search', legacy);
  await page.locator('#recentSearchToggle').click(); await page.locator('[data-recent-search-clear]').click();
  await page.locator('#recentSortToggle').click(); await page.locator('[data-recent-sort-option="amount_desc"]').click();
  await capture(page, 'sorted', legacy);
  await page.locator('[data-ledger-tab="account"]').click();
  await page.locator('[data-account-group-toggle="ccb"]').click();
  await capture(page, 'account-expanded', legacy);
  await page.locator('[data-account-flow-filter="ccb_7332"]').click();
  await capture(page, 'account-drilldown', legacy);
  await page.locator('[data-clear-account-filter]').click();
  await page.locator('[data-ledger-edit="1"]').filter({ visible: true }).first().click();
  await page.locator('[data-ledger-edit-form]').waitFor();
  const editTemplate = await page.locator('[data-ledger-edit-form]').innerHTML();
  if (legacy) snapshots.set('editTemplate', editTemplate); else if (baselineEntry) assert.equal(editTemplate, snapshots.get('editTemplate'));
  await page.locator('[data-ledger-edit-form] [data-sheet-close]').click();
  assert.equal(await page.evaluate(() => __ledgerTest.writes.length), 0);
  await context.close();
}

try {
  if (baselineEntry) await browse(true);
  await browse(false);
  const { page, context } = await makePage(false);
  await page.locator('[data-overview-period="month"]').click();
  await page.locator('[data-overview-flow="expense"]').click();
  await page.locator('#trendWeekly').click();
  await page.locator('[data-ledger-tab="recent"]').click();
  await page.locator('[data-ledger-edit="1"]').filter({ visible: true }).first().click();
  await page.locator('[data-ledger-edit-form]').waitFor();
  await page.locator('[data-edit-field="counterparty"]').fill('  新测试商户  ');
  await page.evaluate(() => {
    const form = document.querySelector('[data-ledger-edit-form]');
    form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  });
  assert.deepEqual(await page.evaluate(() => __ledgerTest.writes), [{ url: '/api/ledger/rows/1/update', body: { cashflow_type: 'operating_expense', category: '餐饮', subcategory: '工作餐', counter_account_code: '', counterparty: '新测试商户' } }]);
  await page.evaluate(() => __ledgerTest.pendingPosts.shift().reject(new Error('fixture rejection')));
  await page.waitForFunction(() => !document.querySelector('[data-ledger-edit-form] button[type="submit"]').disabled);
  assert.match(await page.locator('#err').textContent(), /fixture rejection/);
  await page.locator('[data-ledger-edit-form] button[type="submit"]').click();
  assert.equal(await page.evaluate(() => __ledgerTest.writes.length), 2);
  await page.evaluate(() => __ledgerTest.pendingPosts.shift().resolve({ ok: true }));
  await page.waitForFunction(() => !document.querySelector('[data-ledger-edit-form]'));
  await page.waitForFunction(() => __ledgerTest.reads.filter(r => r.url.startsWith('/api/ledger/workbench')).length === 2);

  // A cancelled form's rejected POST must not alter a newer sheet or its error state.
  await page.locator('[data-ledger-edit="1"]').filter({ visible: true }).first().click();
  await page.locator('[data-ledger-edit-form] button[type="submit"]').click();
  await page.locator('[data-ledger-edit-form] [data-sheet-close]').click();
  await page.locator('#recentSearchToggle').click();
  await page.evaluate(() => __ledgerTest.pendingPosts.shift().reject(new Error('cancelled old form')));
  await page.waitForTimeout(20);
  assert.equal(await page.locator('[data-recent-search-form]').count(), 1);
  assert.equal(await page.locator('#err').textContent(), '');
  await page.keyboard.press('Escape');

  // An older read in the same controller cannot overwrite a newer response.
  await page.evaluate(() => {
    __ledgerTest.deferReads = true;
    __older = __ledgerPage.controller.loadWorkbench();
    __newer = __ledgerPage.controller.loadWorkbench();
    const next = structuredClone(__ledgerTest.fixture); next.periods.ytd.income = 9999;
    __ledgerTest.pendingReads[1].resolve(next);
  });
  await page.waitForFunction(() => __ledgerPage.controller.state.data.periods.ytd.income === 9999);
  await page.evaluate(() => __ledgerTest.pendingReads[0].resolve(structuredClone(__ledgerTest.fixture)));
  await page.evaluate(() => Promise.all([__older, __newer]));
  assert.equal(await page.evaluate(() => __ledgerPage.controller.state.data.periods.ytd.income), 9999);
  await page.evaluate(() => { __ledgerTest.deferReads = false; __ledgerTest.pendingReads = []; });

  // Preserve ordinary view state over BFCache, never resume the edit operation.
  await page.locator('#recentSortToggle').click(); await page.locator('[data-recent-sort-option="amount_desc"]').click();
  await page.locator('[data-ledger-tab="account"]').click(); await page.locator('[data-account-group-toggle="ccb"]').click();
  await page.locator('[data-account-flow-filter="ccb_7332"]').click();
  await page.locator('[data-ledger-flow-filter="review"]').click();
  await page.locator('[data-recent-period="month"]').click();
  await page.locator('#recentSearchToggle').click();
  await page.locator('[data-recent-search-input]').fill('测试');
  await page.locator('[data-recent-search-form] button[type="submit"]').click();
  await page.locator('[data-ledger-edit="1"]').filter({ visible: true }).first().click();
  await page.locator('[data-ledger-edit-form] button[type="submit"]').click();
  const writesBefore = await page.evaluate(() => __ledgerTest.writes.length);
  const viewBefore = await page.evaluate(async () => {
    const { snapshotLedgerViewState } = await import('/assets/js/features/ledger/state.js?v=20261031-ledger-modules-v1');
    return snapshotLedgerViewState(__ledgerPage.controller.state);
  });
  await page.evaluate(() => { __oldController = __ledgerPage.controller; window.dispatchEvent(new PageTransitionEvent('pagehide', { persisted: true })); });
  assert.equal(await page.evaluate(() => Object.keys(Chart.instances).length), 0);
  assert.equal(await page.evaluate(() => __oldController.disposed), true);
  await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true })));
  await page.evaluate(() => __ledgerPage.ready);
  const viewAfter = await page.evaluate(async () => {
    const { snapshotLedgerViewState } = await import('/assets/js/features/ledger/state.js?v=20261031-ledger-modules-v1');
    return snapshotLedgerViewState(__ledgerPage.controller.state);
  });
  assert.deepEqual(viewAfter, viewBefore);
  assert.equal(await page.evaluate(() => Object.keys(Chart.instances).length), 2);
  assert.equal(await page.evaluate(() => __ledgerTest.writes.length), writesBefore);
  await page.locator('#recentSearchToggle').click();
  await page.evaluate(() => __ledgerTest.pendingPosts.shift().resolve({ ok: true }));
  await page.waitForTimeout(20);
  assert.equal(await page.locator('[data-recent-search-form]').count(), 1);
  await page.keyboard.press('Escape');
  assert.equal(await page.evaluate(() => __ledgerTest.writes.length), writesBefore);

  // Mounting again is idempotent; explicit disposal allows a fresh owner.
  await page.evaluate(async () => {
    __ledgerTest.deferReads = true;
    __oldLoading = __ledgerPage.controller.loadWorkbench();
    const { mountLedgerPage } = await import('/assets/js/features/ledger/page.js?v=20261031-ledger-modules-v1');
    if (mountLedgerPage() !== __ledgerPage) throw new Error('Repeated mount must reuse the page');
    if (__ledgerTest.pendingReads.length !== 1) throw new Error('Repeated mount issued another read');
    __ledgerPage.dispose();
    __ledgerPage = mountLedgerPage();
    __ledgerTest.pendingReads[1].resolve(structuredClone(__ledgerTest.fixture));
  });
  await page.evaluate(() => __ledgerPage.ready);
  await page.evaluate(() => __ledgerTest.pendingReads[0].resolve({ periods: { ytd: { income: 111 } } }));
  await page.evaluate(() => __oldLoading);
  assert.equal(await page.evaluate(() => __ledgerPage.controller.state.data.periods.ytd.income), 2250.25);
  assert.equal(await page.evaluate(() => Object.keys(Chart.instances).length), 2);
  const requestCount = await page.evaluate(() => __ledgerTest.reads.length);
  await page.evaluate(() => { __disposed = __ledgerPage.controller; __ledgerPage.dispose(); document.querySelector('[data-overview-period="month"]').click(); window.dispatchEvent(new HashChangeEvent('hashchange')); });
  assert.equal(await page.evaluate(() => Object.keys(Chart.instances).length), 0);
  assert.equal(await page.evaluate(() => __ledgerTest.reads.length), requestCount);
  assert.equal(await page.evaluate(() => __disposed.loadWorkbench()), null);
  assert.equal(await page.evaluate(() => __ledgerTest.writes.length), writesBefore);

  // A mounted element queries and binds only its own subtree.
  await page.evaluate(async () => {
    __ledgerTest.deferReads = false;
    const { mountLedgerPage } = await import('/assets/js/features/ledger/page.js?v=20261031-ledger-modules-v1');
    const outside = document.createElement('button'); outside.id = 'outside-ledger'; outside.dataset.overviewPeriod = 'month'; document.body.append(outside);
    __ledgerPage = mountLedgerPage({ root: document.querySelector('.wrap') });
    await __ledgerPage.ready;
    outside.click();
  });
  assert.equal(await page.evaluate(() => __ledgerPage.controller.state.overviewPeriod), 'ytd');
  assert.equal(await page.evaluate(() => Object.keys(Chart.instances).length), 2);
  await page.evaluate(() => __ledgerPage.dispose());
  assert.equal(await page.evaluate(() => Object.keys(Chart.instances).length), 0);
  assert.deepEqual(errors, []);
  await context.close();
  console.log(JSON.stringify({ status: 'passed', fixedEquivalenceStates: baselineEntry ? snapshots.size : 0, controls: 'tabs/period/flow/trend/search/sort/account drilldown/cancel', edits: 'one POST per submit; explicit retry after failure; exact payload', lifecycle: 'latest read wins; dispose; replacement; BFCache restores view only; late POST inert', pageErrors: errors.length }));
} finally { await browser.close(); }
