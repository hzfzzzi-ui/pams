// Explicit synthetic ledger fixture. Every HTTP request is fulfilled locally or aborted.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const publicRoot = path.resolve(process.argv[2] || 'frontend/public');
const transfersOnly = process.argv[3] === '--transfers-only';
const streamOnly = process.argv[3] === '--stream-only';
const baselineEntry = !transfersOnly && !streamOnly && process.argv[3] ? path.resolve(process.argv[3]) : null;
const runtime = process.env.PAMS_FRONTEND_PLAYWRIGHT_MODULE;
const { chromium } = runtime ? await import(pathToFileURL(runtime).href) : await import('playwright');
const chartScript = process.env.PAMS_FRONTEND_CHART_SCRIPT;
const fixture = {
  periods: { month: { income: 1250.25, expense: 24.5, disposable_net: 1225.75, internal_transfer: 300, pending_count: 1 }, ytd: { income: 2250.25, expense: 124.5, disposable_net: 2125.75, internal_transfer: 400, investment_transfer: 200, investment_transfer_net: -200, cash_net: 1925.75, pending_count: 1 } },
  monthly_trend: [{ period: '2026-01', income: 1000, expense: 100, net: 900 }, { period: '2026-10', income: 1250.25, expense: 24.5, net: 1225.75 }],
  weekly_trend: [{ period: '2026-10-19', income: 1250.25, expense: 24.5, net: 1225.75 }],
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
    { id: 'investment-trade-3', transaction_date: '2026-10-28', flow_type: 'investment_inflow', cashflow_type: 'investment_funding', amount_cny: 200, amount: '28.10', currency: 'USD', account_code: 'ibkr', counterparty: 'ccb_7332', summary: '测试投资入金', category: '内部划转', coarse_category: '内部划转', is_internal_transfer: true, needs_review: false },
    { id: '4', transaction_date: '2026-09-28', flow_type: 'transfer', cashflow_type: 'internal_transfer', amount_cny: -200, amount: '-200', currency: 'CNY', account_code: 'ccb_7332', counter_account_code: 'wechat', summary: '内部转账', category: '内部划转', needs_review: false },
    { id: '5', transaction_date: '2026-01-28', flow_type: 'expense', cashflow_type: 'operating_expense', amount_cny: -100, amount: '-100', currency: 'CNY', account_code: 'ccb_7332', summary: '零散支出', category: '其他支出', needs_review: false },
  ],
};
fixture.recent_rows_month = fixture.recent_rows_ytd.slice(0, 3);
fixture.review_rows = [fixture.recent_rows_ytd[0]];
fixture.periods.month.investment_funding = 35;
fixture.periods.month.cash_net = null;
fixture.periods.ytd.investment_funding = 160;
fixture.periods.ytd.unclassified_count = 1;
const accounts = { accounts: [
  { account_code: 'ccb_7332', display_name: '建行测试储蓄卡', primary_balance: { amount: '9876.54321', currency: 'CNY', as_of: '2026-10-30' } },
  { account_code: 'ibkr', display_name: 'IBKR测试', primary_balance: { amount: '1200.12', currency: 'USD', as_of: '2026-10-30' }, platform_balances: [{ amount: 1300.12, currency: 'USD', holding_pnl: 5 }, { amount: -100, currency: 'USD', asset_liability: '负债' }] },
] };
const streamAccountGroups = [
  { code:'ccb', label:'建设银行', accounts:[
    {code:'ccb_7332', label:'7332 储蓄卡'}, {code:'ccb_credit_0453', label:'0453 信用卡'},
  ] },
  { code:'ibkr', label:'IBKR', accounts:[{code:'ibkr', label:'IBKR 证券账户'}] },
  { code:'tencent', label:'腾讯', accounts:[{code:'tencent_licaitong_current_plus', label:'理财通活期+'}] },
];
const origin = 'http://ledger-fixture.local';
const browser = await chromium.launch({ headless: true, channel: 'msedge' });
const errors = [];
const snapshots = new Map();

async function makePage(legacy = false, fixtureData = fixture) {
  const context = await browser.newContext({ viewport: { width: 390, height: 1084 }, locale: 'zh-CN', timezoneId: 'Asia/Shanghai', serviceWorkers: 'block' });
  const page = await context.newPage();
  page.on('pageerror', error => errors.push(String(error)));
  await page.addInitScript(({ fixture, accounts, streamAccountGroups }) => {
    window.__ledgerTest = { reads: [], writes: [], pendingReads: [], pendingPosts: [], deferReads: false, fixture, accounts };
    window.__ledgerRead = (url, options) => {
      const test = window.__ledgerTest;
      test.reads.push({ url, options });
      if (url.startsWith('/api/ledger/workbench')) {
        const query = new URL(url, location.origin).searchParams;
        const select = payload => {
          if (query.get('view') !== 'stream') return payload;
          const period = query.get('period') || 'ytd';
          const flow = query.get('flow');
          const search = query.get('search')?.toLowerCase() || '';
          const group = query.get('accountGroup');
          const groupAccounts = streamAccountGroups.find(item => item.code === group)?.accounts || [];
          let rows = (payload[`recent_rows_${period}`] || []).filter(row => {
            const transfer = row.flow_type === 'transfer' || row.is_internal_transfer;
            if (['cashflow', 'cash_transfer'].includes(flow) && !/^\d+$/.test(row.id)) return false;
            if (['internal_transfer', 'cash_transfer'].includes(flow) && !transfer) return false;
            if (flow === 'cashflow' && !transfer && !['income','expense','personal_loan'].includes(row.flow_type)) return false;
            if (flow === 'income_expense' && !['income','expense'].includes(row.flow_type)) return false;
            if (['income','expense','personal_loan'].includes(flow) && row.flow_type !== flow) return false;
            if (query.get('account') && row.account_code !== query.get('account')) return false;
            if (group && !groupAccounts.some(account => account.code === row.account_code)) return false;
            return Object.values(row).join(' ').toLowerCase().includes(search);
          });
          if (query.get('pairCards') === 'true') {
            const matches = payload.transfer_pairs || [];
            rows = [...new Map(rows.map(row => {
              const pair = matches.find(item => [item.source_row_id, item.target_row_id].includes(row.id));
              const entry = pair ? { id:'transfer-pair-' + pair.id, transaction_date:pair.match_date,
                transfer_pair:pair, amount:pair.amount, amount_cny:pair.amount_cny } : row;
              return [entry.id, entry];
            })).values()];
          }
          const sort = query.get('sort');
          if (sort) rows.sort((a, b) => (sort === 'amount_asc' ? 1 : -1) *
            (Math.abs(a.amount_cny ?? a.amount) - Math.abs(b.amount_cny ?? b.amount)));
          const offset = Number(query.get('offset') || 0);
          const limit = payload.stream_page_size || rows.length;
          return { rows:rows.slice(offset, offset + limit), next_offset:offset + limit < rows.length ? offset + limit : null,
            account_groups:streamAccountGroups };
        };
        if (test.deferReads) return new Promise((resolve, reject) => test.pendingReads.push({ resolve: value => resolve(select(value)), reject }));
        return Promise.resolve(select(structuredClone(test.fixture)));
      }
      if (url === '/api/accounts/overview') return Promise.resolve(structuredClone(test.accounts));
      return Promise.resolve({});
    };
    window.__ledgerWrite = (url, body) => {
      window.__ledgerTest.writes.push({ url, body });
      return new Promise((resolve, reject) => window.__ledgerTest.pendingPosts.push({ resolve, reject }));
    };
  }, { fixture: fixtureData, accounts, streamAccountGroups });
  await context.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.href.includes('chart.js@')) {
      if (chartScript) return route.fulfill({ contentType: 'application/javascript', path: chartScript });
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
  await page.waitForFunction(() => window.__cashflowTrendChart && window.__overviewCategoryPieChart);
  return { page, context };
}

async function capture(page, label, legacy) {
  const snapshot = await page.evaluate(() => {
    const ids = ['ledgerMobileHero', 'categoryInsightTitle', 'overviewCategoryRank', 'transferSummary', 'classificationRules', 'recentAccountScope', 'recentRows'];
    const bars = window.__cashflowTrendChart?.data;
    const net = Object.values(Chart.instances).find(chart => chart.canvas.id === 'cashflowNetTrendChart')?.data;
    const trend = bars && {
      labels: bars.labels,
      series: [...bars.datasets, ...(net?.datasets || [])].map(({ label, data }) => ({ label, data })),
    };
    return { markup: Object.fromEntries(ids.map(id => [id, document.getElementById(id)?.innerHTML])),
      charts: [trend, window.__overviewCategoryPieChart?.data],
      panel: document.body.dataset.ledgerTab, hash: location.hash };
  });
  if (legacy) snapshots.set(label, snapshot);
  else if (baselineEntry) assert.deepEqual(snapshot, snapshots.get(label), label);
}

async function openStreamEdit(page, id) {
  const action = page.locator(`[data-ledger-edit="${id}"]:visible`).first();
  if (!(await action.count())) await page.locator(`[data-detail-target="recentRows-detail-${id}"]`).click();
  await action.click();
}

async function browse(legacy) {
  const { page, context } = await makePage(legacy);
  if (!legacy) {
    assert.equal(await page.locator('#overviewCategoryTotalValue').textContent(), '¥2,250.25');
    const trend = page.locator('.ledgerTrendCard');
    assert.equal(await trend.locator('xpath=preceding-sibling::*').count(), 0);
    assert.equal(await trend.locator('xpath=following-sibling::*').getByRole('heading', { name: '收入排行' }).count(), 1);
    assert.equal(await page.locator('#ledgerTrendNetPeriod').textContent(), '10月');
    assert.equal(await page.locator('#ledgerTrendNetValue').textContent(), '+¥1,225.75');
    if (chartScript) {
      for (const width of [320, 390, 1280]) {
        await page.setViewportSize({ width, height: 1084 });
        await page.waitForFunction(() => {
          const bar = Chart.getChart('cashflowTrendChart');
          const net = Chart.getChart('cashflowNetTrendChart');
          return bar.width === bar.canvas.parentElement.clientWidth &&
            net.width === net.canvas.parentElement.clientWidth &&
            Math.abs(bar.scales.x.getPixelForValue(0) - net.scales.x.getPixelForValue(0)) < 1 &&
            Math.abs(bar.scales.x.getPixelForValue(1) - net.scales.x.getPixelForValue(1)) < 1;
        });
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
      }
      await page.setViewportSize({ width: 390, height: 1084 });
    }
    assert.equal(await page.locator('#overviewCategoryRank .categoryBarRank').first().evaluate(
      el => el.style.getPropertyValue('--ledger-rank-color')), '#3fb950');
  }
  await capture(page, 'overview', legacy);
  await page.locator('[data-overview-period="month"]').click();
  await page.locator('[data-overview-flow="expense"]').click();
  await page.locator('#trendWeekly').click();
  if (!legacy) {
    assert.equal(await page.locator('#overviewCategoryTotalLabel').textContent(), '消费合计');
    assert.equal(await page.locator('#overviewCategoryTotalValue').textContent(), '¥24.50');
    assert.equal(await page.locator('#overviewCategoryRank .categoryBarRank').first().evaluate(
      el => el.style.getPropertyValue('--ledger-rank-color')), '#f85149');
    assert.deepEqual(await page.locator('.ledgerFundingStats strong').allTextContents(), ['¥35.00', '—']);
    assert.equal(await page.locator('[data-overview-period="month"]').getAttribute('aria-pressed'), 'true');
    assert.equal(await page.locator('[data-overview-flow="expense"]').getAttribute('aria-pressed'), 'true');
    assert.equal(await page.locator('#trendWeekly').getAttribute('aria-pressed'), 'true');
    assert.equal(await page.locator('#ledgerTrendNetPeriod').textContent(), '10/19');
    assert.deepEqual(await page.evaluate(() => [
      window.__cashflowTrendChart.data.labels,
      Object.values(Chart.instances).find(chart => chart.canvas.id === 'cashflowNetTrendChart').data.labels,
    ]), [['2026-10-19'], ['2026-10-19']]);
  }
  await capture(page, 'month-expense-weekly', legacy);
  await page.locator('[data-ledger-tab="recent"]').click();
  if (!legacy) {
    const viewport = page.viewportSize();
    await page.setViewportSize({ width: 320, height: viewport.height });
    const bar = await page.locator('.ledgerRecentBar').evaluate(element => {
      const chips = element.querySelector('#recentFlowChips');
      const buttons = [...element.querySelectorAll('.ledgerFlowChip, .ledgerIconButton')];
      const controls = buttons.map(button => button.getBoundingClientRect());
      return {
        ordered: controls.slice(1).every((rect, index) =>
          rect.top >= controls[index].bottom || (Math.abs(rect.top - controls[index].top) < 1 && rect.left >= controls[index].right)),
        fits: chips.scrollWidth <= chips.clientWidth && controls.at(-1).right <= window.innerWidth,
        borderless: buttons.every(button => {
          const style = getComputedStyle(button);
          return style.borderTopWidth === '0px' && style.backgroundColor === 'rgba(0, 0, 0, 0)';
        }),
        selectedUnderline: getComputedStyle(chips.querySelector('[aria-pressed="true"]'), '::after').height === '2px',
      };
    });
    assert.deepEqual(bar, { ordered: true, fits: true, borderless: true, selectedUnderline: true });
    await page.setViewportSize(viewport);
    assert.equal(await page.locator('[data-ledger-flow-filter="internal_transfer"]').isVisible(), true);
    await page.locator('#recentRows .ledgerListRow').first().waitFor();
    assert.deepEqual(await page.locator('#recentRows .ledgerListRow').evaluateAll(
      rows => rows.map(row => row.dataset.detailTarget)), ['recentRows-detail-1', 'recentRows-detail-2', 'recentRows-detail-4', 'recentRows-detail-5']);
  }
  await page.locator('[data-ledger-flow-filter=""]').click();
  await page.locator('#recentSearchToggle').click();
  await page.locator('[data-recent-search-input]').fill('测试商户');
  await page.locator('[data-recent-search-form] button[type="submit"]').click();
  await capture(page, 'search', legacy);
  await page.locator('#recentSearchToggle').click(); await page.locator('[data-recent-search-clear]').click();
  await page.locator('#recentSortToggle').click(); await page.locator('[data-recent-sort-option="amount_desc"]').click();
  await capture(page, 'sorted', legacy);
  await openStreamEdit(page, '1');
  await page.locator('[data-ledger-edit-form]').waitFor();
  const editTemplate = await page.locator('[data-ledger-edit-form]').innerHTML();
  if (legacy) snapshots.set('editTemplate', editTemplate); else if (baselineEntry) assert.equal(editTemplate, snapshots.get('editTemplate'));
  await page.locator('[data-ledger-edit-form] [data-sheet-close]').click();
  assert.equal(await page.evaluate(() => __ledgerTest.writes.length), 0);
  await context.close();
}

async function checkConfirmedSubtotal() {
  const partial = structuredClone(fixture);
  Object.assign(partial.periods.ytd, {
    income: null, expense: null, disposable_net: null, cash_net: null,
    unclassified_count: 12, pending_count: 13,
    confirmed_subtotal: { income: 2250.25, expense: 124.5, net: 2125.75, cash_net: 1925.75 },
  });
  partial.monthly_trend = [
    { period: '2026-09', unclassified_count: 5, income: null, expense: null, net: null,
      confirmed_subtotal: { income: null, expense: null, net: null } },
    { period: '2026-10', unclassified_count: 7, income: null, expense: null, net: null,
      confirmed_subtotal: { income: 2250.25, expense: 124.5, net: 2125.75 } },
  ];
  const { page, context } = await makePage(false, partial);
  assert.equal(await page.locator('.ledgerMobileValue').textContent(), '+¥2,125.75');
  assert.equal(await page.locator('#overviewCategoryTotalValue').textContent(), '¥2,250.25');
  assert.equal(await page.locator('.ledgerMobileStat').filter({ hasText: '待分类' }).locator('strong').textContent(), '12');
  assert.equal(await page.locator('#ledgerTrendCoverage').isVisible(), true);
  assert.deepEqual(await page.evaluate(() => {
    const net = Object.values(Chart.instances).find(chart => chart.canvas.id === 'cashflowNetTrendChart');
    return [...window.__cashflowTrendChart.data.datasets, ...net.data.datasets].map(series => series.data);
  }),
    [[null, 2250.25], [null, 124.5], [null, 2125.75]]);
  await context.close();
}

async function checkNarrowSummaryGeometry() {
  const layoutFixture = structuredClone(fixture);
  Object.assign(layoutFixture.periods.ytd, {
    income: 123456789012.34,
    expense: 9876543210.98,
    disposable_net: 113580245801.36,
  });
  const { page, context } = await makePage(false, layoutFixture);
  const formatExpected = (value, signed = false) => {
    const number = Math.abs(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    if (!signed) return `¥${number}`;
    return `${value < 0 ? '-' : '+'}¥${number}`;
  };
  const expectedValues = [
    formatExpected(layoutFixture.periods.ytd.income),
    formatExpected(-Math.abs(layoutFixture.periods.ytd.expense), true),
    String(layoutFixture.periods.ytd.pending_count),
    formatExpected(layoutFixture.periods.ytd.investment_funding),
    formatExpected(layoutFixture.periods.ytd.cash_net, true),
  ];
  const widths = [320, 360, 389, 1280];
  for (const width of widths) {
    await page.setViewportSize({ width, height: 1084 });
    await page.evaluate(() => new Promise(requestAnimationFrame));
    const metrics = await page.evaluate(() => {
      const items = [...document.querySelectorAll('.ledgerMobileStat strong')].map(element => {
        const range = document.createRange();
        range.selectNodeContents(element);
        const rects = [...range.getClientRects()].map(rect => ({ left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom }));
        const cell = element.getBoundingClientRect();
        return { text: element.textContent, rects, cell: { left: cell.left, right: cell.right }, overflow: element.scrollWidth > element.clientWidth };
      });
      let overlaps = false;
      for (let i = 0; i < items.length; i += 1) {
        for (let j = i + 1; j < items.length; j += 1) {
          for (const a of items[i].rects) for (const b of items[j].rects) {
            if (a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom) overlaps = true;
          }
        }
      }
      return { viewport: innerWidth, pageScrollWidth: document.documentElement.scrollWidth,
        pageClientWidth: document.documentElement.clientWidth, overlaps,
        items: items.map(item => ({ text: item.text, lines: item.rects.length, overflow: item.overflow,
          insideCell: item.rects.every(rect => rect.left >= item.cell.left - 0.5 && rect.right <= item.cell.right + 0.5) })) };
    });
    assert.equal(metrics.viewport, width);
    assert.ok(metrics.pageScrollWidth <= metrics.pageClientWidth, `page horizontal overflow at ${width}px`);
    assert.equal(metrics.overlaps, false, `summary values overlap at ${width}px`);
    assert.equal(metrics.items.length, expectedValues.length);
    for (const [index, item] of metrics.items.entries()) {
      assert.equal(item.text, expectedValues[index], `summary value is incomplete at ${width}px`);
      if (width <= 389) assert.equal(item.lines, 1, `summary amount wraps at ${width}px`);
      assert.equal(item.overflow, false, `summary amount overflows its cell at ${width}px`);
      assert.equal(item.insideCell, true, `summary amount exceeds its cell at ${width}px`);
    }
  }
  await context.close();
  return widths;
}

async function checkTransferLayout() {
  const transferFixture = structuredClone(fixture);
  const cardPayment = {
    match_date: '2026-09-27', source_date: '2026-09-27', target_date: '2026-09-28',
    from_account_code: 'ccb_7332', to_account_code: 'ccb_credit_0453',
    match_type: 'credit_card_payment', amount: '4567.89', currency_norm: 'CNY',
    source: { summary: '合成还款支出' }, target: { summary: '合成还款入账' },
  };
  transferFixture.month = '2026-11';
  transferFixture.transfer_pairs = [cardPayment, {
    match_date: '2026-08-27', source_date: '2026-08-27', target_date: '2026-08-27',
    from_account_code: 'santander', to_account_code: 'ibkr',
    match_type: 'investment_funding', amount: '65000.00', currency_norm: 'MXN', amount_cny: null,
  }];
  const { page: transferPage, context: transferContext } = await makePage(false, transferFixture);
  assert.deepEqual(await transferPage.locator('.ledgerAnchorNav a').allTextContents(), ['总览','流水']);
  await transferPage.locator('[data-ledger-tab="recent"]').click();
  const reviewActions = transferPage.locator('#transferReviewActions');
  assert.equal(await reviewActions.isVisible(), false);
  await transferPage.locator('[data-ledger-flow-filter="internal_transfer"]').click();
  const confirmed = reviewActions.getByRole('button', { name:'已确认', exact:true });
  const pending = reviewActions.getByRole('button', { name:'待配对', exact:true });
  await confirmed.click();
  const payment = transferPage.locator('#transferConfirmed .ledgerPair').first();
  const route = payment.locator('summary');
  const sender = route.locator('[data-direction="out"]');
  const receiver = route.locator('[data-direction="in"]');
  await sender.getByText('7332 储蓄卡').waitFor();
  await receiver.getByText('0453 信用卡').waitFor();
  assert.deepEqual(await transferPage.locator('#transferConfirmed .ledgerStreamDayHeader time').evaluateAll(
    dates => dates.map(date => date.dateTime)), ['2026-09-27', '2026-08-27']);
  assert.equal(await transferPage.evaluate(() => __ledgerTest.writes.length), 0);
  for (const party of [sender, receiver]) {
    assert.equal(await party.locator('img').evaluate(async image => {
      await image.decode();
      return image.naturalWidth > 0 && new URL(image.src).pathname.endsWith('/account-brands/ccb.jpg');
    }), true);
  }
  assert.match(await route.getAttribute('aria-label'), /转出 建行 7332 储蓄卡，转入 建行 0453 信用卡/);
  assert.equal(await route.locator('time').getAttribute('datetime'), '2026-09-27');
  assert.match(await route.innerText(), /4,567\.89/);
  const investment = transferPage.locator('#transferConfirmed .ledgerPair').nth(1);
  await investment.locator('summary').click();
  await investment.getByText('人民币折算待补').waitFor();
  assert.match(await investment.locator('summary').innerText(), /65,000\.00/);
  for (const width of [320, 390, 1280]) {
    await transferPage.setViewportSize({ width, height: 900 });
    assert.equal(await route.evaluate(summary => {
      const rect = selector => summary.querySelector(selector).getBoundingClientRect();
      const sender = rect('[data-direction="out"]');
      const center = rect('.ledgerPairCenter');
      const receiver = rect('[data-direction="in"]');
      const outgoingArrow = rect('.ledgerPairArrow:not(.is-left)');
      const incomingArrow = rect('.ledgerPairArrow.is-left');
      const date = summary.querySelector('time');
      return summary.scrollWidth <= summary.clientWidth && sender.right <= center.left && center.right <= receiver.left
        && incomingArrow.top - rect('.is-in').bottom === 1
        && outgoingArrow.top - incomingArrow.bottom === 3
        && rect('.is-out').top - outgoingArrow.bottom === 1
        && outgoingArrow.width >= 104
        && outgoingArrow.bottom < date.getBoundingClientRect().top;
    }), true, `transfer row at ${width}px`);
  }
  await route.focus();
  await route.press('Enter');
  await payment.getByText('合成还款支出').waitFor();
  await payment.getByText('合成还款入账').waitFor();
  await pending.click();
  await transferPage.waitForFunction(() => __ledgerTest.pendingPosts.length === 1);
  await transferPage.evaluate(pair => __ledgerTest.pendingPosts.shift().resolve({
    matches:[{...pair, source_kind:'trade', target_kind:'cashflow', source_row_id:320, target_row_id:1843,
      status:'review_required', confirmation_supported:true, ambiguity_status:'ambiguous',
      match_key:'candidate:redemption:320:1843'}],
  }), cardPayment);
  await transferPage.locator('#transferPending .ledgerStreamDayHeader time').waitFor();
  assert.equal(await transferPage.locator('#transferPending .ledgerStreamDayHeader time').getAttribute('datetime'), '2026-09-27');
  assert.equal(await payment.isVisible(), false);
  await transferPage.locator('#transferPending .ledgerPair summary').click();
  const confirmPair = transferPage.getByRole('button',{name:'确认这两笔配对'});
  transferPage.once('dialog', dialog => dialog.dismiss());
  await confirmPair.click();
  assert.equal(await transferPage.evaluate(() => __ledgerTest.pendingPosts.length), 0);
  transferPage.once('dialog', dialog => dialog.accept());
  await confirmPair.click();
  await transferPage.waitForFunction(() => __ledgerTest.pendingPosts.length === 1);
  assert.deepEqual(await transferPage.evaluate(() => __ledgerTest.writes.at(-1).body), {
    from_kind:'trade',from_id:320,to_kind:'cashflow',to_id:1843,
    note:'在收支内部划转页人工核对确认',confirmed:true,
  });
  await transferPage.evaluate(() => __ledgerTest.pendingPosts.shift().reject(new Error('模拟未确认')));
  await transferPage.locator('#transferPending [data-pair-status]').getByText('模拟未确认').waitFor();
  assert.equal(await transferPage.evaluate(() => __ledgerTest.writes.filter(write => write.url.endsWith('/transfers/confirm')).length), 1);
  await pending.click();
  await transferPage.locator('#recentRows .ledgerStreamDayHeader time').waitFor();
  assert.equal(await transferPage.locator('#transferReviewPanel').isVisible(), false);
  await transferPage.locator('[data-ledger-tab="overview"]').click();
  await transferPage.evaluate(() => { location.hash = '#transfers'; });
  await payment.waitFor();
  assert.equal(await transferPage.locator('a[data-ledger-tab="recent"]').getAttribute('aria-current'), 'page');
  assert.equal(await transferPage.evaluate(() => location.hash), '#transfers');
  await transferPage.reload();
  await payment.waitFor();
  assert.equal(await confirmed.getAttribute('aria-expanded'), 'true');
  assert.equal(await transferPage.evaluate(() => __ledgerTest.writes.some(write => write.url.endsWith('/transfers/confirm'))), false);
  await transferContext.close();
}

async function checkStreamLayout() {
  const stream = structuredClone(fixture);
  stream.stream_page_size = 1;
  const extra = { ...stream.recent_rows_ytd[0], id:'6', summary:'待核对外币', amount:-54321.23, amount_cny:null,
    currency:'USD', subcategory_pending:true };
  stream.recent_rows_ytd.splice(1, 0, extra);
  const cardExpense = {...extra, id:'7', account_code:'ccb_credit_0453', amount:-600, amount_cny:-600,
    currency:'CNY', summary:'信用卡测试消费', subcategory_pending:false};
  stream.recent_rows_ytd.push(cardExpense, {...cardExpense, id:'8', account_code:'ibkr', amount_cny:-700, summary:'其他账户消费'});
  stream.recent_rows_month.push(cardExpense);
  const outflow = stream.recent_rows_ytd.find(row => row.id === '4');
  outflow.account_code = 'tencent_licaitong_current_plus';
  stream.recent_rows_ytd.push({...outflow,
    id:'9', account_code:'ccb_credit_0453', amount:200, amount_cny:200});
  stream.transfer_pairs = [{
    id:'pair-1', source_row_id:'4', target_row_id:'9',
    match_date:'2026-09-28', source_date:'2026-09-28', target_date:'2026-09-28',
    from_account_code:'tencent_licaitong_current_plus', to_account_code:'ccb_credit_0453',
    match_type:'credit_card_payment', amount:200, amount_cny:200, currency_norm:'CNY',
    source:{summary:'合成划转转出'}, target:{summary:'合成划转转入'},
  }];
  const {page, context} = await makePage(false, stream);
  await page.locator('[data-ledger-tab="recent"]').click();
  const list = page.locator('#recentRows');
  const first = list.locator('.ledgerListRow').first();
  await first.waitFor();
  assert.match(await list.locator('.ledgerStreamDayHeader').innerText(), /已加载 1 笔/);
  await list.getByRole('button', {name:'展开更多'}).click();
  await list.getByText('待补分类').waitFor();
  assert.equal(await list.locator('.ledgerStreamDay').count(), 1);
  assert.equal(await list.locator('.ledgerListRow').count(), 2);
  assert.match(await list.locator('.ledgerListSide b').nth(1).innerText(), /54,321\.23/);
  for (const width of [320,390,1280]) {
    await page.setViewportSize({width,height:900});
    assert.equal(await first.evaluate(row => {
      const [icon, main, amount] = Array.from(row.children);
      return row.scrollWidth <= row.clientWidth && main.getBoundingClientRect().right <= amount.getBoundingClientRect().left
        && getComputedStyle(icon).backgroundColor === 'rgb(255, 188, 32)'
        && getComputedStyle(icon.querySelector('svg')).stroke === 'rgb(255, 255, 255)';
    }), true, `stream row at ${width}px`);
  }
  await page.setViewportSize({width:390,height:900});
  await first.press('Enter');
  await page.getByText('测试商户',{exact:true}).filter({visible:true}).first().waitFor();
  await page.locator('[data-ledger-edit="1"]:visible').click();
  await page.locator('[data-ledger-edit-form] [data-sheet-close]').click();
  await page.getByRole('button',{name:'收入',exact:true}).click();
  await list.getByText('工资入账').waitFor();
  assert.equal(await list.getByText('待补分类').count(),0);
  await page.locator('#recentSortToggle').click();
  await page.locator('[data-recent-sort-option="amount_desc"]').click();
  await list.locator('.ledgerListDate').waitFor();
  assert.equal(await list.locator('.ledgerStreamDay').count(),0);

  const toggle = page.locator('#recentAccountToggle');
  const picker = page.locator('#recentAccountPicker');
  await page.locator('[data-ledger-flow-filter="expense"]').click();
  await page.setViewportSize({width:320,height:680});
  await toggle.click();
  assert.equal(await toggle.getAttribute('aria-expanded'), 'true');
  const bounds = await picker.boundingBox();
  assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= 320 && bounds.height <= 320);
  await page.keyboard.press('Escape');
  assert.equal(await picker.isVisible(), false);
  assert.equal(await toggle.evaluate(button => button === document.activeElement), true);
  await toggle.click();
  await picker.getByRole('button',{name:'建设银行',exact:true}).click();
  await list.getByText('待核对外币').first().waitFor();
  await page.waitForFunction(() => document.querySelector('#recentAccountToggle img')?.naturalWidth > 0);
  await list.getByRole('button',{name:'展开更多'}).click();
  await list.getByRole('button',{name:/信用卡测试消费/}).waitFor();
  assert.equal(await list.getByText('其他账户消费').count(), 0);
  await toggle.click();
  assert.equal(await picker.getByRole('button',{name:'建设银行',exact:true}).getAttribute('aria-pressed'), 'true');
  await picker.getByRole('button',{name:'0453 信用卡',exact:true}).click();
  await list.getByRole('button',{name:/信用卡测试消费/}).waitFor();
  assert.equal(await list.locator('.ledgerListRow').count(), 1);
  assert.match(await page.locator('#recentAccountLabel').textContent(), /0453/);

  await toggle.click();
  await page.locator('#recentPeriodToggle').click();
  assert.equal(await picker.isVisible(), false);
  await page.locator('#recentPeriodForm').getByText('本月至今').click();
  await page.locator('#recentPeriodForm').getByRole('button',{name:'确认'}).click();
  await list.getByRole('button',{name:/信用卡测试消费/}).waitFor();
  assert.deepEqual(await page.evaluate(() => {
    const query = new URL(__ledgerTest.reads.at(-1).url, location.origin).searchParams;
    return ['account', 'accountGroup', 'period', 'flow', 'sort', 'offset'].map(key => query.get(key));
  }), ['ccb_credit_0453', null, 'month', 'expense', 'amount_desc', '0']);
  await toggle.click();
  await picker.getByRole('button',{name:'全部账户',exact:true}).click();
  assert.equal(await page.locator('#recentAccountLabel').textContent(), '');
  await list.getByRole('button',{name:/信用卡测试消费/}).waitFor();

  const transfers = page.getByRole('button',{name:'内部划转',exact:true});
  await page.locator('#recentSortToggle').click();
  await page.locator('[data-recent-sort-option=""]').click();
  await transfers.click();
  assert.equal(await transfers.getAttribute('aria-pressed'), 'true');
  await list.getByText('暂无流水').waitFor();
  await page.locator('#recentPeriodToggle').click();
  await page.locator('#recentPeriodForm').getByText('年初至今').click();
  await page.locator('#recentPeriodForm').getByRole('button',{name:'确认'}).click();
  const pairCard = list.locator('.ledgerPair');
  await pairCard.locator('summary').waitFor();
  assert.equal(await pairCard.count(),1);
  assert.equal(await list.locator('.ledgerStreamDayHeader time').getAttribute('datetime'),'2026-09-28');
  assert.equal(await pairCard.locator('summary img').count(),2);
  assert.match(await pairCard.locator('summary').getAttribute('aria-label'), /转出 腾讯 理财通活期\+/);
  assert.match(await pairCard.locator('[data-direction="out"] img').getAttribute('src'), /account-brands\/wechat\.jpg$/);
  assert.equal(await pairCard.locator('.ledgerPairArrow:visible').count(),2);
  assert.equal(await list.getByRole('button',{name:'展开更多'}).count(),0);
  const pairSummary = await pairCard.locator('summary').innerHTML();
  await pairCard.locator('summary').click();
  await pairCard.getByText('合成划转转入').waitFor();

  await page.locator('[data-ledger-flow-filter=""]').click();
  await list.locator('.ledgerListRow').first().waitFor();
  for (let pageIndex = 0; pageIndex < stream.recent_rows_ytd.length && !(await pairCard.count()); pageIndex++) {
    const count = await list.locator('.ledgerListRow').count();
    await list.getByRole('button', {name:'展开更多'}).click();
    await page.waitForFunction(previous =>
      document.querySelectorAll('#recentRows .ledgerListRow').length > previous
        || document.querySelector('#recentRows .ledgerPair'), count);
  }
  assert.equal(await pairCard.count(),1);
  assert.equal(await pairCard.locator('summary').innerHTML(),pairSummary);
  assert.equal(await list.locator('[data-detail-target="recentRows-detail-4"], [data-detail-target="recentRows-detail-9"]').count(),0);
  await pairCard.locator('summary').click();
  await pairCard.getByText('合成划转转入').waitFor();

  await transfers.click();
  await pairCard.locator('summary').waitFor();
  await toggle.click();
  await picker.getByRole('button',{name:'IBKR 证券账户',exact:true}).click();
  await list.getByText('暂无流水').waitFor();
  assert.equal(await page.evaluate(() => {
    const query = new URL(__ledgerTest.reads.at(-1).url, location.origin).searchParams;
    return query.get('flow') + ':' + query.get('account') + ':' + query.get('pairCards');
  }), 'cash_transfer:ibkr:true');
  assert.equal(await page.evaluate(() => __ledgerTest.writes.length),0);
  await context.close();
}

async function checkPersonalLoanNavigation() {
  const loans = structuredClone(fixture);
  const row = {
    id:'11', transaction_date:'2026-10-02', account_code:'ccb_7332', currency:'CNY',
    flow_type:'personal_loan', amount:-500, amount_cny:-500, subcategory:'归还借款本金', summary:'还款测试',
  };
  loans.recent_rows_ytd.push(row);
  loans.recent_rows_month.push(row);
  loans.personal_loan_recent = { month:[row], ytd:[row] };
  for (const period of ['month','ytd']) {
    loans.periods[period].personal_loan = { borrowed:0, repaid:500, net:-500, count:1 };
  }
  const {page, context} = await makePage(false, loans);
  await page.setViewportSize({width:320,height:820});
  await page.locator('[data-overview-period="month"]').click();
  assert.equal(await page.locator('#overviewLoanRepaid').textContent(),'¥500.00');
  assert.equal(await page.locator('#overviewLoanRows').isVisible(),false);
  await page.locator('#overviewLoanDetails summary').click();
  await page.locator('#overviewLoanRows').getByText('归还借款本金').waitFor();
  await page.locator('#overviewLoanJump').click();
  const tab = page.getByRole('button',{name:'借还款',exact:true});
  assert.equal(await tab.getAttribute('aria-pressed'),'true');
  assert.equal(await page.locator('#recentPeriodLabel').textContent(),'本月至今');
  const list = page.locator('#recentRows');
  await list.getByRole('button',{name:/归还借款本金/}).waitFor();
  const controls = ['#recentPeriodToggle','#recentSearchToggle','#recentSortToggle','#recentAccountToggle'];
  for (const id of controls) {
    const box = await page.locator(id).boundingBox();
    assert.ok(box && box.x >= 0 && box.x + box.width <= 320, id);
  }
  await page.locator('#recentSearchToggle').click();
  await page.locator('[data-recent-search-input]').fill('还款测试');
  await page.locator('[data-recent-search-form] button[type="submit"]').click();
  await list.getByRole('button',{name:/归还借款本金/}).waitFor();
  await page.locator('#recentSortToggle').click();
  await page.locator('[data-recent-sort-option="amount_desc"]').click();
  await list.locator('.ledgerListDate').waitFor();
  await page.locator('#recentAccountToggle').click();
  await page.locator('#recentAccountPicker').getByRole('button',{name:'IBKR 证券账户',exact:true}).click();
  await list.getByText('暂无流水').waitFor();
  assert.deepEqual(await page.evaluate(() => {
    const query = new URL(__ledgerTest.reads.at(-1).url, location.origin).searchParams;
    return ['flow','period','search','sort','account'].map(key => query.get(key));
  }), ['personal_loan','month','还款测试','amount_desc','ibkr']);
  await context.close();
}

try {
  if (!streamOnly) await checkTransferLayout();
  if (!transfersOnly) {
    await checkStreamLayout();
    await checkPersonalLoanNavigation();
  }
  if (transfersOnly || streamOnly) {
    assert.deepEqual(errors, []);
    console.log(transfersOnly ? 'Transfer layout and details passed' : 'Stream day groups, pagination, icons, amounts and edit access passed');
  } else await checkLedgerLifecycle();
} finally { await browser.close(); }

async function checkLedgerLifecycle() {
  if (baselineEntry) await browse(true);
  await browse(false);
  await checkConfirmedSubtotal();
  const mobileSummaryWidths = await checkNarrowSummaryGeometry();
  const { page, context } = await makePage(false);
  await page.locator('[data-overview-period="month"]').click();
  await page.locator('[data-overview-flow="expense"]').click();
  await page.locator('#trendWeekly').click();
  await page.locator('[data-ledger-tab="recent"]').click();
  await openStreamEdit(page, '1');
  await page.locator('[data-ledger-edit-form]').waitFor();
  await page.locator('[data-edit-field="counterparty"]').fill('  新测试商户  ');
  await page.evaluate(() => {
    const form = document.querySelector('[data-ledger-edit-form]');
    form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  });
  assert.deepEqual(await page.evaluate(() => __ledgerTest.writes), [{ url: '/api/ledger/rows/1/update', body: { cashflow_type: 'operating_expense', category: '餐饮', subcategory: '工作餐', counterparty: '新测试商户' } }]);
  await page.evaluate(() => __ledgerTest.pendingPosts.shift().reject(new Error('fixture rejection')));
  await page.waitForFunction(() => !document.querySelector('[data-ledger-edit-form] button[type="submit"]').disabled);
  assert.match(await page.locator('#err').textContent(), /fixture rejection/);
  await page.locator('[data-ledger-edit-form] button[type="submit"]').click();
  assert.equal(await page.evaluate(() => __ledgerTest.writes.length), 2);
  const readsBeforeSave = await page.evaluate(() => __ledgerTest.reads.length);
  await page.evaluate(() => __ledgerTest.pendingPosts.shift().resolve({ ok: true }));
  await page.waitForFunction(() => !document.querySelector('[data-ledger-edit-form]'));
  await page.waitForFunction(count => __ledgerTest.reads.length > count, readsBeforeSave);
  assert.deepEqual(await page.evaluate(count => __ledgerTest.reads.slice(count).map(({ url }) =>
    new URL(url, location.origin).searchParams.get('view')), readsBeforeSave), ['stream']);

  // A cancelled form's rejected POST must not alter a newer sheet or its error state.
  await openStreamEdit(page, '1');
  await page.locator('[data-ledger-edit-form] button[type="submit"]').click();
  await page.locator('[data-ledger-edit-form] [data-sheet-close]').click();
  await page.locator('#recentSearchToggle').click();
  const errorBeforeCancel = await page.locator('#err').textContent();
  await page.evaluate(() => __ledgerTest.pendingPosts.shift().reject(new Error('cancelled old form')));
  await page.waitForTimeout(20);
  assert.equal(await page.locator('[data-recent-search-form]').count(), 1);
  assert.equal(await page.locator('#err').textContent(), errorBeforeCancel);
  await page.keyboard.press('Escape');

  // An older read in the same controller cannot overwrite a newer response.
  await page.locator('[data-ledger-tab="overview"]').click();
  await page.waitForFunction(() => __ledgerPage.controller.state.data?.periods.ytd.income === 2250.25);
  await page.evaluate(() => {
    __ledgerTest.deferReads = true;
    __older = __ledgerPage.controller.loadWorkbench().then(() => 'resolved', error => error.name);
    __newer = __ledgerPage.controller.loadWorkbench();
    const next = structuredClone(__ledgerTest.fixture); next.periods.ytd.income = 9999;
    __ledgerTest.pendingReads[1].resolve(next);
  });
  await page.waitForFunction(() => __ledgerPage.controller.state.data.periods.ytd.income === 9999);
  await page.evaluate(() => __ledgerTest.pendingReads[0].resolve(structuredClone(__ledgerTest.fixture)));
  await page.evaluate(() => Promise.all([__older, __newer]));
  assert.equal(await page.evaluate(() => __older), 'AbortError');
  assert.equal(await page.evaluate(() => __ledgerPage.controller.state.data.periods.ytd.income), 9999);
  await page.evaluate(() => { __ledgerTest.deferReads = false; __ledgerTest.pendingReads = []; });

  // Preserve ordinary view state over BFCache, never resume the edit operation.
  await page.locator('[data-ledger-tab="recent"]').click();
  await page.locator('#recentSortToggle').click(); await page.locator('[data-recent-sort-option="amount_desc"]').click();
  await page.locator('[data-ledger-flow-filter="expense"]').click();
  const periodToggle = page.locator('#recentPeriodToggle');
  const periodForm = page.locator('#recentPeriodForm');
  assert.equal(await page.locator('#recentAccountPicker').isVisible(), false);
  await periodToggle.click();
  await periodForm.getByText('本月至今', { exact: true }).click();
  assert.equal(await periodForm.getByLabel('本月至今').isChecked(), true);
  assert.equal(await periodForm.locator('label').filter({ hasText: '本月至今' }).locator('svg').isVisible(), true);
  assert.equal(await periodForm.locator('label').filter({ hasText: '年初至今' }).locator('svg').isVisible(), false);
  assert.equal(await page.evaluate(() => __ledgerPage.controller.state.recentPeriod), 'ytd');
  await page.keyboard.press('Escape');
  assert.equal(await periodForm.isVisible(), false);
  await periodToggle.click();
  assert.equal(await periodForm.getByLabel('年初至今').isChecked(), true);
  await periodForm.getByText('本月至今', { exact: true }).click();
  await periodForm.getByRole('button', { name: '确认' }).click();
  assert.equal(await periodForm.isVisible(), false);
  assert.equal(await periodToggle.getAttribute('aria-expanded'), 'false');
  assert.equal(await page.evaluate(() => __ledgerPage.controller.state.recentPeriod), 'month');
  await page.locator('#recentSearchToggle').click();
  await page.locator('[data-recent-search-input]').fill('测试');
  await page.locator('[data-recent-search-form] button[type="submit"]').click();
  await openStreamEdit(page, '1');
  await page.locator('[data-ledger-edit-form] button[type="submit"]').click();
  const writesBefore = await page.evaluate(() => __ledgerTest.writes.length);
  const viewBefore = await page.evaluate(async () => {
    const { snapshotLedgerViewState } = await import('/assets/js/features/ledger/state.js');
    return snapshotLedgerViewState(__ledgerPage.controller.state);
  });
  await page.evaluate(() => { __oldController = __ledgerPage.controller; window.dispatchEvent(new PageTransitionEvent('pagehide', { persisted: true })); });
  assert.equal(await page.evaluate(() => Object.keys(Chart.instances).length), 0);
  assert.equal(await page.evaluate(() => __oldController.disposed), true);
  await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true })));
  await page.evaluate(() => __ledgerPage.ready);
  const viewAfter = await page.evaluate(async () => {
    const { snapshotLedgerViewState } = await import('/assets/js/features/ledger/state.js');
    return snapshotLedgerViewState(__ledgerPage.controller.state);
  });
  assert.deepEqual(viewAfter, viewBefore);
  assert.equal(await page.evaluate(() => Object.keys(Chart.instances).length), 0);
  assert.equal(await page.evaluate(() => __ledgerTest.writes.length), writesBefore);
  await page.locator('#recentSearchToggle').click();
  await page.evaluate(() => __ledgerTest.pendingPosts.shift().resolve({ ok: true }));
  await page.waitForTimeout(20);
  assert.equal(await page.locator('[data-recent-search-form]').count(), 1);
  await page.keyboard.press('Escape');
  assert.equal(await page.evaluate(() => __ledgerTest.writes.length), writesBefore);

  // Mounting again is idempotent; explicit disposal allows a fresh owner.
  await page.locator('[data-ledger-tab="overview"]').click();
  await page.waitForFunction(() => Object.keys(Chart.instances).length === 3);
  await page.evaluate(async () => {
    __ledgerTest.deferReads = true;
    __oldLoading = __ledgerPage.controller.loadWorkbench().then(() => 'resolved', error => error.name);
    const { mountLedgerPage } = await import('/assets/js/features/ledger/page.js');
    if (mountLedgerPage() !== __ledgerPage) throw new Error('Repeated mount must reuse the page');
    if (__ledgerTest.pendingReads.length !== 1) throw new Error('Repeated mount issued another read');
    __ledgerPage.dispose();
    __ledgerPage = mountLedgerPage();
    __ledgerTest.pendingReads[1].resolve(structuredClone(__ledgerTest.fixture));
  });
  await page.evaluate(() => __ledgerPage.ready);
  await page.evaluate(() => __ledgerTest.pendingReads[0].resolve({ periods: { ytd: { income: 111 } } }));
  assert.equal(await page.evaluate(() => __oldLoading), 'AbortError');
  assert.equal(await page.evaluate(() => __ledgerPage.controller.state.data.periods.ytd.income), 2250.25);
  assert.equal(await page.evaluate(() => Object.keys(Chart.instances).length), 3);
  const requestCount = await page.evaluate(() => __ledgerTest.reads.length);
  await page.evaluate(() => { __disposed = __ledgerPage.controller; __ledgerPage.dispose(); document.querySelector('[data-overview-period="month"]').click(); window.dispatchEvent(new HashChangeEvent('hashchange')); });
  assert.equal(await page.evaluate(() => Object.keys(Chart.instances).length), 0);
  assert.equal(await page.evaluate(() => __ledgerTest.reads.length), requestCount);
  assert.equal(await page.evaluate(() => __disposed.loadWorkbench()), null);
  assert.equal(await page.evaluate(() => __ledgerTest.writes.length), writesBefore);

  // A mounted element queries and binds only its own subtree.
  await page.evaluate(async () => {
    __ledgerTest.deferReads = false;
    const { mountLedgerPage } = await import('/assets/js/features/ledger/page.js');
    const outside = document.createElement('button'); outside.id = 'outside-ledger'; outside.dataset.overviewPeriod = 'month'; document.body.append(outside);
    __ledgerPage = mountLedgerPage({ root: document.querySelector('.wrap') });
    await __ledgerPage.ready;
    outside.click();
  });
  assert.equal(await page.evaluate(() => __ledgerPage.controller.state.overviewPeriod), 'ytd');
  assert.equal(await page.evaluate(() => Object.keys(Chart.instances).length), 3);
  await page.evaluate(() => __ledgerPage.dispose());
  assert.equal(await page.evaluate(() => Object.keys(Chart.instances).length), 0);
  assert.deepEqual(errors, []);
  await context.close();

  const hintFixture = structuredClone(fixture);
  const [hinted, unknown] = hintFixture.recent_rows_ytd;
  const brokerInterest = hintFixture.recent_rows_ytd.find(row => row.id === '5');
  Object.assign(brokerInterest, { category: '消费', subcategory: '券商利息', summary: '券商利息' });
  hintFixture.recent_rows_ytd.push({...brokerInterest, id:'80', cashflow_type:'fee',
    category:'金融费用', subcategory:'银行费用', classification_status:'confirmed', summary:'合成银行费用'});
  for (const row of [hinted, unknown]) Object.assign(row, {
    classification_status: 'pending', subcategory_pending: true, account_flow_type: row.flow_type, coarse_category: '待分类',
    cashflow_type: null, category: null, subcategory: null, needs_review: true,
  });
  hinted.classification_hint = {
    cashflow_type: 'operating_expense', category: '交通出行', subcategory: '打车',
    matched_keyword: '网约车',
  };
  const hintPage = await makePage(false, hintFixture);
  await hintPage.page.locator('[data-ledger-tab="recent"]').click();
  await openStreamEdit(hintPage.page, '5');
  const form = hintPage.page.locator('[data-ledger-edit-form]');
  const type = form.locator('[data-edit-field="flow_group"]');
  const transferType = form.locator('[data-edit-field="transfer_type"]');
  assert.deepEqual((await type.locator('option').allTextContents()).map(label => label.trim()),
    ['消费', '收入', '内部转账', '归还借款本金']);
  const category = form.locator('[data-edit-field="category"]');
  const subcategory = form.locator('[data-edit-field="subcategory"]');
  assert.equal(await type.inputValue(), 'operating_expense');
  assert.equal(await category.inputValue(), '金融费用');
  assert.equal(await subcategory.inputValue(), '券商利息');
  assert.equal(await form.getByRole('button', { name: '消费', exact: true }).count(), 0);
  await form.locator('[data-sheet-close]').click();
  await openStreamEdit(hintPage.page, '1');
  await form.getByText('词典建议，匹配到「网约车」。').waitFor();
  assert.equal(await type.inputValue(), 'operating_expense');
  assert.equal(await category.inputValue(), '交通出行');
  assert.equal(await subcategory.inputValue(), '打车');
  await form.locator('[data-sheet-close]').click();
  await openStreamEdit(hintPage.page, '2');
  assert.equal(await type.inputValue(), 'operating_income');
  await type.selectOption('internal_transfer');
  for (const [value, options] of [
    ['investment_funding', ['投资入金', '基金定投申购', '积存金买入']],
    ['investment_withdrawal', ['投资出金', '基金赎回', '理财赎回']],
    ['internal_transfer', ['内部转账', '转账退回', '资金在途']],
    ['debt_repayment', ['信用卡还款']],
    ['fx_conversion', ['换汇转账']],
    ['debt_drawdown', ['信用卡取现']],
  ]) {
    await transferType.selectOption(value);
    assert.deepEqual(await subcategory.locator('option').allTextContents(), [...options, '手动确认']);
    assert.equal(await subcategory.inputValue(), options[0]);
    await subcategory.selectOption(options.at(-1));
  }
  await type.selectOption('operating_expense');
  const expense = form.locator('[data-ledger-expense-picker]');
  const details = expense.locator('[data-expense-details]');
  const counterAccount = form.locator('[data-edit-field="counter_account_code"]');
  const submit = form.locator('button[type="submit"]');
  assert.equal(await details.isVisible(), false);
  assert.equal(await submit.isDisabled(), true);
  assert.equal(await counterAccount.isVisible(), false);
  assert.equal(await counterAccount.isDisabled(), true);
  assert.equal(await form.locator('[data-edit-field="counterparty"]').isVisible(), true);
  for (const width of [320, 390, 1280]) {
    await hintPage.page.setViewportSize({ width, height: 900 });
    assert.equal(await expense.evaluate(el => el.scrollWidth <= el.clientWidth), true);
  }
  const transport = expense.getByRole('button', { name: '交通出行', exact: true });
  await transport.focus();
  await transport.press('Enter');
  await details.getByRole('button', { name: '交通卡', exact: true }).click();
  assert.equal(await transport.getAttribute('aria-pressed'), 'true');
  assert.equal(await subcategory.inputValue(), '交通卡');
  assert.equal(await submit.isEnabled(), true);
  await expense.getByRole('button', { name: '其他支出', exact: true }).click();
  assert.equal(await submit.isDisabled(), true);
  assert.equal(await details.getByRole('button', { name: '交通卡', exact: true }).count(), 0);
  assert.equal(await details.getByRole('button', { name: '利息支出', exact: true }).count(), 0);
  await expense.getByRole('button', { name: '金融费用', exact: true }).click();
  await details.getByRole('button', { name: '利息支出', exact: true }).click();
  assert.equal(await subcategory.inputValue(), '利息支出');
  await type.selectOption('internal_transfer');
  assert.equal(await expense.isVisible(), false);
  assert.equal(await transferType.isVisible(), true);
  assert.equal(await counterAccount.isEnabled(), false);
  assert.equal(await counterAccount.isVisible(), false);
  await type.selectOption('operating_income');
  await category.selectOption('退款返现');
  await subcategory.selectOption('退货退税');
  assert.equal(await hintPage.page.evaluate(() => __ledgerTest.writes.length), 0);
  await type.selectOption('internal_transfer');
  await transferType.selectOption('investment_withdrawal');
  await subcategory.selectOption('基金赎回');
  await submit.click();
  await hintPage.page.waitForFunction(() => __ledgerTest.writes.length === 1);
  assert.deepEqual(await hintPage.page.evaluate(() => __ledgerTest.writes[0]), {
    url:'/api/ledger/rows/2/update', body:{cashflow_type:'investment_withdrawal', category:'内部划转',
      subcategory:'基金赎回', counterparty:''},
  });
  await hintPage.page.evaluate(() => __ledgerTest.pendingPosts.shift().resolve({ok:true}));
  await form.waitFor({state:'hidden'});
  await openStreamEdit(hintPage.page, '80');
  assert.equal(await type.inputValue(), 'operating_expense');
  assert.equal(await category.inputValue(), '金融费用');
  assert.equal(await subcategory.inputValue(), '银行费用');
  await submit.click();
  await hintPage.page.waitForFunction(() => __ledgerTest.writes.length === 2);
  assert.deepEqual(await hintPage.page.evaluate(() => __ledgerTest.writes[1]), {
    url:'/api/ledger/rows/80/update', body:{cashflow_type:'fee', category:'金融费用',
      subcategory:'银行费用', counterparty:''},
  });
  await hintPage.page.evaluate(() => __ledgerTest.pendingPosts.shift().resolve({ok:true}));
  await form.waitFor({state:'hidden'});
  await hintPage.context.close();

  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ status: 'passed', fixedEquivalenceStates: baselineEntry ? snapshots.size : 0, mobileSummaryWidths, controls: 'tabs/period/flow/trend/search/sort/account drilldown/cancel', edits: 'one POST per submit; explicit retry after failure; exact payload', lifecycle: 'latest read wins; dispose; replacement; BFCache restores view only; late POST inert', pageErrors: errors.length }));
}
