import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {pathToFileURL} from 'node:url';
import path from 'node:path';
import {riskPayload, configPayload} from './portfolio_panel_fixtures.mjs';

const {chromium} = await import(pathToFileURL(process.env.PAMS_FRONTEND_PLAYWRIGHT_MODULE).href);
const publicRoot = path.resolve(process.argv[2]);
const browser = await chromium.launch({headless: true, channel: process.env.PAMS_FRONTEND_PLAYWRIGHT_CHANNEL || undefined});
const page = await browser.newPage({viewport: {width: 390, height: 844}});
const errors = [];
page.on('pageerror', error => errors.push(error.message));
await page.route('**/*', async route => {
  const url = new URL(route.request().url());
  assert.equal(url.origin, 'http://portfolio-fixture.invalid'); assert.equal(route.request().method(), 'GET');
  if (url.pathname === '/') return route.fulfill({contentType: 'text/html', body: `<!doctype html><meta charset="utf-8"><title>固定组合面板测试</title>
    <h1>固定测试数据</h1><button id="outside" data-risk-category="liquidity">外部同名入口</button>
    <script type="module">
      import {mount as mountRisk} from '/assets/js/features/portfolio/risk_controller.js';
      import {mount as mountConfig} from '/assets/js/features/portfolio/config_controller.js';
      window.mounts = {risk: mountRisk, config: mountConfig};
    </script>`});
  if (url.pathname === '/assets/js/api.js') return route.fulfill({contentType: 'text/javascript', body: `export async function jget(url) {
    window.compatCalls.push(url); return structuredClone(window.payloads[url.includes('risk-disclosure') ? 'risk' : 'config']);
  }`});
  if (url.pathname === '/assets/js/investment_startup.js') return route.fulfill({contentType: 'text/javascript', body: 'export const afterInvestmentStartup = task => Promise.resolve().then(task);'});
  if (url.pathname === '/assets/js/ui.js') return route.fulfill({contentType: 'text/javascript', body: 'export const openBottomSheet = (...args) => window.compatDetails.push(args); export function closeBottomSheet() {}'});
  assert.match(url.pathname, /^\/assets\/js\/(features\/portfolio\/|components\/data_state\.js|portfolio_(risk|config)\.js)/);
  const file = path.resolve(publicRoot, '.' + url.pathname);
  assert.ok(file.startsWith(publicRoot + path.sep));
  return route.fulfill({contentType: 'text/javascript', body: await readFile(file, 'utf8')});
});
try {
  await page.goto('http://portfolio-fixture.invalid/');
  await page.waitForFunction(() => window.mounts);
  await page.evaluate(({riskPayload, configPayload}) => {
    window.payloads = {risk: riskPayload, config: configPayload};
    window.scenarios = {};
    window.createHost = (kind, id) => {
      const root = document.createElement('section'); root.id = id;
      root.innerHTML = kind === 'risk'
        ? '<h2 data-risk-checklist-title>旧标题</h2><span id="portfolioRiskAsOf"></span><div id="portfolioRiskDashboard"></div><div id="portfolioRiskCategories"></div>'
        : '<span id="portfolioConfigAsOf"></span><div id="portfolioConfigRules"></div><div id="portfolioRebalanceQueue"></div>';
      document.body.append(root); return root;
    };
    window.setup = (kind, id) => {
      const root = createHost(kind, id), calls = [], details = [];
      let overlay;
      const context = {get: async (url, options) => { calls.push({url, cache: options.cache, signal: options.signal}); return structuredClone(payloads[kind]); },
        showDetails: (...args) => {
          details.push(args); overlay = document.createElement('div'); overlay.innerHTML = args[1];
          document.body.append(overlay); return overlay;
        }, closeDetails: () => overlay?.remove()};
      const controller = mounts[kind](root, context);
      return scenarios[id] = {root, context, controller, calls, details, sheet: () => overlay};
    };
  }, {riskPayload, configPayload});

  for (const kind of ['risk', 'config']) {
    await page.evaluate(async kind => {
      const one = setup(kind, `${kind}-one`), two = setup(kind, `${kind}-two`);
      await Promise.all([one.controller.ready, two.controller.ready]);
      if (mounts[kind](one.root, one.context) !== one.controller) throw new Error('Repeated mount created a second owner');
    }, kind);
    assert.equal(await page.evaluate(id => scenarios[id].calls.length, `${kind}-one`), 1);
    assert.equal(await page.evaluate(id => scenarios[id].calls[0].cache, `${kind}-one`), false);
    assert.equal(await page.evaluate(id => scenarios[id].calls[0].url, `${kind}-one`),
      kind === 'risk' ? '/api/portfolio/risk-disclosure?baseCurrency=CNY' : '/api/portfolio/config?baseCurrency=CNY');
  }
  await page.locator('#risk-one [data-risk-category="liquidity"]').click();
  assert.equal(await page.locator('#risk-one [data-risk-category="liquidity"]').getAttribute('aria-expanded'), 'true');
  assert.equal(await page.locator('#risk-two [data-risk-category="liquidity"]').getAttribute('aria-expanded'), 'false');
  assert.equal(await page.locator('#risk-one .portfolioRiskInlineItem').count(), 1);
  assert.match(await page.locator('#risk-one').innerText(), /固定风险项/);
  await page.locator('#outside').click();
  assert.equal(await page.locator('#risk-one [data-risk-category="liquidity"]').getAttribute('aria-expanded'), 'true');
  await page.locator('#risk-one [data-risk-category="concentration"]').click();
  assert.equal(await page.locator('#risk-one [data-risk-category="liquidity"]').getAttribute('aria-expanded'), 'false');

  await page.locator('#config-one [data-portfolio-rule="equity"]').click();
  await page.locator('#config-one [data-portfolio-subrule="domestic"]').click();
  assert.equal(await page.locator('#config-one .portfolioLeafRow').count(), 1);
  assert.equal(await page.locator('#config-two .portfolioLeafRow').count(), 0);
  await page.locator('#config-one [data-rebalance-item]').click();
  assert.deepEqual(await page.evaluate(() => scenarios['config-one'].details), [
    ['固定再平衡详情', '<div class="portfolioConfigSheet"><p>说明 &lt;保留&gt; &amp; 原文</p></div>'],
  ]);

  for (const kind of ['risk', 'config']) {
    await page.evaluate(async kind => {
      const s = scenarios[`${kind}-one`], snapshot = s.controller.snapshot();
      await s.controller.reload();
      if (JSON.stringify(s.controller.snapshot()) !== JSON.stringify(snapshot)) throw new Error('Reload lost expansion state');
      s.controller.dispose(); const markup = s.root.innerHTML;
      if (s.sheet()?.isConnected) throw new Error('Disposed panel retained its own detail sheet');
      s.root.querySelector(kind === 'risk' ? '[data-risk-category]' : '[data-portfolio-rule]').click();
      if (s.root.innerHTML !== markup) throw new Error('Disposed click listener remains active');
      s.controller = mounts[kind](s.root, {...s.context, state: snapshot}); await s.controller.ready;
      if (JSON.stringify(s.controller.snapshot()) !== JSON.stringify(snapshot)) throw new Error('Restored mount lost expansion state');
      for (let index = 0; index < 8; index++) {
        const state = s.controller.snapshot(); s.controller.dispose();
        s.controller = mounts[kind](s.root, {...s.context, state}); await s.controller.ready;
      }
      s.root.querySelector(kind === 'risk' ? '[data-risk-category="concentration"]' : '[data-portfolio-rule="equity"]').click();
    }, kind);
  }
  assert.equal(await page.locator('#risk-one [data-risk-category="concentration"]').getAttribute('aria-expanded'), 'false');
  assert.equal(await page.locator('#config-one [data-portfolio-rule="equity"]').getAttribute('aria-expanded'), 'false');
  await page.locator('#config-two [data-rebalance-item]').click();
  assert.equal(await page.evaluate(() => {
    const s = scenarios['config-two'];
    s.sheet().innerHTML = '<p>另一模块的详情</p>';
    s.controller.dispose();
    const retained = s.sheet().isConnected;
    s.sheet().remove();
    return retained;
  }), true, 'Disposal must not close another feature’s replacement sheet');

  for (const kind of ['risk', 'config']) {
    const result = await page.evaluate(async kind => {
      let unlock, reads = 0;
      const gate = new Promise(resolve => { unlock = resolve; });
      const root = createHost(kind, `${kind}-startup`);
      const controller = mounts[kind](root, {get: async () => { reads++; return payloads[kind]; },
        afterStartup: task => gate.then(task), showDetails() { throw new Error('Unexpected detail'); }});
      const markup = root.innerHTML; controller.dispose(); unlock(); await controller.ready;
      return {reads, unchanged: root.innerHTML === markup};
    }, kind);
    assert.deepEqual(result, {reads: 0, unchanged: true});
    const late = await page.evaluate(async kind => {
      let resolve, signal;
      const root = createHost(kind, `${kind}-late`);
      const old = mounts[kind](root, {get: (_url, options) => { signal = options.signal; return new Promise(done => { resolve = done; }); }, showDetails() {}});
      old.dispose();
      const current = mounts[kind](root, {get: async () => structuredClone(payloads[kind]), showDetails() {}});
      await current.ready; const markup = root.innerHTML;
      const payload = structuredClone(payloads[kind]); payload.as_of = '2099-12-31'; resolve(payload); await old.ready;
      return {aborted: signal.aborted, unchanged: root.innerHTML === markup};
    }, kind);
    assert.deepEqual(late, {aborted: true, unchanged: true});
  }
  for (const kind of ['risk', 'config']) {
    await page.evaluate(async kind => {
      const root = createHost(kind, `${kind}-error`);
      const controller = mounts[kind](root, {get: async () => { throw new Error('固定错误'); }, showDetails() {}});
      await controller.ready;
      const empty = createHost(kind, `${kind}-empty`);
      await mounts[kind](empty, {get: async () => ({}), showDetails() {}}).ready;
    }, kind);
    assert.equal(await page.locator(`#${kind}-error [role="alert"]`).count(), kind === 'risk' ? 2 : 1);
    assert.match(await page.locator(`#${kind}-empty`).innerText(), kind === 'risk' ? /暂无风险检查项/ : /暂无再平衡动作/);
  }
  await page.evaluate(() => window.dispatchEvent(new Event('pagehide')));
  assert.equal(await page.evaluate(() => Object.values(scenarios).every(s => s.controller.disposed)), true);
  // Exercise the actual compatibility entries: restored documents do not
  // re-evaluate their module scripts, so persisted pageshow must remount once.
  await page.evaluate(async () => {
    window.compatCalls = []; window.compatDetails = [];
    createHost('risk', 'portfolio-risk-disclosure'); createHost('config', 'portfolio-constraints');
    await Promise.all([import('/assets/js/portfolio_risk.js'), import('/assets/js/portfolio_config.js')]);
  });
  await page.locator('#portfolio-risk-disclosure [data-risk-category="liquidity"]').click();
  await page.locator('#portfolio-constraints [data-portfolio-rule="equity"]').click();
  await page.locator('#portfolio-constraints [data-portfolio-subrule="domestic"]').click();
  for (let index = 0; index < 2; index++) {
    await page.evaluate(() => {
      window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted: true}));
      window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}));
      window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}));
    });
    await page.waitForFunction(expected => compatCalls.length === expected, 4 + index * 2);
    assert.equal(await page.locator('#portfolio-risk-disclosure [data-risk-category="liquidity"]').getAttribute('aria-expanded'), 'true');
    assert.equal(await page.locator('#portfolio-constraints .portfolioLeafRow').count(), 1);
  }
  await page.locator('#portfolio-constraints [data-rebalance-item]').click();
  assert.equal(await page.evaluate(() => compatDetails.length), 1);
  assert.deepEqual(errors, []);
  console.log('Portfolio panels: root isolation, repeated mount/dispose, late/startup guards, expansion, details and restored-document entry passed.');
} finally { await browser.close(); }
