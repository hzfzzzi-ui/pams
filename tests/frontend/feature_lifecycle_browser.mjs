import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {pathToFileURL} from 'node:url';
import path from 'node:path';

const {chromium} = await import(pathToFileURL(process.env.PAMS_FRONTEND_PLAYWRIGHT_MODULE).href);
const publicRoot = path.resolve(process.argv[2]);
const browser = await chromium.launch({headless: true, channel: process.env.PAMS_FRONTEND_PLAYWRIGHT_CHANNEL || undefined});
const page = await browser.newPage({viewport: {width: 390, height: 844}});
const errors = [], network = [];
let entrySmoke = false;
page.on('pageerror', error => errors.push(error.message));
await page.route('**/*', async route => {
  const request = route.request(), url = new URL(request.url());
  if (url.origin !== 'http://feature-fixture.invalid' || request.method() !== 'GET') {
    network.push(`${request.method()} ${request.url()}`); await route.abort(); return;
  }
  if (url.pathname.startsWith('/api/')) {
    const fixtures = {
      '/api/portfolio/data-quality': {as_of: '2026-10-29', data_quality: quality},
      '/api/accounts/overview': {accounts: [{account_code: 'fixture', display_name: '固定 <账户>'}]},
      '/api/sync/center': syncPayload,
    };
    if (entrySmoke && fixtures[url.pathname]) return route.fulfill({json: fixtures[url.pathname]});
    network.push(`${request.method()} ${request.url()}`); await route.abort(); return;
  }
  if (url.pathname === '/') return route.fulfill({contentType: 'text/html', body: `<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><title>固定生命周期测试数据</title>
<style>[hidden]{display:none!important}.appBottomSheet{position:fixed;inset:0}.appBottomSheetScrim{position:absolute;inset:0;background:#0008}.appBottomSheetPanel{position:absolute;inset:25% 0 0;background:#222;color:white;padding:16px;overflow:auto}button{min-height:44px}body.sheet-open{overflow:hidden}section{border-bottom:1px solid #aaa}</style>
<h1>固定生命周期测试数据</h1><div id="roots"></div>
<script type="module">
import {mount as mountMore} from '/assets/js/features/more/controller.js';
import {mount as mountData} from '/assets/js/features/data_sources/controller.js';
import {mount as mountSync} from '/assets/js/sync_center.js';
window.reads = {}; window.writes = []; window.navRenders = [];
window.makeGet = name => (url, options) => new Promise((resolve, reject) => {
  (reads[name] ||= []).push({url, options, resolve, reject});
});
window.post = (url, body) => new Promise((resolve, reject) => writes.push({url, body, resolve, reject}));
window.makeRoot = (id, kind) => {
  const root = document.createElement('section'); root.id = id;
  root.innerHTML = kind === 'more'
    ? '<a id="moreIssues" hidden><strong>待处理</strong><span id="moreIssueText"></span></a><span id="moreOrigin"></span><details id="moreNavGrowthDetails"><summary>NAV</summary><div id="moreNavGrowthExplanationPanel"></div></details>'
    : '<div id="issuesView" hidden><div id="issuesContent"></div></div><div id="statusLine"></div><div id="todayActions"></div><div id="sourceList"></div><table><tbody id="sourceGrid"></tbody></table><div id="manualSyncStatus"></div><span id="asof"></span><div id="dataQuality"></div><div id="err"></div><button data-sync-scope="a_stock">A 股刷新</button>';
  root.clickListeners = new Set();
  const add = root.addEventListener.bind(root), remove = root.removeEventListener.bind(root);
  root.addEventListener = (type, listener, options) => {if(type === 'click') root.clickListeners.add(listener); add(type, listener, options);};
  root.removeEventListener = (type, listener, options) => {if(type === 'click') root.clickListeners.delete(listener); remove(type, listener, options);};
  document.getElementById('roots').append(root); return root;
};
window.mountMore = mountMore; window.mountData = mountData; window.mountSync = mountSync;
window.loaded = true;
</script></html>`});
  if (!url.pathname.startsWith('/assets/') && !['/more.html', '/data_sources.html'].includes(url.pathname)) { network.push(url.href); return route.abort(); }
  const file = path.resolve(publicRoot, `.${url.pathname}`);
  assert.ok(file.startsWith(publicRoot + path.sep));
  return route.fulfill({contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html', body: await readFile(file, 'utf8')});
});

const quality = {summary: {status: 'warning', warning_count: 2, error_count: 0}, checks: [
  {id: 'account_balance_coverage', status: 'attention', details: {missing_accounts: ['fixture']}},
  {id: 'valuation_series_freshness', status: 'attention', details: {valuation_series_latest_as_of: '2026-06-18', investment_positions_latest_as_of: '2026-10-29'}},
]};
const syncPayload = {updated_at: '2026-10-31T00:00:00Z', summary: {ok: 3, manual_required: 2, errors: 1, last_investment_date: '2026-10-29'}, workflows: [{label: '固定流程', description: '固定说明'}], rows: [
  {id: 'synthetic-source', source: '固定数据源', sync_type: '固定快照', status: 'review', status_label: '待核准', latest_date: '2026-10-29', rows: 123, action: '核对固定记录', details: '固定口径', href: 'accounts.html#overview', trigger_endpoint: '/api/fixture/manual', trigger_body: {only: 'synthetic'}},
]};

try {
  await page.goto('http://feature-fixture.invalid/');
  await page.waitForFunction(() => window.loaded);
  await page.evaluate(() => {
    window.moreRoot = makeRoot('more-a', 'more'); window.moreOther = makeRoot('more-b', 'more');
    window.moreContext = {get: makeGet('more'), renderExplanation: (value, el) => {navRenders.push(value); el.textContent = '固定 NAV 内容';}};
    window.more = mountMore(moreRoot, moreContext);
  });
  assert.equal(await page.evaluate(() => mountMore(moreRoot, moreContext) === more), true);
  await page.evaluate(payload => reads.more[0].resolve({data_quality: payload}), quality);
  await page.evaluate(() => more.ready);
  assert.equal(await page.locator('#more-a #moreIssueText').textContent(), '错误 0 · 警告 2');
  assert.equal(await page.locator('#more-b #moreIssueText').textContent(), '');
  await page.evaluate(() => {
    const details = moreRoot.querySelector('details'); details.open = true;
    details.dispatchEvent(new Event('toggle')); details.dispatchEvent(new Event('toggle'));
  });
  assert.equal(await page.evaluate(() => reads.more.length), 2);
  await page.evaluate(() => {
    more.dispose(); moreRoot.querySelector('#moreNavGrowthExplanationPanel').textContent = '销毁后保留';
    reads.more[1].resolve({fixture: true});
  });
  await page.waitForTimeout(20);
  assert.equal(await page.locator('#more-a #moreNavGrowthExplanationPanel').textContent(), '销毁后保留');
  assert.equal(await page.evaluate(() => navRenders.length), 0);
  await page.evaluate(() => moreRoot.querySelector('details').dispatchEvent(new Event('toggle')));
  assert.equal(await page.evaluate(() => reads.more.length), 2);

  await page.evaluate(() => {
    window.issueRoot = makeRoot('issues-a', 'data'); window.issueOther = makeRoot('issues-b', 'data');
    window.issues = mountData(issueRoot, {get: makeGet('issues'), location: {search: '?view=issues'}, mountSyncCenter() {throw new Error('Unexpected sync mount');}});
  });
  await page.evaluate(payload => {
    reads.issues[0].resolve({data_quality: payload});
    reads.issues[1].resolve({accounts: [{account_code: 'fixture', display_name: '固定 <账户>'}]});
  }, quality);
  await page.evaluate(() => issues.ready);
  assert.equal(await page.locator('#issues-a .issueRow').count(), 2);
  assert.match(await page.locator('#issues-a #issuesContent').textContent(), /固定 <账户>/);
  assert.match(await page.locator('#issues-a #issuesContent').textContent(), /2026-06-18.*2026-10-29/);
  assert.equal(await page.locator('#issues-a .issueRow a').first().getAttribute('href'), 'accounts.html#overview');
  assert.equal(await page.locator('#issues-b #issuesContent').textContent(), '');
  await page.evaluate(() => issues.dispose());
  assert.equal(await page.locator('#issues-a #issuesView').evaluate(el => el.hidden), true);
  assert.equal(await page.locator('#issues-a').evaluate(el => el.classList.contains('issues-only')), false);

  await page.evaluate(() => {
    window.sourceRoot = makeRoot('source-a', 'data'); window.sourceOther = makeRoot('source-b', 'data');
    window.source = mountData(sourceRoot, {get: makeGet('quality'), mountSyncCenter: (root, context) => {
      window.sync = mountSync(root, {...context, get: makeGet('sync'), post}); return sync;
    }});
  });
  await page.evaluate(payload => {
    reads.quality[0].resolve({as_of: '2026-10-29', data_quality: null}); reads.sync[0].resolve(payload);
  }, syncPayload);
  await page.evaluate(() => source.ready);
  assert.equal(await page.locator('#source-a #asof').textContent(), '2026-10-29');
  assert.match(await page.locator('#source-a #dataQuality').textContent(), /暂无统一数据质量信息/);
  assert.equal(await page.locator('#source-b #sourceList').textContent(), '');
  for (let i = 0; i < 4; i++) {
    await page.evaluate(() => {window.reloading = sync.reload();});
    await page.evaluate(payload => reads.sync.at(-1).resolve(payload), syncPayload);
    await page.evaluate(() => reloading);
  }
  assert.equal(await page.evaluate(() => sourceRoot.clickListeners.size), 1);
  await page.locator('#source-b [data-sync-scope]').click();
  assert.equal(await page.evaluate(() => writes.length), 0);
  await page.locator('#source-a [data-sync-scope]').click();
  assert.equal(await page.evaluate(() => writes.length), 1, 'Repeated reload must not multiply manual writes');
  assert.deepEqual(await page.evaluate(() => ({url: writes[0].url, body: writes[0].body})), {url: '/api/prices/refresh/investments', body: {scopes: ['a_stock']}});
  await page.evaluate(() => writes[0].resolve({success: true}));
  await page.waitForFunction(() => reads.sync.length === 6);
  await page.evaluate(payload => reads.sync[5].resolve(payload), syncPayload);
  await page.waitForFunction(() => document.querySelector('#source-a #sourceList [data-sync-row]'));

  await page.locator('#source-a #sourceList [data-sync-row]').click();
  assert.match(await page.locator('#appBottomSheetBody').textContent(), /固定数据源/);
  assert.match(await page.locator('#appBottomSheetBody').textContent(), /固定快照/);
  assert.match(await page.locator('#appBottomSheetBody').textContent(), /123/);
  assert.match(await page.locator('#appBottomSheetBody').textContent(), /固定口径/);
  assert.equal(await page.locator('#appBottomSheetBody a').getAttribute('href'), 'accounts.html#overview');
  await page.locator('[data-sync-trigger]').click();
  assert.equal(await page.evaluate(() => writes.length), 2);
  assert.deepEqual(await page.evaluate(() => ({url: writes[1].url, body: writes[1].body})), {url: '/api/fixture/manual', body: {only: 'synthetic'}});
  await page.evaluate(() => {source.dispose(); document.querySelector('#source-a #manualSyncStatus').textContent = '销毁后状态';});
  assert.equal(await page.locator('#appBottomSheet').count(), 0);
  assert.equal(await page.evaluate(() => sourceRoot.clickListeners.size), 0);
  await page.evaluate(() => writes[1].resolve({success: true}));
  await page.waitForTimeout(20);
  assert.equal(await page.evaluate(() => reads.sync.length), 6, 'Disposed writes must not start a follow-up reload');
  assert.equal(await page.locator('#source-a #manualSyncStatus').textContent(), '销毁后状态');
  await page.locator('#source-a [data-sync-scope]').click();
  assert.equal(await page.evaluate(() => writes.length), 2);

  // Removing and remounting the same real root starts with one delegated listener.
  await page.evaluate(() => {
    window.syncNew = mountSync(sourceRoot, {get: makeGet('syncNew'), post, targets: {rowsEl: sourceRoot.querySelector('#sourceList')}});
    window.pendingNew = syncNew.reload();
  });
  assert.equal(await page.evaluate(() => sourceRoot.clickListeners.size), 1);
  await page.evaluate(() => {syncNew.dispose(); sourceRoot.querySelector('#sourceList').textContent = '新的页面内容'; reads.syncNew[0].reject(new Error('晚返回错误'));});
  await page.evaluate(() => pendingNew);
  assert.equal(await page.locator('#source-a #sourceList').textContent(), '新的页面内容');
  assert.equal(await page.evaluate(() => sourceRoot.clickListeners.size), 0);

  // Exercise the unchanged direct HTML entry behavior too. saveData isolates
  // this controller regression from the separately tested shared warmup scope.
  entrySmoke = true;
  await page.addInitScript(() => Object.defineProperty(navigator, 'connection', {configurable: true, value: {saveData: true}}));
  await page.goto('http://feature-fixture.invalid/more.html?content=1');
  await page.getByText('错误 0 · 警告 2', {exact: true}).waitFor();
  assert.equal(await page.locator('#moreOrigin').textContent(), 'http://feature-fixture.invalid');
  await page.evaluate(() => {
    window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted: true}));
    document.querySelector('#moreIssueText').textContent = '等待恢复';
    window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}));
  });
  await page.getByText('错误 0 · 警告 2', {exact: true}).waitFor();
  await page.goto('http://feature-fixture.invalid/data_sources.html?content=1&view=issues');
  await page.locator('#issuesContent .issueRow').first().waitFor();
  assert.equal(await page.locator('#issuesContent .issueRow').count(), 2);
  assert.match(await page.locator('#issuesContent').textContent(), /固定 <账户>/);
  await page.goto('http://feature-fixture.invalid/data_sources.html?content=1');
  await page.locator('#sourceList [data-sync-row]').waitFor();
  assert.equal(await page.locator('#asof').textContent(), '2026-10-29');
  assert.match(await page.locator('#dataQuality').textContent(), /行动提示2/);
  assert.equal(await page.locator('#sourceList [data-sync-row]').count(), 1);
  await page.evaluate(() => {
    window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted: true}));
    document.querySelector('#sourceList').replaceChildren();
    window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}));
  });
  await page.locator('#sourceList [data-sync-row]').waitFor();
  assert.equal(await page.locator('#sourceList [data-sync-row]').count(), 1);
  assert.deepEqual(network, []); assert.deepEqual(errors, []);
  console.log('Real browser feature lifecycle: isolated roots, idempotent mounting, four reloads/one write, owned sheet cleanup, late GET/POST responses and all three direct entry modes passed.');
} finally {
  await browser.close();
}
