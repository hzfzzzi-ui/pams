// Explicit synthetic fixtures. Every browser request is fulfilled locally or aborted.
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const publicRoot = path.resolve(process.argv[2] || 'frontend/public');
const baselineEntry = process.argv[3] ? path.resolve(process.argv[3]) : null;
const runtime = process.env.PAMS_FRONTEND_PLAYWRIGHT_MODULE;
const {chromium} = runtime ? await import(pathToFileURL(runtime).href) : await import('playwright');
const dimensionKeys = ['asset_type', 'industry', 'theme', 'market', 'region', 'currency', 'underlying_exposure'];
const rows = Array.from({length: 9}, (_, i) => ({
  name: `固定测试标的 ${i + 1}`, symbol: `TEST${i + 1}`, value_cny: 1000 - i * 50,
  weight: 0.1 - i * 0.005, family: i % 2 ? '科技成长' : '中国资产', score_weighted: 0.2,
  industry_bucket: `测试行业 ${i % 3}`, theme_names: ['固定测试主题'], source: i % 2 ? '固定基金' : '直接持仓',
  confidence: 'high', quality_status: 'FULL', source_breakdown: {DIRECT: 1},
  source_details: [{name: '固定测试来源', account: 'ibkr', source_symbol: `TEST${i + 1}`, value_cny: 1000 - i * 50, weight: 0.1}],
}));
const lookthrough = {
  total_market_value_cny: 10000, exposures: Object.fromEntries(dimensionKeys.map(key => [key, rows])),
  quality: {coverage_ratio: 0.8, covered_value_cny: 8000, uncovered_value_cny: 2000,
    full_coverage_value_cny: 7000, top10_only_value_cny: 1000, product_tag_only_value_cny: 0, missing_value_cny: 2000, stale_value_cny: 0,
    low_confidence_assets: [{asset_name: '测试残差', quality_status: 'MISSING', market_value_cny: 2000, uncovered_value_cny: 2000, coverage_ratio: 0, current_mapping: '待核准', reason: '合成数据', suggested_action: '测试提示'}]},
  master_data: {status: 'ok', current_assets: 9, total_assets: 12, coverage: {industry: 0.8, theme: 0.7}, fund_underlying_rows: 3, funds_with_underlyings: 1,
    field_contract: [{table: 'test_asset_master', meaning: '显式固定测试字段'}],
    low_confidence_assets: [{name: '固定低可信标的', symbol: 'TEST-LOW', asset_type: 'ETF / 基金', industry_bucket: '测试行业', theme_count: 1, confidence: 'medium'}],
    fund_underlying_top10: [{fund_name: '固定测试基金', lookthrough_type: 'FULL_HOLDINGS', coverage_ratio: 0.8, fund_market_value_cny: 1000,
      underlyings: [{name: '固定测试底层', symbol: 'TEST1', weight_in_fund: 0.25, value_cny: 250, industry_bucket: '测试行业'}]}]},
};
const attributionRows = rows.map((row, i) => ({...row, display_name: row.name,
  pnl_cny: i % 2 ? -80.25 : 123.45, market_value_cny: row.value_cny,
  daily_pnl_cny: 8888, daily_price_pnl_cny: i ? null : 42.1234, pnl_contribution: 0.1,
  currency: i ? 'CNY' : 'USD', account: 'ibkr',
  fx_display: i ? {show: false} : {show: true, status: 'estimated', value_cny: 13.25, label: '证券汇兑估算（人民币）', date_status: 'not_independently_verified', scope: 'instrument_only_not_account_net_fx'},
  source_details: [{name: '固定归因来源', account: 'ibkr', source_symbol: row.symbol, pnl_cny: 123.45, daily_pnl_cny: 7777}],
}));
const attribution = {as_of: '2026-10-30', source: 'holdings_page', total_pnl_cny: 512.34,
  dimensions: Object.fromEntries(['holding', 'industry', 'region', 'theme', 'underlying'].map(key => [key, attributionRows])),
  quality: {underlying_selection: {truncated: true, returned_count: 9, total_count: 12, required_omitted_count: 1}},
  daily_fx_attribution: {status: 'confirmed', status_label: '已确认', as_of: '2026-10-30', opening_as_of: '2026-10-29',
    read_only: true, affects_nav: false, scope: 'positions_and_signed_cash', ownership_basis: 'full_account',
    net_fx_pnl_cny: '-110.533266363120', accrual_other_change_cny: null, official_total_pnl_cny: 20,
    summary: {position_fx_pnl_cny: '425.797174286382', cash_fx_pnl_cny: '-536.330440649502', official_residual_cny: '-1.9e-9'}, currencies: []},
};
const summary = {children: rows.map((row, i) => ({symbol: row.symbol, name: row.name, daily_pnl: 55.25 + i}))};
const origin = 'http://analysis-fixture.local';
const browser = await chromium.launch({headless: true, channel: 'msedge'});
const errors = [], snapshots = new Map();
let parityStates = 0;

async function makePage(legacy = false) {
  const context = await browser.newContext({viewport: {width: 390, height: 1084}, locale: 'zh-CN', serviceWorkers: 'block'});
  const page = await context.newPage();
  page.on('pageerror', error => errors.push(String(error)));
  await page.addInitScript(({lookthrough, attribution, summary}) => {
    window.__test = {reads: [], events: [], pending: [], queued: [], defer: false, deferNav: false, lookthrough, attribution, summary};
    window.addEventListener('pams:load-trade-attribution', event => window.__test.events.push(event.type));
    window.__read = url => {
      const test = window.__test;
      test.reads.push(url);
      if (test.defer) return new Promise((resolve, reject) => test.pending.push({url, resolve, reject}));
      if (url === '/api/analysis/lookthrough?baseCurrency=CNY') return Promise.resolve(structuredClone(test.lookthrough));
      if (url === '/api/analysis/return-attribution?baseCurrency=CNY') return Promise.resolve(structuredClone(test.attribution));
      if (url === 'summary') return Promise.resolve(structuredClone(test.summary));
      throw new Error('Unexpected synthetic read: ' + url);
    };
    window.__afterNav = task => {
      if (window.__test.deferNav) {window.__test.queued.push(task); return Promise.resolve();}
      return Promise.resolve().then(task);
    };
  }, {lookthrough, attribution, summary});
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url());
    if (request.method() !== 'GET' || url.origin !== origin) return route.abort();
    const pathname = url.pathname;
    let body, type = 'application/javascript; charset=utf-8';
    if (pathname.endsWith('/api.js')) body = 'export const jget = (...args) => window.__read(...args);';
    else if (pathname.endsWith('/investment_requests.js')) body = "export const loadInvestmentSummaryFresh = () => window.__read('summary');";
    else if (pathname.endsWith('/investment_startup.js')) body = 'export const afterInvestmentNav = task => window.__afterNav(task);';
    else if (pathname.endsWith('/ui.js')) body = "import {createBottomSheet} from './components/bottom_sheet.js?v=20261031-ui-v1'; const sheet=createBottomSheet(document); export const openBottomSheet=(...args)=>sheet.open(...args);";
    else if (pathname.endsWith('/lookthrough.js')) body = legacy ? await readFile(baselineEntry, 'utf8') : "import {mountAnalysisPage} from './features/analysis/page.js?v=20261031-analysis-modules-v1'; window.__analysisPage=mountAnalysisPage();";
    else if (pathname === '/fixture.html') {
      type = 'text/html; charset=utf-8';
      body = (await readFile(path.join(publicRoot, 'investments.html'), 'utf8')).replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, '')
        .replace('</body>', '<script>document.body.dataset.investTab="analysis";document.querySelectorAll("[data-invest-panel]").forEach(p=>p.hidden=p.dataset.investPanel!=="analysis");</script><script type="module" src="/assets/js/lookthrough.js"></script></body>');
    } else {
      const target = path.resolve(publicRoot, '.' + pathname);
      if (!target.startsWith(publicRoot + path.sep)) return route.abort();
      try {body = await readFile(target);} catch {return route.abort();}
      type = pathname.endsWith('.css') ? 'text/css' : pathname.endsWith('.js') ? type : 'application/octet-stream';
    }
    await route.fulfill({status: 200, contentType: type, body});
  });
  await page.goto(origin + '/fixture.html');
  await page.locator('[data-return-asset-heat-row]').first().waitFor();
  return {context, page};
}

async function tab(page, name) {
  await page.evaluate(name => {
    document.body.dataset.investTab = name;
    document.querySelectorAll('[data-invest-panel]').forEach(panel => {panel.hidden = panel.dataset.investPanel !== name;});
  }, name);
  if (name === 'risk') await page.locator('#lookthroughList [data-lookthrough-row]').first().waitFor();
}
async function snapshot(page, key, selectors, legacy) {
  const value = await page.evaluate(selectors => selectors.map(selector => {
    const element = document.querySelector(selector);
    return element ? element.innerHTML : null;
  }), selectors);
  if (legacy) snapshots.set(key, value);
  else if (baselineEntry) {assert.deepEqual(value, snapshots.get(key), key + ': identical existing template output'); parityStates++;}
}

try {
  for (const legacy of baselineEntry ? [true, false] : [false]) {
    const {context, page} = await makePage(legacy);
    await snapshot(page, 'return-holding-initial', ['#returnAttributionSummary', '#returnAttributionTabs', '#returnAttributionList'], legacy);
    assert.equal(await page.locator('[data-return-attr-row]').count(), 5);
    await page.locator('[data-return-attr-more]').click();
    assert.equal(await page.locator('[data-return-attr-row]').count(), 9);
    await snapshot(page, 'return-expanded', ['#returnAttributionList'], legacy);
    await page.locator('[data-return-asset-heat-row="0"]').click();
    const detail = await page.locator('#appBottomSheetBody').innerHTML();
    assert.match(detail, /55\.25/); assert.match(detail, /110\.53/); assert.match(detail, /1\.9e-9/);
    await snapshot(page, 'heat-detail', ['#appBottomSheetBody'], legacy);
    await page.locator('.appBottomSheetClose').click();
    for (const dimension of ['industry', 'region', 'theme', 'underlying', 'holding']) {
      await page.locator(`[data-return-attr-dim="${dimension}"]`).click();
      await snapshot(page, 'return-' + dimension, ['#returnAttributionTabs', '#returnAttributionList'], legacy);
      assert.equal(await page.locator('[data-return-attr-row]').count(), 5);
      await page.locator('[data-return-attr-row="0"]').click();
      await snapshot(page, 'detail-' + dimension, ['#appBottomSheetBody'], legacy);
      await page.locator('.appBottomSheetClose').click();
    }
    await page.locator('[data-analysis-panel-switch="asset_master"]').click();
    await page.locator('#assetMasterList .assetMasterFund').waitFor();
    await snapshot(page, 'asset-master', ['#assetMasterSummary', '#assetMasterList'], legacy);
    await page.locator('[data-analysis-panel-switch="trade_attribution"]').click();
    assert.deepEqual(await page.evaluate(() => window.__test.events), ['pams:load-trade-attribution']);
    await tab(page, 'risk');
    for (const dimension of dimensionKeys) {
      await page.locator(`[data-lookthrough-dim="${dimension}"]`).click();
      await snapshot(page, 'exposure-' + dimension, ['#lookthroughTabs', '#lookthroughList', '#lookthroughQuality'], legacy);
      if (dimension === 'theme') {
        await page.locator('[data-theme-family="科技成长"]').click();
        await snapshot(page, 'theme-family', ['#lookthroughList'], legacy);
      }
      if (dimension === 'underlying_exposure') for (const sort of ['weight', 'industry', 'theme', 'source', 'value']) {
        await page.locator(`[data-underlying-sort="${sort}"]`).click();
        await snapshot(page, 'underlying-' + sort, ['#lookthroughList'], legacy);
      }
      await page.locator('#lookthroughList [data-lookthrough-row], #lookthroughList [data-underlying-row]').first().click();
      await snapshot(page, 'exposure-detail-' + dimension, ['#appBottomSheetBody'], legacy);
      await page.locator('.appBottomSheetClose').click();
    }
    await page.locator('[data-lookthrough-dim="currency"]').click();
    await page.locator('[data-lookthrough-segment-list="1"]').hover();
    assert.equal(await page.locator('[data-lookthrough-center-label]').innerText(), rows[1].name);
    await snapshot(page, 'donut-hover', ['#lookthroughList'], legacy);
    if (!legacy) {
      // Persisted restore remounts one controller and rereads data; ordinary choices survive.
      const before = await page.evaluate(() => {
        const old = window.__analysisPage.controller;
        old.state.activeReturnAttributionDimension = 'industry'; old.state.returnAttributionShowAll = true;
        window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted: true}));
        const removed = document.querySelectorAll('[data-lookthrough-chart]').length;
        window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}));
        return {changed: old !== window.__analysisPage.controller, removed};
      });
      assert.deepEqual(before, {changed: true, removed: 0});
      await page.locator('[data-lookthrough-chart]').waitFor();
      assert.deepEqual(await page.evaluate(() => {
        const state = window.__analysisPage.controller.state;
        return [state.activeDimension, state.activeReturnAttributionDimension, state.returnAttributionShowAll];
      }), ['currency', 'industry', true]);
      // A disposed generation's deferred requests cannot overwrite its replacement.
      await page.evaluate(async () => {
        window.__analysisPage.dispose();
        const {mountAnalysisPage} = await import('/assets/js/features/analysis/page.js?v=20261031-analysis-modules-v1');
        window.__test.defer = true;
        window.__analysisPage = mountAnalysisPage();
        window.__old = window.__analysisPage.controller;
      });
      await page.waitForFunction(() => window.__test.pending.length === 1);
      await page.evaluate(async () => {
        const {mountAnalysisPage} = await import('/assets/js/features/analysis/page.js?v=20261031-analysis-modules-v1');
        window.__test.defer = false;
        if (mountAnalysisPage() !== window.__analysisPage) throw new Error('Repeated mount replaced the current page');
        window.__analysisPage.dispose();
        window.__analysisPage = mountAnalysisPage();
        window.__test.pending.splice(0).forEach(request => request.resolve({total_market_value_cny: 999999, exposures: {}}));
      });
      await page.locator('[data-lookthrough-chart]').waitFor();
      assert.deepEqual(await page.evaluate(() => [window.__old.state.lookthroughData, window.__analysisPage.controller.state.lookthroughData.total_market_value_cny]), [null, 10000]);
      // Scoped controllers never consume another root's tabs or stale NAV callbacks.
      await page.evaluate(async () => {
        window.__analysisPage.dispose();
        const {createAnalysisController} = await import('/assets/js/features/analysis/controller.js?v=20261031-analysis-modules-v1');
        const content = '<button data-return-attr-dim="industry">industry</button><div id="return-attribution"></div><div id="returnAttributionTabs"></div><div id="returnAttributionList"></div>';
        const one = document.createElement('section'), two = document.createElement('section');
        one.innerHTML = content; two.innerHTML = content; document.body.append(one, two);
        document.body.dataset.investTab = 'analysis'; window.__test.deferNav = true;
        const first = createAnalysisController({root: one}), second = createAnalysisController({root: two});
        first.init(); second.init(); first.init();
        one.querySelector('button').click();
        window.__roots = {first, second, one, two};
      });
      assert.deepEqual(await page.evaluate(() => [window.__roots.first.state.activeReturnAttributionDimension, window.__roots.second.state.activeReturnAttributionDimension]), ['industry', 'holding']);
      const reads = await page.evaluate(() => window.__test.reads.length);
      await page.evaluate(async () => {
        window.__roots.first.dispose(); window.__roots.second.dispose();
        for (const task of window.__test.queued.splice(0)) await task();
        window.dispatchEvent(new HashChangeEvent('hashchange'));
      });
      assert.equal(await page.evaluate(() => window.__test.reads.length), reads);
    }
    await context.close();
  }
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({parityStates, dimensionCoverage: dimensionKeys.length, attributionDimensions: 5, lifecycle: ['remount', 'late-response', 'root-isolation', 'queued-NAV-disposal', 'listener-cleanup', 'SVG-disposal', 'BFCache-event-restore'], pageErrors: errors.length}));
} finally {await browser.close();}
