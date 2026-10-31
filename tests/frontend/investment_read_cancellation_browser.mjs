import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const {chromium} = await import(pathToFileURL(process.env.PAMS_FRONTEND_PLAYWRIGHT_MODULE).href);
const publicRoot = path.resolve(process.argv[2]);
const records = [], unexpected = [], errors = [], totals = [];
const isBusiness = pathname => ['/api/', '/nav/', '/investment/'].some(prefix => pathname.startsWith(prefix));
const waitFor = async (predicate, label) => {
  const end = Date.now() + 8000;
  while (!predicate()) {
    if (Date.now() > end) throw new Error(`Timed out: ${label}`);
    await new Promise(resolve => setTimeout(resolve, 10));
  }
};
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://fixture.invalid');
  res.setHeader('Access-Control-Allow-Origin', '*');
  if (isBusiness(url.pathname)) {
    const record = {url: req.url, method: req.method, cancelled: false, ended: false}; records.push(record);
    res.on('close', () => {if (!res.writableEnded) record.cancelled = true;});
    record.respond = data => {record.ended = true; res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(data));};
    return;
  }
  if (url.pathname === '/') {res.setHeader('Content-Type', 'text/html'); res.end('<!doctype html><meta charset="utf-8"><title>固定投资取消测试</title><body></body>'); return;}
  const file = path.resolve(publicRoot, `.${url.pathname}`);
  if (!file.startsWith(publicRoot + path.sep)) {res.statusCode = 404; res.end(); return;}
  try {
    res.setHeader('Content-Type', file.endsWith('.html') ? 'text/html' : file.endsWith('.css') ? 'text/css' : 'text/javascript');
    res.end(await readFile(file));
  } catch {res.statusCode = 404; res.end();}
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const base = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch({headless: true, channel: process.env.PAMS_FRONTEND_PLAYWRIGHT_CHANNEL || undefined});
const nav = {rows: [{date: '2026-10-29', netWorth: '1000.125', confirmation_status: 'ok'}, {date: '2026-10-30', netWorth: '1040.5', confirmation_status: 'unconfirmed'}]};
const returns = {rows: [{date: '2026-10-29', market_value: 1000.125, total_pnl: 0, return_rate: 0, cumulative_capital: 1000.125},
  {date: '2026-10-30', market_value: 1040.5, total_pnl: 40.375, return_rate: 0.04037, cumulative_capital: 1000.125}]};
try {
  for (const transport of ['fetch', 'xhr']) {
    const start = records.length;
    const page = await browser.newPage();
    page.on('pageerror', error => errors.push(`${transport}: ${error.message}`));
    await page.route('**/*', route => {
      const request = route.request(), url = new URL(request.url());
      if (url.hostname !== '127.0.0.1' || ![String(server.address().port), '8090'].includes(url.port) || request.method() !== 'GET') {
        unexpected.push(`${request.method()} ${url.href}`); return route.abort();
      }
      if (isBusiness(url.pathname)) return route.continue({url: base + url.pathname + url.search});
      if (url.origin !== base) {unexpected.push(url.href); return route.abort();}
      return route.continue();
    });
    if (transport === 'xhr') await page.addInitScript(() => {window.fetch = undefined;});
    await page.goto(base);
    await page.evaluate(async () => {
      const [api, charts, bootstrap] = await Promise.all([
        import('/assets/js/api.js?v=20261031-modules-v1'),
        import('/assets/js/features/investment/charts/controller.js'),
        import('/assets/js/app/page_bootstrap.js'),
      ]);
      class Chart {
        static instances = [];
        constructor(canvas, config) {this.canvas = canvas; this.data = config.data; this.options = config.options; this.scales = {}; this.destroyCount = 0; Chart.instances.push(this);}
        destroy() {this.destroyCount++;} update() {}
        getDatasetMeta() {return {data: []};} getElementsAtEventForMode() {return [];}
      }
      const presenters = {esc: String, statusBadge: String, formatNumber: String, formatPercentRate: String,
        fmtCny: String, fmtSignedCny: String, fmtSignedPctFromRate: String, TopContributionPanel: () => ''};
      const makeRoot = () => {
        const root = document.createElement('section');
        root.innerHTML = '<canvas id="investmentReturnChart"></canvas><canvas id="investmentNavChart"></canvas><div id="investmentReturnToggle"></div><div id="investmentReturnWindowToggle"></div><div id="returnEventHotspots"></div><div id="returnEventPopover"></div><div id="investmentReturnNote"></div>';
        document.body.append(root); return root;
      };
      Object.assign(window, {api, charts, bootstrap, Chart, presenters, makeRoot, rootA: makeRoot(), rootB: makeRoot()});
      window.create = root => charts.createInvestmentChartController({root, host: window, presenters, ChartClass: Chart});
      window.a = create(rootA); window.oldLoading = a.loadNavOnce();
    });
    await waitFor(() => records.length === start + 1, 'first NAV request');
    // Overlap the same NAV endpoint without exhausting the browser's six HTTP/1
    // connections before the second owner's requests can reach the fixture.
    await page.evaluate(() => {window.b = create(rootB); window.bNavLoading = b.loadNavOnce();});
    await waitFor(() => records.length === start + 2, 'independent overlapping NAV reader');
    await page.evaluate(() => {window.replacement = create(rootA);});
    await waitFor(() => records[start].cancelled, 'replaced owner cancels its physical NAV GET');
    assert.equal(records[start + 1].cancelled, false);
    assert.equal(await page.evaluate(() => oldLoading), null);
    assert.equal(await page.evaluate(() => Chart.instances.length), 0, 'cancellation must not draw empty charts');
    records[start + 1].respond(nav);
    await page.evaluate(() => bNavLoading);
    await page.evaluate(() => {window.bLoading = b.loadReturnsOnce();});
    await waitFor(() => records.length === start + 7, 'five return requests after independent NAV');
    for (const record of records.slice(start + 2, start + 7)) record.respond(returns);
    await page.evaluate(() => bLoading);
    assert.deepEqual(await page.evaluate(() => b.state.navRows.map(row => row.netWorth)), ['1000.125', '1040.5']);
    assert.equal(await page.evaluate(() => b.state.navRows[1].confirmation_status), 'unconfirmed');
    assert.equal(await page.evaluate(() => Chart.instances.length), 2);
    await page.evaluate(() => replacement.dispose());

    // Same-owner replacement cancels all five old series, with NAV kept intact.
    let index = records.length;
    await page.evaluate(() => {api.clearApiCache(); window.older = b.renderReturns();});
    await waitFor(() => records.length === index + 5, 'old return refresh');
    await page.evaluate(() => {window.newer = b.renderReturns();});
    await waitFor(() => records.length === index + 10, 'replacement return refresh');
    await waitFor(() => records.slice(index, index + 5).every(record => record.cancelled), 'same-key physical cancellation');
    assert.equal(await page.evaluate(() => older), null);
    for (const record of records.slice(index + 5)) record.respond(returns);
    await page.evaluate(() => newer);
    assert.deepEqual(await page.evaluate(() => b.state.navRows.map(row => row.netWorth)), ['1000.125', '1040.5']);
    assert.equal(await page.evaluate(() => rootB.dataset.investmentReturnChartError), undefined);

    index = records.length;
    await page.evaluate(() => {api.clearApiCache(); window.exiting = b.renderReturns();});
    await waitFor(() => records.length === index + 5, 'pending returns before disposal');
    await page.evaluate(() => b.dispose());
    await waitFor(() => records.slice(index).every(record => record.cancelled), 'disposal physical cancellation');
    assert.equal(await page.evaluate(() => exiting), null);
    assert.equal(await page.evaluate(() => rootB.dataset.investmentReturnChartError), undefined);
    assert.ok(await page.evaluate(() => Chart.instances.every(chart => chart.destroyCount === 1)));

    // Actual warmApiCache starts only after its original startup gate; disposing
    // it cancels four reads, while an independent foreground read keeps running.
    index = records.length;
    await page.evaluate(() => {
      api.clearApiCache();
      const timers = new Map(); let id = 0;
      window.allowStartup = null;
      const gate = new Promise(resolve => {allowStartup = resolve;});
      const win = {AbortController: window.AbortController, navigator: {},
        setTimeout(callback, delay) {const key = ++id; timers.set(key, {callback, delay}); return key;},
        clearTimeout(key) {timers.delete(key);},
        addEventListener: window.addEventListener.bind(window), removeEventListener: window.removeEventListener.bind(window)};
      const doc = {baseURI: `${location.origin}/investments.html?content=1`, body: document.body, head: document.head,
        querySelectorAll: () => [], createElement: name => document.createElement(name)};
      window.warmup = bootstrap.createPageBootstrap({doc, win, afterStartup: task => gate.then(task), warmCache: api.warmApiCache, now: () => new Date(2026, 9, 31)});
      warmup.start();
      window.delays = [...timers.values()].map(timer => timer.delay);
      for (const timer of [...timers.values()]) timer.callback();
    });
    assert.deepEqual(await page.evaluate(() => delays), [250, 900]);
    assert.equal(records.length, index, 'startup gate still prevents prefetch GETs');
    await page.evaluate(() => allowStartup());
    await waitFor(() => records.length === index + 4, 'four warmup GETs');
    assert.deepEqual(records.slice(index).map(record => record.url).sort(), [
      '/api/accounts/overview', '/api/sync/center', '/api/portfolio/data-quality?baseCurrency=CNY', '/api/ledger/workbench?month=2026-10',
    ].sort());
    await page.evaluate(() => {window.foregroundOwner = new AbortController(); window.foreground = api.jget('/api/accounts/overview', {signal: foregroundOwner.signal});});
    await waitFor(() => records.length === index + 5, 'foreground reader overlapping warmup');
    await page.evaluate(() => warmup.dispose());
    await waitFor(() => records.slice(index, index + 4).every(record => record.cancelled), 'warmup physical cancellation');
    assert.equal(records[index + 4].cancelled, false);
    records[index + 4].respond({synthetic: true, amount: '123.456'});
    assert.equal(await page.evaluate(async () => (await foreground).amount), '123.456');
    const segment = records.slice(start);
    assert.equal(segment.length, 27); assert.equal(segment.filter(record => record.cancelled).length, 15);
    assert.ok(segment.every(record => record.method === 'GET'));
    totals.push({transport, synthetic_gets: segment.length, native_cancellations: segment.filter(record => record.cancelled).length});
    await page.close();
  }
  assert.deepEqual(unexpected, []); assert.deepEqual(errors, []);
  console.log(JSON.stringify({investment_and_warmup_cancellation: totals, independent_readers: true, cancellation_does_not_draw_empty_chart: true, startup_and_four_warmup_paths_preserved: true}));
} finally {
  await browser.close(); server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
}
