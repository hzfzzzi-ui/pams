import assert from 'node:assert/strict';
import {createReadSession} from '../../frontend/public/assets/js/features/portfolio/read_session.js';
import {loadRiskDisclosure, loadPortfolioConfig} from '../../frontend/public/assets/js/features/portfolio/requests.js';

const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return {promise, resolve, reject}; };
const fixture = overrides => {
  const reads = [], applied = [], errors = [], tabs = [];
  const session = createReadSession({win: {AbortController}, tab: 'risk',
    afterStartup: (task, tab) => { tabs.push(tab); return task(); },
    read: async signal => { reads.push(signal); return {fixed: true}; },
    apply: data => applied.push(data), onError: error => errors.push(error), ...overrides});
  return {session, reads, applied, errors, tabs};
};

{
  const calls = [], signal = new AbortController().signal;
  const get = async (...args) => { calls.push(args); return {fixed: true}; };
  await loadRiskDisclosure(get, signal); await loadPortfolioConfig(get, signal);
  assert.deepEqual(calls, [
    ['/api/portfolio/risk-disclosure?baseCurrency=CNY', {cache: false, signal}],
    ['/api/portfolio/config?baseCurrency=CNY', {cache: false, signal}],
  ]);
}
{
  const f = fixture(); await f.session.reload();
  assert.deepEqual(f.tabs, ['risk']); assert.equal(f.reads.length, 1); assert.equal(f.applied.length, 1);
  f.session.dispose(); f.session.dispose(); await f.session.reload();
  assert.equal(f.session.signal.aborted, true); assert.equal(f.reads.length, 1);
}
{
  const gate = deferred();
  const f = fixture({afterStartup: task => gate.promise.then(task)});
  const ready = f.session.reload(); f.session.dispose(); gate.resolve(); await ready;
  assert.equal(f.reads.length, 0); assert.equal(f.applied.length, 0); assert.equal(f.errors.length, 0);
}
{
  const response = deferred(); let signal;
  const f = fixture({read: current => { signal = current; return response.promise; }});
  const ready = f.session.reload(); f.session.dispose(); response.resolve({late: true}); await ready;
  assert.equal(signal.aborted, true); assert.equal(f.applied.length, 0); assert.equal(f.errors.length, 0);
}
{
  const first = deferred(), second = deferred(), signals = [];
  const f = fixture({read: signal => { signals.push(signal); return signals.length === 1 ? first.promise : second.promise; }});
  const older = f.session.reload(), newer = f.session.reload();
  assert.equal(signals[0].aborted, true); assert.equal(signals[1].aborted, false);
  second.resolve({version: 2}); await newer; first.resolve({version: 1}); await older;
  assert.deepEqual(f.applied, [{version: 2}]); f.session.dispose();
}
{
  const gate = deferred();
  const f = fixture({afterStartup: task => gate.promise.then(task)});
  const first = f.session.reload(), second = f.session.reload(); gate.resolve(); await Promise.all([first, second]);
  assert.equal(f.reads.length, 1); assert.equal(f.applied.length, 1); f.session.dispose();
}
{
  const failure = new Error('fixed read failure'); const f = fixture({read: async () => { throw failure; }});
  await f.session.reload(); assert.deepEqual(f.errors, [failure]); assert.deepEqual(f.applied, []); f.session.dispose();
}
{
  const response = deferred(); const f = fixture({read: () => response.promise});
  const ready = f.session.reload(); f.session.dispose(); response.reject(new Error('late failure')); await ready;
  assert.deepEqual(f.errors, []);
}
console.log('Portfolio reads: API scope, startup disposal, late success/failure and overlapping loads passed.');
