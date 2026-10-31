import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import * as accounts from './fixtures/accounts_lifecycle.mjs';
import * as transactions from './fixtures/transactions_lifecycle.mjs';

const {chromium} = await import(pathToFileURL(process.env.PAMS_FRONTEND_PLAYWRIGHT_MODULE).href);
const publicRoot = path.resolve(process.argv[2]);
const markup = {};
for (const name of ['more', 'data_sources', 'accounts', 'ledger', 'investments']) {
  markup[name] = (await readFile(path.join(publicRoot, `${name}.html`), 'utf8')).match(/<body\b[^>]*>([\s\S]*?)<\/body>/i)[1];
}
const quality = {data_quality: {summary: {status: 'warning', error_count: 0, warning_count: 2}}};
const records = [];
const waitFor = async (predicate, label) => {
  const end = Date.now() + 8000;
  while (!predicate()) {
    if (Date.now() > end) throw new Error(`Timed out: ${label}; requests: ${records.map(r => `${r.method} ${r.url} closed=${r.cancelled}`).join(', ')}`);
    await new Promise(resolve => setTimeout(resolve, 10));
  }
};
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://fixture.invalid');
  res.setHeader('Access-Control-Allow-Origin', '*');
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/summary2/')) {
    const record = {method: req.method, url: req.url, cancelled: false, body: '', ended: false};
    records.push(record);
    req.on('data', chunk => {record.body += chunk;});
    req.on('end', () => {record.received = true;});
    res.on('close', () => {if (!res.writableEnded) record.cancelled = true;});
    record.respond = (data, status = 200) => {
      record.ended = true; res.statusCode = status;
      res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(data));
    };
    return;
  }
  if (url.pathname === '/') {res.setHeader('Content-Type', 'text/html'); res.end('<!doctype html><meta charset="utf-8"><title>固定取消读取测试</title><body></body>'); return;}
  const file = path.resolve(publicRoot, `.${url.pathname}`);
  if (!file.startsWith(publicRoot + path.sep)) {res.statusCode = 404; res.end(); return;}
  try {res.setHeader('Content-Type', file.endsWith('.css') ? 'text/css' : 'text/javascript'); res.end(await readFile(file));}
  catch {res.statusCode = 404; res.end();}
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const base = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch({headless: true, channel: process.env.PAMS_FRONTEND_PLAYWRIGHT_CHANNEL || undefined});
const unexpected = [], errors = [], totals = [];
try {
  for (const transport of ['fetch', 'xhr']) {
    const start = records.length;
    const page = await browser.newPage();
    page.on('pageerror', error => errors.push(`${transport}: ${error.message}`));
    // Every business request is redirected before it reaches a real service.
    // The loopback server records native connection closure, not only a DOM guard.
    await page.route('**/*', route => {
      const request = route.request(), url = new URL(request.url());
      if (url.hostname !== '127.0.0.1' || ![String(server.address().port), '8090'].includes(url.port)) {
        unexpected.push(url.href); return route.abort();
      }
      if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/summary2/')) {
        return route.continue({url: base + url.pathname + url.search});
      }
      if (url.origin !== base || request.method() !== 'GET') {unexpected.push(url.href); return route.abort();}
      return route.continue();
    });
    if (transport === 'xhr') await page.addInitScript(() => {window.fetch = undefined;});
    await page.goto(base);
    await page.evaluate(async ({markup}) => {
      const load = name => import(`/assets/js/features/${name}/controller.js`);
      const [api, readScope, more, dataSources, syncCenter, accounts, ledger, analysis, risk, config, transaction, ui] = await Promise.all([
        import('/assets/js/api.js?v=20261031-modules-v1'), import('/assets/js/shared/read_scope.js?v=20261031-modules-v1'),
        load('more'), load('data_sources'), load('sync_center'), load('accounts'), load('ledger'), load('analysis'),
        import('/assets/js/features/portfolio/risk_controller.js'), import('/assets/js/features/portfolio/config_controller.js'),
        load('transactions'), import('/assets/js/ui.js?v=20261031-modules-v1'),
      ]);
      window.mods = {api, readScope, more, dataSources, syncCenter, accounts, ledger, analysis, risk, config, transaction, ui};
      Object.assign(window, {root: null, rootA: null, rootB: null, a: null, b: null, feature: null, child: null, first: null, second: null, third: null, reloading: null, triggered: null});
      window.makeRoot = name => {
        const root = document.createElement('section'); root.innerHTML = markup[name];
        root.querySelectorAll('script').forEach(script => script.remove());
        document.body.append(root); return root;
      };
      window.reset = () => {ui.closeBottomSheet(); document.body.replaceChildren(); api.clearApiCache(); document.body.dataset.investTab = '';};
      window.ctx = {get: api.jget, post: api.jpost, openSheet: ui.openBottomSheet, closeSheet: ui.closeBottomSheet, bootstrap() {}, replaceAddress() {}};
      window.outcome = promise => promise.then(() => 'resolved', error => error.name);
    }, {markup});
    const latest = (url, from = start, method = 'GET') => records.filter(r => r.method === method && r.url.startsWith(url)).filter(r => records.indexOf(r) >= from).at(-1);
    const requestAt = async (url, from, method = 'GET') => {await waitFor(() => latest(url, from, method), url); return latest(url, from, method);};
    const cancelled = async record => waitFor(() => record.cancelled, `native cancellation: ${record.url}`);

    // Two real controllers reading the same cached route remain independent.
    let index = records.length;
    await page.evaluate(() => {
      rootA = makeRoot('more');
      a = mods.more.mount(rootA, {...ctx, renderExplanation() {}});
    });
    await waitFor(() => records.length >= index + 1, 'first quality read');
    await page.evaluate(() => {rootB = makeRoot('more'); b = mods.more.mount(rootB, {...ctx, renderExplanation() {}});});
    await waitFor(() => records.length >= index + 2, 'two independent quality reads');
    await page.evaluate(() => a.dispose()); await cancelled(records[index]);
    assert.equal(records[index + 1].cancelled, false);
    records[index + 1].respond(quality); await page.evaluate(() => b.ready);
    assert.match(await page.evaluate(() => rootB.querySelector('#moreIssueText').textContent), /警告 2/);
    index = records.length;
    await page.evaluate(() => {rootB.querySelector('#moreNavGrowthDetails').open = true;});
    const explanation = await requestAt('/api/portfolio/equity-bridge', index);
    await page.evaluate(() => b.dispose()); await cancelled(explanation);
    await page.evaluate(() => reset());

    // Cancellation is not the optional endpoint's successful null fallback.
    index = records.length;
    await page.evaluate(() => {root = makeRoot('data_sources'); feature = mods.dataSources.mount(root, {...ctx, location: {search: '?view=issues'}});});
    await waitFor(() => records.length >= index + 2, 'issue quality/overview reads');
    await page.evaluate(() => feature.dispose());
    await Promise.all(records.slice(index).map(cancelled));
    await page.evaluate(() => feature.ready);
    await page.evaluate(() => reset());
    index = records.length;
    await page.evaluate(() => {
      root = makeRoot('data_sources');
      feature = mods.dataSources.mount(root, {...ctx, location: {search: ''}, mountSyncCenter: (root, options) => mods.syncCenter.mount(root, {...ctx, ...options})});
    });
    await waitFor(() => records.length >= index + 2, 'source quality/sync reads');
    await page.evaluate(() => feature.dispose()); await Promise.all(records.slice(index).map(cancelled));
    await page.evaluate(() => feature.ready); await page.evaluate(() => reset());
    // Independently replacing the child sync read must not discard a valid
    // quality payload that the data-source page is still waiting to display.
    index = records.length;
    await page.evaluate(() => {
      root = makeRoot('data_sources');
      feature = mods.dataSources.mount(root, {...ctx, location: {search: ''}, mountSyncCenter: (root, options) => {
        child = mods.syncCenter.mount(root, {...ctx, ...options}); return child;
      }});
    });
    await waitFor(() => records.length >= index + 2, 'independent source quality/sync reads');
    const childRead = latest('/api/sync/center', index);
    await page.evaluate(() => {second = outcome(child.reload());}); await cancelled(childRead);
    latest('/api/portfolio/data-quality', index).respond({...quality, as_of: '2026-10-31'});
    (await requestAt('/api/sync/center', records.indexOf(childRead) + 1)).respond({rows: [], summary: {}, workflows: []});
    await page.evaluate(() => Promise.all([feature.ready, second]));
    assert.equal(await page.evaluate(() => root.querySelector('#asof').textContent), '2026-10-31');
    await page.evaluate(() => {feature.dispose(); reset();});
    index = records.length;
    await page.evaluate(() => {
      root = makeRoot('data_sources'); feature = mods.syncCenter.mount(root, {...ctx, targets: {statusEl: root.querySelector('#statusLine')}});
      first = outcome(feature.reload());
    });
    const oldSync = await requestAt('/api/sync/center', index);
    await page.evaluate(() => {second = outcome(feature.reload());}); await cancelled(oldSync);
    const newSync = await requestAt('/api/sync/center', records.indexOf(oldSync) + 1);
    newSync.respond({rows: [], summary: {}, workflows: []});
    assert.equal(await page.evaluate(() => first), 'AbortError'); assert.equal(await page.evaluate(() => second), 'resolved');
    index = records.length;
    await page.evaluate(() => {
      triggered = mods.syncCenter.triggerSync({source: '合成拒绝发布', trigger_endpoint: '/api/fixture/blocked', trigger_body: {fixture: true}}, {
        statusEl: root.querySelector('#statusLine'), post: mods.api.jpost, after: feature.reload, isActive: () => !feature.disposed,
      });
    });
    const blocked = await requestAt('/api/fixture/blocked', index, 'POST');
    blocked.respond({status: 'blocked', reason: '固定服务端禁止发布'});
    const afterBlocked = await requestAt('/api/sync/center', index);
    await page.evaluate(() => {third = outcome(feature.reload());}); await cancelled(afterBlocked);
    await page.evaluate(() => triggered);
    assert.match(await page.evaluate(() => root.querySelector('#statusLine').textContent), /固定服务端禁止发布/);
    (await requestAt('/api/sync/center', records.indexOf(afterBlocked) + 1)).respond({rows: [], summary: {}, workflows: []});
    await page.evaluate(() => third);
    assert.match(await page.evaluate(() => root.querySelector('#statusLine').textContent), /固定服务端禁止发布/);
    assert.equal(blocked.cancelled, false);
    await page.evaluate(() => {feature.dispose(); reset();});

    // All sequential account endpoints are real reads. A submitted review is
    // intentionally left pending while its page exits, then completes once.
    index = records.length;
    await page.evaluate(() => {root = makeRoot('accounts'); feature = mods.accounts.mount(root, {...ctx});});
    (await requestAt('/api/bank-reconcile/config', index)).respond(accounts.config);
    (await requestAt('/api/accounts/overview', index)).respond(accounts.overview);
    (await requestAt('/api/bank-reconcile/transfer-matches', index)).respond(accounts.matches);
    (await requestAt('/api/bank-reconcile/unmatched-rows', index)).respond(accounts.unmatched);
    (await requestAt('/api/bank-reconcile/imports', index)).respond(accounts.imports);
    await page.evaluate(() => feature.ready);
    await page.evaluate(() => root.querySelector('[data-account-review="fixture/cny"][data-review-action="approve"]').click());
    const review = await requestAt('/api/accounts/fixture%2Fcny/review', index, 'POST');
    await waitFor(() => review.received, 'review POST body');
    const afterPost = records.length;
    await page.evaluate(() => feature.dispose());
    assert.equal(review.cancelled, false); assert.deepEqual(JSON.parse(review.body), {action: 'approve', note: '账户核准中心确认当前余额'});
    review.respond(accounts.overview); await new Promise(resolve => setTimeout(resolve, 30));
    assert.equal(records.length, afterPost, 'no follow-up read or write replay after account disposal');
    await page.evaluate(() => reset());
    index = records.length;
    await page.evaluate(() => {root = makeRoot('accounts'); feature = mods.accounts.mount(root, {...ctx});});
    const configRead = await requestAt('/api/bank-reconcile/config', index);
    await page.evaluate(() => feature.dispose()); await cancelled(configRead);
    await page.evaluate(() => feature.ready); assert.equal(records.length, index + 1, 'account exit stops the remaining chain');
    await page.evaluate(() => reset());

    // One workbench generation owns both GETs; a newer reload cancels both.
    index = records.length;
    await page.evaluate(() => {root = makeRoot('ledger'); feature = mods.ledger.createLedgerController({root, host: window}); first = outcome(feature.loadWorkbench());});
    await waitFor(() => records.length >= index + 2, 'ledger pair');
    await page.evaluate(() => {second = outcome(feature.loadWorkbench());});
    await Promise.all(records.slice(index, index + 2).map(cancelled));
    await waitFor(() => records.length >= index + 4, 'new ledger pair');
    await page.evaluate(() => feature.dispose()); await Promise.all(records.slice(index + 2).map(cancelled));
    assert.equal(await page.evaluate(() => first), 'AbortError'); assert.equal(await page.evaluate(() => second), 'AbortError');
    await page.evaluate(() => reset());

    // Analysis uses its real request adapter, including the investment summary
    // cancellation contract supplied by the investment feature's candidate.
    index = records.length;
    await page.evaluate(() => {
      root = makeRoot('investments'); document.body.dataset.investTab = 'analysis';
      feature = mods.analysis.createAnalysisController({root, host: window, afterNav: task => task()});
      first = outcome(feature.loadReturnAttributionData()); second = outcome(feature.loadLookthroughData());
    });
    await waitFor(() => records.length >= index + 3, 'analysis attribution/holding summary/exposure');
    assert.ok(records.slice(index).some(r => r.url === '/summary2/latest?baseCurrency=CNY'));
    await page.evaluate(() => feature.dispose()); await Promise.all(records.slice(index).map(cancelled));
    assert.equal(await page.evaluate(() => first), 'AbortError'); assert.equal(await page.evaluate(() => second), 'AbortError');
    await page.evaluate(() => reset());

    for (const name of ['risk', 'config']) {
      index = records.length;
      await page.evaluate(name => {root = makeRoot('investments'); feature = mods[name].mount(root, {...ctx, afterStartup: task => task(), showDetails() {}});}, name);
      await waitFor(() => records.length >= index + 1, `${name} read`);
      await page.evaluate(() => {reloading = feature.reload();}); await cancelled(records[index]);
      await waitFor(() => records.length >= index + 2, `${name} replacement read`);
      await page.evaluate(() => feature.dispose()); await cancelled(records[index + 1]);
      await page.evaluate(() => Promise.all([feature.ready, reloading])); await page.evaluate(() => reset());
    }

    index = records.length;
    await page.evaluate(() => {root = makeRoot('investments'); root.querySelector('#trade-attribution-analysis').hidden = true; feature = mods.transaction.mount(root, {...ctx});});
    (await requestAt('/api/investment-transactions/options', index)).respond(transactions.options);
    (await requestAt('/api/investment-transactions?', index)).respond(transactions.ledger);
    await page.evaluate(() => feature.ready);
    index = records.length;
    await page.evaluate(() => root.querySelector('#btnReload').click());
    const oldLedger = await requestAt('/api/investment-transactions?', index);
    await page.evaluate(() => root.querySelector('#btnReload').click()); await cancelled(oldLedger);
    (await requestAt('/api/investment-transactions?', records.indexOf(oldLedger) + 1)).respond(transactions.ledger);
    await page.waitForFunction(() => root.querySelectorAll('.tradeLedgerItem').length === 10);
    index = records.length;
    await page.evaluate(() => root.querySelector('[data-trade-review="1"]').click());
    const oldDetail = await requestAt('/api/investment-transactions/1/attribution', index);
    await page.evaluate(() => mods.ui.closeBottomSheet()); await cancelled(oldDetail);
    index = records.length;
    await page.evaluate(() => root.querySelector('[data-trade-review="2"]').click());
    const replacementDetail = await requestAt('/api/investment-transactions/2/attribution', index);
    await page.evaluate(() => mods.ui.openBottomSheet('其他功能', '<p id="replacement-sheet">合成新浮层</p>'));
    await cancelled(replacementDetail);
    assert.equal(await page.locator('#replacement-sheet').textContent(), '合成新浮层');
    await page.evaluate(() => mods.ui.closeBottomSheet());
    index = records.length;
    await page.evaluate(() => window.dispatchEvent(new Event('pams:load-trade-attribution')));
    (await requestAt('/api/investment-transactions/attribution/summary', index)).respond(transactions.attribution);
    const campaignRead = await requestAt('/api/investment-transactions/campaigns', index);
    await page.evaluate(() => {feature.dispose(); reset();}); await cancelled(campaignRead);
    index = records.length;
    await page.evaluate(() => {root = makeRoot('investments'); root.querySelector('#trade-attribution-analysis').hidden = true; feature = mods.transaction.mount(root, {...ctx});});
    (await requestAt('/api/investment-transactions/options', index)).respond(transactions.options);
    (await requestAt('/api/investment-transactions?', index)).respond(transactions.ledger);
    await page.evaluate(() => feature.ready);
    index = records.length;
    await page.evaluate(() => window.dispatchEvent(new Event('pams:load-trade-attribution')));
    const summaryRead = await requestAt('/api/investment-transactions/attribution/summary', index);
    await page.evaluate(() => {feature.dispose(); reset();}); await cancelled(summaryRead);
    const segment = records.slice(start);
    assert.ok(segment.filter(r => r.cancelled).length >= 20, 'native connection closures are observed across controllers');
    assert.equal(segment.filter(r => r.method === 'POST').length, 2);
    totals.push({transport, requests: segment.length, native_get_aborts: segment.filter(r => r.method === 'GET' && r.cancelled).length, posts_completed: segment.filter(r => r.method === 'POST' && r.ended && !r.cancelled).length});
    await page.close();
  }
  assert.deepEqual(unexpected, []); assert.deepEqual(errors, []);
  console.log(JSON.stringify({native_controller_cancellation: totals, independent_readers: true, cancelled_fallback_is_not_data: true, writes_not_cancelled_or_replayed: true}));
} finally {
  await browser.close();
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
}
