import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {pathToFileURL} from 'node:url';
import path from 'node:path';
import * as fixtures from './fixtures/accounts_lifecycle.mjs';

const {chromium} = await import(pathToFileURL(process.env.PAMS_FRONTEND_PLAYWRIGHT_MODULE).href);
const publicRoot = path.resolve(process.argv[2]);
const html = await readFile(path.join(publicRoot, 'accounts.html'), 'utf8');
const markup = html.match(/<body[^>]*>([\s\S]*?)<\/body>/)[1].replace(/<script\b[^>]*>[\s\S]*?<\/script>/g, '');
const fixtureData = JSON.stringify({config: fixtures.config, overview: fixtures.overview, matches: fixtures.matches, unmatched: fixtures.unmatched, imports: fixtures.imports});
const browser = await chromium.launch({headless: true, channel: process.env.PAMS_FRONTEND_PLAYWRIGHT_CHANNEL || undefined});
const page = await browser.newPage({viewport: {width: 390, height: 844}});
const errors = [], unexpected = [], entryReads = [];
let entryMode = false;
page.on('pageerror', error => errors.push(error.message));
await page.route('**/*', async route => {
  const request = route.request(), url = new URL(request.url());
  if (url.origin !== 'http://accounts-fixture.invalid' || request.method() !== 'GET') {unexpected.push(`${request.method()} ${url.href}`); return route.abort();}
  if (url.pathname.startsWith('/api/')) {
    if (!entryMode || !['/api/bank-reconcile/config', '/api/accounts/overview', '/api/bank-reconcile/transfer-matches', '/api/bank-reconcile/unmatched-rows', '/api/bank-reconcile/imports'].includes(url.pathname)) {unexpected.push(url.href); return route.abort();}
    entryReads.push(url.pathname + url.search); return route.fulfill({json: fixtures.responseFor(url.pathname)});
  }
  if (url.pathname === '/') return route.fulfill({contentType: 'text/html', body: `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>固定账户生命周期测试</title>
<style>[hidden]{display:none!important}button,input{min-height:44px}.appBottomSheet{position:fixed;inset:0}.appBottomSheetScrim{position:absolute;inset:0;background:#0008}.appBottomSheetPanel{position:absolute;inset:15% 0 0;background:#222;color:white;padding:16px;overflow:auto}body.sheet-open{overflow:hidden}.accountCollapsed{display:none}</style>
<h1>固定账户生命周期测试</h1><div id="roots"></div>
<script type="module">
import {mount} from '/assets/js/features/accounts/controller.js';
import {openBottomSheet, closeBottomSheet} from '/assets/js/ui.js';
window.fixtures = ${fixtureData}; window.markup = ${JSON.stringify(markup)};
window.reads = []; window.writes = []; window.pendingReads = []; window.holdReads = false;
window.responseFor = url => url.includes('/config') ? fixtures.config : url.includes('/overview') ? fixtures.overview : url.includes('/unmatched-rows') ? fixtures.unmatched : url.includes('/imports') ? fixtures.imports : fixtures.matches;
window.context = {
  get: url => {reads.push(url); return holdReads ? new Promise((resolve, reject) => pendingReads.push({resolve,reject})) : Promise.resolve(responseFor(url));},
  post: (url, body) => new Promise((resolve, reject) => writes.push({url, body, resolve, reject})),
  location: {hash: '#overview', pathname: '/accounts.html', search: '?content=1'},
  replaceAddress: url => {context.location.hash = new URL(url, 'http://accounts-fixture.invalid').hash;},
  openSheet: openBottomSheet, closeSheet: closeBottomSheet,
};
window.makeRoot = id => {
  const root = document.createElement('section'); root.id = id; root.innerHTML = markup;
  const add = root.addEventListener.bind(root), remove = root.removeEventListener.bind(root);
  root.clickListeners = new Set();
  root.addEventListener = (name, listener, options) => {if(name === 'click') root.clickListeners.add(listener); add(name, listener, options);};
  root.removeEventListener = (name, listener, options) => {if(name === 'click') root.clickListeners.delete(listener); remove(name, listener, options);};
  document.getElementById('roots').append(root); return root;
};
window.mountAccounts = mount; window.rootA = makeRoot('a'); window.rootB = makeRoot('b');
window.accounts = mount(rootA, context); window.loaded = true;
</script></html>`});
  const file = path.resolve(publicRoot, `.${url.pathname}`);
  if (!file.startsWith(publicRoot + path.sep)) {unexpected.push(url.href); return route.abort();}
  return route.fulfill({contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html', body: await readFile(file)});
});
try {
  await page.goto('http://accounts-fixture.invalid/'); await page.waitForFunction(() => window.loaded); await page.evaluate(() => accounts.ready);
  assert.deepEqual(await page.evaluate(() => reads), fixtures.initialUrls);
  assert.equal(await page.evaluate(() => mountAccounts(rootA, context) === accounts), true);
  assert.equal(await page.evaluate(() => rootA.clickListeners.size), 1);
  assert.equal(await page.locator('#b #accountOverviewRows').textContent(), '');
  await page.locator('#b #btnMatchTransfers').evaluate(button => button.click());
  assert.equal(await page.evaluate(() => writes.length), 0);

  await page.locator('#a #mobileAccountList [data-mobile-account-index]').first().click();
  assert.match(await page.locator('#appBottomSheetBody').textContent(), /核对合成金额/);
  assert.match(await page.locator('#appBottomSheetBody').textContent(), /1,000.00/);
  assert.match(await page.locator('#appBottomSheetBody').textContent(), /980.00/);
  await page.locator('#appBottomSheetBody [data-account-review]').evaluate(button => {button.click(); button.click();});
  await page.waitForFunction(() => writes.length === 1);
  assert.deepEqual(await page.evaluate(() => ({url: writes[0].url, body: writes[0].body})), {url: '/api/accounts/fixture%2Fcny/review', body: {action: 'approve', note: '账户核准中心确认当前余额'}});
  await page.evaluate(() => writes[0].resolve(fixtures.overview)); await page.keyboard.press('Escape');
  await page.locator('#a #mobileAccountList [data-mobile-account-index]').nth(1).click();
  await page.locator('#appBottomSheetBody [data-account-review]').click(); await page.waitForFunction(() => writes.length === 2);
  assert.deepEqual(await page.evaluate(() => ({url: writes[1].url, body: writes[1].body})), {url: '/api/accounts/fixture%2Fhkd/review', body: {action: 'defer_missing', note: '账户核准中心暂缓当前缺数据项'}});
  await page.evaluate(() => writes[1].resolve(fixtures.overview)); await page.keyboard.press('Escape');

  await page.locator('#a [data-account-tab="sources"]').click();
  await page.locator('#a [data-profile="synthetic_hkd"]').click();
  assert.equal(await page.locator('#a #profileCode').inputValue(), 'synthetic_hkd');
  assert.equal(await page.locator('#a #since').inputValue(), '2026-02-02');
  assert.equal(await page.locator('#a #btnImport').getAttribute('href'), 'account_import.html?kind=cash&account=fixture%2Fhkd');
  await page.locator('#a #since').fill('2026-09-01');
  await page.locator('#a [data-account-tab="transfers"]').click();
  await page.locator('#a #btnMatchTransfers').evaluate(button => {button.click(); button.click();}); await page.waitForFunction(() => writes.length === 3);
  assert.deepEqual(await page.evaluate(() => ({url: writes[2].url, body: writes[2].body})), {url: '/api/bank-reconcile/transfer-matches/run', body: {}});
  await page.evaluate(() => writes[2].resolve(fixtures.matches));
  await page.waitForFunction(() => reads.length === 8);
  assert.deepEqual(await page.evaluate(() => reads.slice(-3)), ['/api/bank-reconcile/unmatched-rows?since=2026-09-01&limit=500', '/api/bank-reconcile/imports', '/api/accounts/overview']);
  await page.waitForFunction(() => document.querySelector('#a #opMsg').textContent.startsWith('匹配完成：1'));

  await page.locator('#a #btnMatchTransfers').click(); await page.waitForFunction(() => writes.length === 4);
  await page.evaluate(() => {accounts.dispose(); rootA.querySelector('#opMsg').textContent = '销毁后保留'; writes[3].resolve(fixtures.matches);});
  await page.waitForTimeout(20);
  assert.equal(await page.evaluate(() => reads.length), 8); assert.equal(await page.locator('#a #opMsg').textContent(), '销毁后保留');
  assert.equal(await page.evaluate(() => rootA.clickListeners.size), 0);
  await page.locator('#a #btnMatchTransfers').click(); assert.equal(await page.evaluate(() => writes.length), 4);
  await page.evaluate(() => {holdReads = true; window.late = mountAccounts(rootA, context); late.dispose(); rootA.querySelector('#err').textContent = '新的页面'; pendingReads[0].reject(new Error('旧请求失败'));});
  await page.evaluate(() => late.ready);
  assert.equal(await page.locator('#a #err').textContent(), '新的页面'); assert.equal(await page.evaluate(() => reads.length), 9);

  // Direct entry and persisted lifecycle replay use the real entry module. Only
  // GET fixtures are available, so a replayed review/match would fail this test.
  entryMode = true;
  await page.addInitScript(() => Object.defineProperty(navigator, 'connection', {configurable: true, value: {saveData: true}}));
  await page.goto('http://accounts-fixture.invalid/accounts.html?content=1');
  await page.evaluate(async () => {window.entry = await import('/assets/js/bank_reconcile.js?v=20261031-modules-v1'); await entry.mount().ready;});
  assert.deepEqual(entryReads, fixtures.initialUrls);
  await page.locator('[data-account-filter="缺数据"]').click();
  await page.locator('[data-account-tab="sources"]').click(); await page.locator('[data-profile="synthetic_hkd"]').click();
  assert.equal(await page.locator('#profileCode').inputValue(), 'synthetic_hkd');
  await page.locator('#since').fill('2026-08-08');
  await page.evaluate(() => {
    document.querySelector('.truthNote').open = true;
    document.querySelector('#accountOverviewRows [data-group-key="bank"]').click();
    document.querySelector('[data-account-detail="fixture/cny"]').open = true;
  });
  await page.locator('[data-account-tab="transfers"]').click();
  await page.evaluate(() => {
    window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted: true}));
    window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}));
  });
  await page.evaluate(() => entry.mount().ready);
  // The existing API layer retains its account overview TTL across BFCache.
  // Logical read ordering is asserted above/in VM; this verifies no cache bypass.
  assert.deepEqual(entryReads.slice(5), ['/api/bank-reconcile/config', '/api/bank-reconcile/transfer-matches', '/api/bank-reconcile/unmatched-rows?since=2026-08-08&limit=500', '/api/bank-reconcile/imports']);
  assert.equal(await page.locator('#profileCode').inputValue(), 'synthetic_hkd');
  assert.equal(await page.locator('#since').inputValue(), '2026-08-08');
  assert.equal(await page.locator('[data-account-tab="transfers"]').evaluate(el => el.classList.contains('active')), true);
  assert.equal(await page.locator('.truthNote').evaluate(el => el.open), true);
  assert.equal(await page.locator('[data-account-detail="fixture/cny"]').evaluate(el => el.open), true);
  assert.equal(await page.locator('#accountOverviewRows [data-parent-group="bank"]').first().evaluate(el => el.classList.contains('accountCollapsed')), true);
  assert.equal(await page.locator('[data-account-filter="缺数据"]').evaluate(el => el.classList.contains('active')), true);
  assert.equal(await page.locator('#mobileAccountList [data-mobile-account-index]').count(), 1);
  assert.deepEqual(unexpected, []); assert.deepEqual(errors, []);
  console.log('Real browser accounts: root isolation, one approval/defer/match per click, original payloads/read order, profile/date input, late-response disposal and persisted pageshow UI restoration passed.');
} finally {await browser.close();}
