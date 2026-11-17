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
      '/api/sync/accounts': {groups: []},
      '/api/holdings/by-account': {base_currency:'CNY', by_account:[]},
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
window.reads = {}; window.navRenders = [];
window.makeGet = name => (url, options) => new Promise((resolve, reject) => {
  (reads[name] ||= []).push({url, options, resolve, reject});
});
window.makeRoot = id => {
  const root = document.createElement('section'); root.id = id;
  root.innerHTML = '<span id="moreOrigin"></span><details id="moreNavGrowthDetails"><summary>NAV</summary><div id="moreNavGrowthExplanationPanel"></div></details>';
  document.getElementById('roots').append(root); return root;
};
window.mountMore = mountMore;
window.loaded = true;
</script></html>`});
  if (url.pathname === '/investments.html') {
    return route.fulfill({contentType: 'text/html', body: '<title>合成投资页</title><h1>投资占位页</h1>'});
  }
  if (!url.pathname.startsWith('/assets/') && !['/app.html', '/more.html'].includes(url.pathname)) { network.push(url.href); return route.abort(); }
  const file = path.resolve(publicRoot, `.${url.pathname}`);
  assert.ok(file.startsWith(publicRoot + path.sep));
  return route.fulfill({contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html', body: await readFile(file, 'utf8')});
});

try {
  await page.goto('http://feature-fixture.invalid/');
  await page.waitForFunction(() => window.loaded);
  await page.evaluate(() => {
    window.moreRoot = makeRoot('more-a');
    window.moreContext = {get: makeGet('more'), renderExplanation: (value, el) => {navRenders.push(value); el.textContent = '固定 NAV 内容';}};
    window.more = mountMore(moreRoot, moreContext);
  });
  assert.equal(await page.evaluate(() => mountMore(moreRoot, moreContext) === more), true);
  await page.evaluate(() => more.ready);
  await page.evaluate(() => {
    const details = moreRoot.querySelector('details'); details.open = true;
    details.dispatchEvent(new Event('toggle')); details.dispatchEvent(new Event('toggle'));
  });
  assert.equal(await page.evaluate(() => reads.more.length), 1);
  await page.evaluate(() => {
    more.dispose(); moreRoot.querySelector('#moreNavGrowthExplanationPanel').textContent = '销毁后保留';
    reads.more[0].resolve({fixture: true});
  });
  await page.waitForTimeout(20);
  assert.equal(await page.locator('#more-a #moreNavGrowthExplanationPanel').textContent(), '销毁后保留');
  assert.equal(await page.evaluate(() => navRenders.length), 0);
  await page.evaluate(() => moreRoot.querySelector('details').dispatchEvent(new Event('toggle')));
  assert.equal(await page.evaluate(() => reads.more.length), 1);

  // Exercise the unchanged direct HTML entry behavior too. saveData isolates
  // this controller regression from the separately tested shared warmup scope.
  entrySmoke = true;
  await page.addInitScript(() => Object.defineProperty(navigator, 'connection', {configurable: true, value: {saveData: true}}));
  await page.goto('http://feature-fixture.invalid/more.html?content=1');
  await page.waitForFunction(() => document.querySelector('#accountSyncStatus').textContent === '');
  await page.evaluate(() => {
    window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted: true}));
    document.querySelector('#moreOrigin').textContent = '等待恢复';
    window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}));
  });
  assert.equal(await page.locator('#moreOrigin').textContent(), 'http://feature-fixture.invalid');
  await page.goto('http://feature-fixture.invalid/app.html?view=data_sources.html');
  await page.frameLocator('#page-content').getByRole('heading', {name:'投资占位页'}).waitFor();
  await page.locator('[data-page="investments.html"][aria-current="page"]').waitFor();
  assert.deepEqual(errors, []);
  assert.deepEqual(network, []);
} finally {
  await browser.close();
}
