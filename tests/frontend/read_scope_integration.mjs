import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import vm from 'node:vm';

const publicRoot = path.resolve(process.argv[2] || fileURLToPath(new URL('../../frontend/public', import.meta.url)));
const requests = [], stored = new Map(), timers = new Map(); let timerId = 0;
const aborted = () => Object.assign(new Error('cancelled native fixture'), {name: 'AbortError'});
const context = vm.createContext({AbortController, URL, URLSearchParams,
  window: {location: {hostname: 'fixture.invalid', port: '8080', origin: 'http://fixture.invalid', search: ''}, sessionStorage: {
    getItem: key => stored.get(key), setItem: (key, value) => stored.set(key, value), removeItem: key => stored.delete(key), key: index => [...stored.keys()][index], get length() {return stored.size;},
  }},
  setTimeout: (fn, ms) => {const id = ++timerId; timers.set(id, {fn, ms}); return id;}, clearTimeout: id => timers.delete(id),
  fetch: (url, init) => new Promise((resolve, reject) => {
    const item = {url, init, aborted: false}; requests.push(item);
    init.signal?.addEventListener('abort', () => {item.aborted = true; reject(aborted());}, {once: true});
    item.respond = (value, status = 200) => resolve({ok: status < 400, status, statusText: status >= 400 ? 'Synthetic failure' : 'OK', json: async () => value});
  }),
});
async function load(relative) {
  const module = new vm.SourceTextModule(readFileSync(path.join(publicRoot, relative), 'utf8'), {context});
  await module.link(() => {throw new Error('Transport/read scope has no module dependencies');});
  await module.evaluate(); return module.namespace;
}
const api = await load('assets/js/api.js');
const {createReadScope, readFallback, isReadCancelled} = await load('assets/js/shared/read_scope.js');
const rejection = promise => assert.rejects(promise, error => isReadCancelled(error) && error.name === 'AbortError');
const outcome = promise => promise.then(value => ({value}), error => ({error}));
const left = createReadScope({get: api.jget}), right = createReadScope({get: api.jget});

const a = left.get('overview', '/api/accounts/overview', {cache: false});
const b = right.get('overview', '/api/accounts/overview', {cache: false});
const cancelledA = rejection(a); left.dispose(); await cancelledA;
assert.equal(requests[0].aborted, true); assert.equal(requests[1].aborted, false);
requests[1].respond({amount: '123.456', status: 'synthetic'});
assert.equal((await b).amount, '123.456');
await rejection(left.get('overview', '/api/accounts/overview'));
assert.equal(requests.length, 2, 'disposed scope starts no further GET');

const old = right.get('ledger', '/api/ledger/workbench?old', {cache: false});
const oldRejected = rejection(old);
const newer = right.get('ledger', '/api/ledger/workbench?new', {cache: false});
await oldRejected; assert.equal(requests[2].aborted, true);
requests[3].respond({revision: 'new'}); assert.equal((await newer).revision, 'new');

const parent = new AbortController(), caller = new AbortController();
const combined = createReadScope({get: api.jget, signal: parent.signal});
const owned = combined.get('caller', '/synthetic-caller', {signal: caller.signal});
const ownedRejected = rejection(owned); caller.abort('caller left'); await ownedRejected;
assert.equal(requests[4].aborted, true); assert.equal(combined.disposed, false);
const parentOwned = combined.get('parent', '/synthetic-parent');
const parentRejected = rejection(parentOwned); parent.abort('page left'); await parentRejected;
assert.equal(requests[5].aborted, true); assert.equal(combined.disposed, true);

// A cancellation remains a rejected read, never a successful optional fallback.
const group = createReadScope({get: api.jget});
const pair = group.run('pair', signal => Promise.all([
  api.jget('/synthetic-primary', {signal}),
  api.jget('/synthetic-optional', {signal}).catch(error => readFallback(error, {accounts: []})),
]));
const pairRejected = rejection(pair); group.cancel('pair'); await pairRejected;
assert.equal(requests[6].aborted, true); assert.equal(requests[7].aborted, true);
const normalFailure = new Error('ordinary optional failure');
assert.deepEqual(readFallback(normalFailure, {accounts: []}), {accounts: []});

// A non-optional failure aborts pending siblings while preserving its real error.
const failedGroup = group.run('failure', signal => Promise.all([
  api.jget('/synthetic-required', {signal}), api.jget('/synthetic-sibling', {signal}),
]));
const failed = assert.rejects(failedGroup, /500 Synthetic failure/);
requests[8].respond({}, 500); await failed;
assert.equal(requests[9].aborted, true);

// Old injected readers still receive guards even if they ignore cancellation.
let resolveIgnored;
const ignored = createReadScope({get: () => new Promise(resolve => {resolveIgnored = resolve;})});
const ignoredRead = outcome(ignored.get('ignored', '/synthetic-ignored'));
ignored.dispose(); resolveIgnored({amount: 999});
assert.equal((await ignoredRead).error.name, 'AbortError');

// GET cancellation does not gain authority over already-submitted writes.
const write = api.jpost('/synthetic-write', {revision: 'fixture'});
const reading = group.get('read', '/synthetic-last'); const readRejected = rejection(reading);
group.dispose(); await readRejected;
assert.equal(requests[10].init.method, 'POST'); assert.equal(requests[10].aborted, false);
assert.equal(requests[11].aborted, true);
requests[10].respond({accepted: true}); assert.equal((await write).accepted, true);
assert.equal(requests.filter(item => item.init.method === 'POST').length, 1);
right.dispose(); assert.equal(timers.size, 0);
console.log('Read scope/API integration: independent GETs, same-key replacement, parent/caller abort, cancelled fallbacks, failed parallel siblings, ignored-reader guard and untouched POST passed.');
