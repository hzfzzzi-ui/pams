// Real renderer, synthetic responses; no production requests or financial writes.
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
const {chromium} = await import(pathToFileURL(process.env.PAMS_FRONTEND_PLAYWRIGHT_MODULE).href);
const browser = await chromium.launch({headless: true});
const root = path.resolve(process.argv[2]);
try {
  const page = await browser.newPage({viewport: {width: 390, height: 844}});
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url());
    assert.equal(url.origin, 'http://freshness.invalid');
    assert.equal(request.method(), 'GET');
    const file = path.resolve(root, '.' + url.pathname);
    assert.ok(file.startsWith(root + path.sep));
    if (!/\.(html|js|css)$/.test(file)) return route.abort();
    let body = await readFile(file, 'utf8');
    if (file.endsWith('.html')) body = body.replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, '');
    await route.fulfill({contentType: file.endsWith('.js') ? 'application/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html', body});
  });
  await page.clock.install();
  await page.goto('http://freshness.invalid/investments.html?content=1');
  const result = await page.evaluate(async () => {
    const {mount} = await import('/assets/js/features/investment/holdings/list_controller.js');
    const {createPageRead} = await import('/assets/js/shared/page_read.js');
    let revision = 1, reads = 0;
    const row = i => ({account: 'sample', name: `合成基金${i}版本${revision}`, symbol: String(i).padStart(6, '0'),
      major_category: 'A股', minor_category: 'A股基金', currency: 'CNY', quantity: 10,
      cost_cny: 100, market_value_cny: revision * 100, current_price: revision * 10,
      daily_pnl_stale: true, daily_return_status: 'pending', daily_pnl_cny: null,
      valuation_updated_at: '2026-10-27T12:00:00Z'});
    const loadHoldingView = createPageRead(() => '/synthetic', {maxAge: 0, read: async () => {
      reads++;
      return {holdings: {as_of: '2026-10-28', rows: Array.from({length: 30}, (_, i) => row(i))}, account_positions: {by_account: []}};
    }});
    const instance = mount(document.body, {host: window, loadHoldingView,
      get: async () => {throw Error('Unexpected legacy request');}, bootstrap() {}, showDetails() {}});
    await instance.ready;
    document.querySelector('#mobileHoldingFilterChips [data-group="A股基金"]').click();
    window.scrollTo(0, 700);
    window.freshnessProbe = {instance, next: () => {revision = 2;}, reads: () => reads,
      beforeScroll: window.scrollY, originalGroup: instance.snapshot().group};
    return document.querySelector('#mobileHoldingList').textContent;
  });
  assert.match(result, /版本1/);
  await page.evaluate(() => window.freshnessProbe.next());
  await page.clock.runFor(60001);
  await page.waitForFunction(() => document.querySelector('#mobileHoldingList').textContent.includes('版本2'));
  const after = await page.evaluate(() => {
    const probe = window.freshnessProbe;
    const result = {reads: probe.reads(), group: probe.instance.snapshot().group,
      originalGroup: probe.originalGroup, scroll: window.scrollY, beforeScroll: probe.beforeScroll};
    probe.instance.dispose();
    return result;
  });
  assert.equal(after.reads, 2);
  assert.equal(after.group, after.originalGroup);
  assert.equal(after.scroll, after.beforeScroll);
  await page.clock.runFor(60001);
  assert.equal(await page.evaluate(() => window.freshnessProbe.reads()), 2);
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({...after, pageErrors: 0, productionRequests: 0, writes: 0}));
} finally {await browser.close();}
