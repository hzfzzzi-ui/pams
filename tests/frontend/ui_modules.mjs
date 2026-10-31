import assert from 'node:assert/strict';
import {createPageBootstrap, collectPrefetchResources} from '../../frontend/public/assets/js/app/page_bootstrap.js';
import {resolveContentRoute, primaryPageFor, replaceContentAddress} from '../../frontend/public/assets/js/app/routes.js';
import {dataStateHtml} from '../../frontend/public/assets/js/components/data_state.js';

const settle = () => new Promise(resolve => setImmediate(resolve));
function fixture({saveData = false, gate} = {}) {
  let nextId = 0;
  const timers = new Map(), idle = new Map(), listeners = new Map(), nodes = [], calls = [], signals = [];
  const node = (tagName, attr, value) => ({tagName, getAttribute: key => key === attr ? value : null});
  const resources = [
    node('LINK', 'href', 'assets/css/tokens.css?v=fresh-token-version'),
    node('SCRIPT', 'src', 'assets/js/holdings.js?v=fresh-controller-version'),
    node('SCRIPT', 'src', 'https://outside.invalid/assets/secret.js'),
    node('SCRIPT', 'src', '/api/account-import/raw?file=private'),
  ];
  const doc = {
    baseURI: 'http://local.invalid/investments.html?content=1',
    body: {classList: {add() {}}},
    head: {append: node => nodes.push(node)},
    querySelectorAll: () => resources,
    createElement: () => ({dataset: {}, remove() { this.removed = true; }}),
  };
  const win = {
    navigator: {connection: {saveData}},
    setTimeout(callback, delay) { const id = ++nextId; timers.set(id, {callback, delay}); return id; },
    clearTimeout: id => timers.delete(id),
    requestIdleCallback(callback) { const id = ++nextId; idle.set(id, callback); return id; },
    cancelIdleCallback: id => idle.delete(id),
    addEventListener: (name, callback) => listeners.set(name, callback),
    removeEventListener: (name, callback) => { if (listeners.get(name) === callback) listeners.delete(name); },
  };
  const controller = createPageBootstrap({doc, win,
    afterStartup: callback => gate ? gate.then(callback) : Promise.resolve().then(callback),
    warmCache: (paths, {signal}) => {calls.push(paths); signals.push(signal);}, now: () => new Date(2026, 9, 31),
  });
  async function expire() {
    for (const [id, timer] of [...timers]) { timers.delete(id); timer.callback(); }
    for (const [id, callback] of [...idle]) { idle.delete(id); callback(); }
    await settle();
  }
  return {controller, expire, timers, idle, listeners, nodes, calls, signals, doc};
}

{
  const f = fixture();
  f.controller.start(); f.controller.start();
  assert.equal(f.timers.size, 2);
  assert.deepEqual([...f.timers.values()].map(t => t.delay), [250, 900]);
  await f.expire();
  assert.equal(f.calls.length, 1);
  assert.deepEqual(f.calls[0], ['/api/accounts/overview', '/api/sync/center',
    '/api/portfolio/data-quality?baseCurrency=CNY', '/api/ledger/workbench?month=2026-10']);
  assert.equal(f.nodes.length, 4);
  assert.equal(new Set(f.nodes.map(n => n.href)).size, f.nodes.length);
  assert.ok(f.nodes.some(n => n.href.endsWith('tokens.css?v=fresh-token-version')));
  assert.ok(f.nodes.some(n => n.href.endsWith('holdings.js?v=fresh-controller-version')));
  assert.ok(!f.nodes.some(n => n.href.includes('/api/') || n.href.includes('outside.invalid')));
  assert.deepEqual(collectPrefetchResources(f.doc).filter(n => n.as === 'document').map(n => new URL(n.href).pathname),
    ['/ledger.html', '/more.html']);
  assert.equal(f.signals[0].aborted, false);
  f.controller.dispose(); assert.equal(f.listeners.size, 0);
  assert.equal(f.signals[0].aborted, true);
  assert.ok(f.nodes.every(node => node.removed));
}
{
  let resolve;
  const f = fixture({gate: new Promise(done => { resolve = done; })});
  f.controller.start(); await f.expire();
  assert.equal(f.nodes.length, 0); assert.equal(f.calls.length, 0);
  f.controller.dispose(); resolve(); await settle();
  assert.equal(f.nodes.length, 0); assert.equal(f.calls.length, 0);
}
{
  const f = fixture(); f.controller.start(); f.listeners.get('pagehide')();
  assert.equal(f.timers.size, 0); assert.equal(f.listeners.size, 0);
  await f.expire(); assert.equal(f.nodes.length, 0); assert.equal(f.calls.length, 0);
}
{
  const f = fixture({saveData: true}); f.controller.start(); await f.expire();
  assert.equal(f.nodes.length, 0); assert.equal(f.calls.length, 0); f.controller.dispose();
}
assert.equal(resolveContentRoute('https://outside.invalid/ledger.html', 'http://local.invalid/app.html'), null);
assert.equal(resolveContentRoute('/unrelated/ledger.html', 'http://local.invalid/app.html'), null);
assert.equal(resolveContentRoute('unknown.html', 'http://local.invalid/app.html'), null);
assert.equal(resolveContentRoute('ledger.html?account=fixture#recent-flow-section', 'http://local.invalid/app.html').url.hash, '#recent-flow-section');
assert.equal(primaryPageFor('account_import.html'), 'more.html');
assert.equal(primaryPageFor('unknown.html'), null);
{
  const calls = [], messages = [];
  const win = {location: new URL('http://local.invalid/ledger.html?content=1'),
    history: {replaceState: (...args) => calls.push(args), pushState() { throw new Error('Tabs must not push history'); }},
    parent: {postMessage: (...args) => messages.push(args)}};
  assert.equal(replaceContentAddress('ledger.html?content=1#recent-flow-section', win), true);
  assert.deepEqual(calls, [[null, '', 'http://local.invalid/ledger.html?content=1#recent-flow-section']]);
  assert.deepEqual(messages, [[{type: 'pams-content-route', href: calls[0][2]}, 'http://local.invalid']]);
  for (const invalid of ['more.html', 'https://outside.invalid/ledger.html', '/elsewhere/ledger.html']) {
    assert.equal(replaceContentAddress(invalid, win), false);
  }
  assert.equal(calls.length, 1); assert.equal(messages.length, 1);
  win.parent = win;
  assert.equal(replaceContentAddress('#cashflow-workbench', win), true);
  assert.equal(messages.length, 1);
}
assert.match(dataStateHtml({tone: 'error', title: '<Unavailable>', detail: 'retry & review'}), /role="alert" aria-live="assertive"/);
assert.match(dataStateHtml({title: '<Unavailable>', detail: 'retry & review'}), /&lt;Unavailable&gt;/);
assert.match(dataStateHtml(), /暂无数据/);
console.log('UI modules: idle/startup ordering, disposal, live asset versions, read scope, route allowlist and data states passed.');
