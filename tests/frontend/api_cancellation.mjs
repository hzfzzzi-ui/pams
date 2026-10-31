import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const source = readFileSync(process.argv[2] || 'frontend/public/assets/js/api.js', 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
const abortError = () => Object.assign(new Error('aborted transport'), {name: 'AbortError'});

async function fixture(transport = 'fetch') {
  const requests = [], timers = new Map(), stored = new Map();
  const storage = {getItem: k => stored.get(k), setItem: (k,v) => stored.set(k,v),
    removeItem: k => stored.delete(k), key: n => [...stored.keys()][n], get length() { return stored.size; }};
  let timerId = 0;
  const globals = {AbortController, URL, URLSearchParams, console,
    window: {location: {hostname: 'local.invalid', port: '8080', origin: 'http://local.invalid', search: ''}, sessionStorage: storage},
    setTimeout: (fn, ms) => { const id = ++timerId; timers.set(id, {fn, ms}); return id; },
    clearTimeout: id => timers.delete(id)};
  if (transport === 'fetch') globals.fetch = (url, init) => new Promise((resolve, reject) => {
    const request = {url, init, aborted: false};
    requests.push(request);
    let rejectBody;
    init.signal?.addEventListener('abort', () => {
      request.aborted = true; reject(abortError()); rejectBody?.(abortError());
    }, {once: true});
    request.respond = (value, status = 200) => resolve({ok: status < 400, status,
      statusText: status === 409 ? 'Conflict' : 'OK', json: async () => value});
    request.headers = () => resolve({ok: true, status: 200, json: () => new Promise((done, fail) => {
      request.finishBody = done; rejectBody = fail;
    })});
  });
  else globals.XMLHttpRequest = class {
    constructor() { requests.push(this); }
    open(method, url) { this.method = method; this.url = url; }
    setRequestHeader() {}
    send(body) { this.body = body; }
    abort() { this.aborted = true; this.onabort?.(); }
    respond(value, status = 200) { this.status = status; this.statusText = status === 409 ? 'Conflict' : 'OK'; this.responseText = JSON.stringify(value); this.onload(); }
  };
  const module = new vm.SourceTextModule(source, {context: vm.createContext(globals)});
  await module.link(() => { throw new Error('No imports expected'); });
  await module.evaluate();
  return {api: module.namespace, requests, timers, stored};
}

for (const transport of ['fetch', 'xhr']) {
  const f = await fixture(transport), {jget, jpost, clearApiCache} = f.api;
  const path = '/api/accounts/overview';
  const sharedA = jget(path), sharedB = jget(path);
  assert.equal(f.requests.length, 1, 'ordinary readers keep in-flight sharing');
  const owner = new AbortController();
  const owned = jget(path, {signal: owner.signal});
  assert.equal(f.requests.length, 2, 'caller-owned cancellation is independent');
  const cancellation = assert.rejects(owned, e => e.name === 'AbortError' && !e.message.includes('timeout'));
  owner.abort(); await cancellation;
  assert.equal(f.requests[1].aborted, true); assert.ok(!f.requests[0].aborted);
  const value = {amount: null, currency: 'CNY', status: 'unknown'};
  f.requests[0].respond(value);
  assert.equal(JSON.stringify(await sharedA), JSON.stringify(value)); assert.equal(JSON.stringify(await sharedB), JSON.stringify(value));
  assert.equal(JSON.stringify(await jget(path)), JSON.stringify(value)); assert.equal(f.requests.length, 2);
  await assert.rejects(jget(path, {signal: owner.signal}), e => e.name === 'AbortError');
  assert.equal(f.requests.length, 2, 'already-cancelled reads do not fetch or return cached data');
  clearApiCache();

  const timeout = jget(path, {cache: false});
  const timedOut = assert.rejects(timeout, /Request timeout \(30s\)/);
  if (transport === 'fetch') [...f.timers.values()].find(t => t.ms === 30000).fn();
  else f.requests.at(-1).ontimeout();
  await timedOut;

  const posted = jpost('/synthetic-write-only', {revision: 'fixture'});
  const conflict = assert.rejects(posted, /409 Conflict/);
  f.requests.at(-1).respond(null, 409); await conflict;
  assert.equal(f.requests.filter(r => (r.init?.method || r.method) === 'POST').length, 1, 'no write retry');
  assert.equal(f.timers.size, 0, 'transport timers are cleared');
}
{
  const f = await fixture(), owner = new AbortController();
  const pending = f.api.jget('/api/accounts/overview', {signal: owner.signal});
  f.requests[0].headers(); await tick();
  assert.equal(typeof f.requests[0].finishBody, 'function');
  const rejected = assert.rejects(pending, e => e.name === 'AbortError');
  owner.abort(); await rejected;
  assert.equal(f.requests[0].aborted, true, 'body consumption remains cancellable');
  assert.equal(f.stored.size, 0, 'cancelled payload never enters the cache');
  const retry = f.api.jget('/api/accounts/overview');
  assert.equal(f.requests.length, 2);
  f.requests[1].respond({amount: '123.456', status: 'fixture'}); await retry;
  assert.equal(f.timers.size, 0);
}
console.log('Read cancellation: fetch/XHR, shared reader isolation, settled cache, timeout, body abort and no write retry passed.');
