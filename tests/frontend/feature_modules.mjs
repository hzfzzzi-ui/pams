import assert from 'node:assert/strict';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {loadFeatureModule} from './feature_module_loader.mjs';

const publicRoot = path.resolve(process.argv[2] || fileURLToPath(new URL('../../frontend/public', import.meta.url)));
const settle = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => {resolve = yes; reject = no;});
  return {promise, resolve, reject};
};
class Node {
  constructor() { this.nodes = new Map(); this.listeners = new Map(); this.classes = new Set(); this.style = {}; this.textContent = ''; this.hidden = false; this.html = ''; }
  get classList() { return {add: name => this.classes.add(name), remove: name => this.classes.delete(name), contains: name => this.classes.has(name)}; }
  get innerHTML() { return this.html; }
  set innerHTML(value) { this.html = value; this.firstElementChild = new Node(); }
  querySelector(selector) { return this.nodes.get(selector) || null; }
  add(selector, node = new Node()) { this.nodes.set(selector, node); node.parent = this; return node; }
  contains(node) { return node === this || [...this.nodes.values()].some(child => child.contains(node)); }
  addEventListener(name, listener) { if (!this.listeners.has(name)) this.listeners.set(name, new Set()); this.listeners.get(name).add(listener); }
  removeEventListener(name, listener) { this.listeners.get(name)?.delete(listener); }
  dispatch(name, event = {}) { for (const listener of [...this.listeners.get(name) || []]) listener(event); }
  count(name) { return this.listeners.get(name)?.size || 0; }
}
function moreRoot() {
  const root = new Node();
  for (const id of ['moreIssues', 'moreIssueText', 'moreNavGrowthExplanationPanel', 'moreNavGrowthDetails', 'moreOrigin']) root.add(`#${id}`);
  root.querySelector('#moreIssues').add('strong').textContent = '待处理';
  root.querySelector('#moreIssues').hidden = true;
  return root;
}
function dataRoot() {
  const root = new Node();
  for (const id of ['issuesView', 'issuesContent', 'statusLine', 'todayActions', 'sourceList', 'sourceGrid', 'manualSyncStatus', 'asof', 'dataQuality', 'err']) root.add(`#${id}`);
  root.querySelector('#issuesView').hidden = true;
  return root;
}
const quality = {summary: {status: 'warning', warning_count: 2, error_count: 0}, checks: [
  {id: 'account_balance_coverage', status: 'attention', details: {missing_accounts: ['a']}},
  {id: 'valuation_series_freshness', status: 'attention', details: {valuation_series_latest_as_of: '2026-06-18', investment_positions_latest_as_of: '2026-10-29'}},
]};

const more = await loadFeatureModule(publicRoot, 'features/more/controller.js');
{
  const root = moreRoot(), other = moreRoot(), win = new Node(), reads = [], rendered = [];
  const get = (url, options) => { const item = deferred(); reads.push({url, options, ...item}); return item.promise; };
  const context = {get, win, location: {origin: 'http://fixture.invalid'}, renderExplanation: (payload, panel) => {rendered.push(payload); panel.innerHTML = 'NAV fixture';}};
  const page = more.mount(root, context);
  assert.equal(more.mount(root, context), page);
  assert.equal(reads.length, 1);
  assert.equal(reads[0].url, '/api/portfolio/data-quality?baseCurrency=CNY');
  assert.equal(reads[0].options.cache, false);
  reads[0].resolve({data_quality: quality}); await page.ready;
  assert.equal(root.querySelector('#moreIssueText').textContent, '错误 0 · 警告 2');
  assert.equal(other.querySelector('#moreIssueText').textContent, '');
  assert.equal(root.querySelector('#moreOrigin').textContent, 'http://fixture.invalid');
  const details = root.querySelector('#moreNavGrowthDetails'); details.open = true;
  details.dispatch('toggle'); details.dispatch('toggle');
  assert.equal(reads.length, 2, 'Repeated toggle must share the pending read');
  assert.equal(reads[1].url, '/api/portfolio/equity-bridge?start=2026-01-01&baseCurrency=CNY');
  assert.equal(reads[1].options.cache, false);
  const panel = root.querySelector('#moreNavGrowthExplanationPanel');
  page.dispose(); panel.innerHTML = 'after dispose';
  reads[1].resolve({fixture: true}); await settle();
  assert.equal(panel.innerHTML, 'after dispose'); assert.equal(rendered.length, 0);
  assert.equal(details.count('toggle'), 0); assert.equal(win.count('pagehide'), 0);
  details.dispatch('toggle'); assert.equal(reads.length, 2);
  details.open = false;
  const old = more.mount(root, context); old.dispose();
  const fresh = more.mount(root, context);
  reads[3].resolve({data_quality: {summary: {status: 'ok', warning_count: 0, error_count: 0}}}); await fresh.ready;
  reads[2].reject(new Error('obsolete failure')); await old.ready;
  assert.equal(root.querySelector('#moreIssues').hidden, true, 'An old instance may not overwrite a new instance');
  win.dispatch('pagehide'); assert.equal(fresh.disposed, true);
  assert.equal(win.count('pagehide'), 0);
}
{
  const root = moreRoot();
  const page = more.mount(root, {get: async () => {throw new Error('unavailable');}, renderExplanation() {}});
  await page.ready;
  assert.equal(root.querySelector('#moreIssues').hidden, false);
  assert.equal(root.querySelector('#moreIssues').querySelector('strong').textContent, '数据状态暂不可用');
  assert.equal(root.querySelector('#moreIssueText').textContent, ''); page.dispose();
  const recovered = more.mount(root, {get: async () => ({data_quality: quality}), renderExplanation() {}});
  await recovered.ready;
  assert.equal(root.querySelector('#moreIssues').querySelector('strong').textContent, '待处理');
  assert.equal(root.querySelector('#moreIssueText').textContent, '错误 0 · 警告 2'); recovered.dispose();
}

const data = await loadFeatureModule(publicRoot, 'features/data_sources/controller.js');
{
  const root = dataRoot(), other = dataRoot(), win = new Node(), reads = [];
  const context = {win, location: {search: '?view=issues'}, mountSyncCenter() {throw new Error('Issues view must not mount or read sync center');},
    get: (url) => { const item = deferred(); reads.push({url, ...item}); return item.promise; }};
  const page = data.mount(root, context);
  assert.equal(data.mount(root, context), page); assert.equal(reads.length, 2);
  assert.deepEqual(reads.map(item => item.url), ['/api/portfolio/data-quality?baseCurrency=CNY', '/api/accounts/overview']);
  reads[0].resolve({data_quality: quality}); reads[1].resolve({accounts: [{account_code: 'a', display_name: '<合成账户>'}]});
  await page.ready;
  const html = root.querySelector('#issuesContent').innerHTML;
  assert.match(html, /&lt;合成账户&gt;/); assert.match(html, /2026-06-18/); assert.match(html, /2026-10-29/);
  assert.match(html, /accounts.html#overview/); assert.match(html, /data_sources.html#dataQuality/);
  assert.equal(other.querySelector('#issuesContent').innerHTML, '');
  page.dispose();
  assert.equal(root.classList.contains('issues-only'), false); assert.equal(root.querySelector('#issuesView').hidden, true);
  assert.equal(win.count('pagehide'), 0);
  const late = data.mount(root, context); late.dispose(); root.querySelector('#issuesContent').innerHTML = 'preserve';
  reads[2].resolve({data_quality: quality}); reads[3].reject(new Error('late failure')); await late.ready;
  assert.equal(root.querySelector('#issuesContent').innerHTML, 'preserve');
}
{
  const root = dataRoot(), reads = [], syncRead = deferred(); let childDisposed = 0, reloads = 0;
  const page = data.mount(root, {location: {search: ''}, get: url => {reads.push(url); return Promise.resolve({as_of: '2026-10-29', data_quality: null});},
    mountSyncCenter: (mountedRoot, context) => {
      assert.equal(mountedRoot, root); assert.equal(context.targets.rowsEl, root.querySelector('#sourceList'));
      return {reload() {reloads++; return syncRead.promise;}, dispose() {childDisposed++;}};
    }});
  assert.deepEqual(reads, ['/api/portfolio/data-quality?baseCurrency=CNY']); assert.equal(reloads, 1);
  page.dispose(); syncRead.resolve({rows: []}); await page.ready;
  assert.equal(childDisposed, 1); assert.equal(root.querySelector('#asof').textContent, '');
  assert.equal(root.querySelector('#dataQuality').innerHTML, '');
  assert.equal(root.querySelector('#statusLine').classList.contains('syncSummaryStrip'), false);
}

const sync = await loadFeatureModule(publicRoot, 'features/sync_center/controller.js');
const syncPayload = {updated_at: '2026-10-31T00:00:00Z', summary: {ok: 3, manual_required: 2, errors: 1, last_investment_date: '2026-10-29'}, workflows: [], rows: [
  {id: 'fixture', source: '<固定数据>', sync_type: '只读快照', status: 'review', status_label: '待核准', latest_date: '2026-10-29', rows: 123, action: '核对', details: '说明', trigger_endpoint: '/api/fixture/manual', trigger_body: {fixture: true}},
]};
{
  const root = dataRoot(), other = dataRoot(), win = new Node(), reads = [];
  const context = {win, get: () => {const item = deferred(); reads.push(item); return item.promise;},
    post: () => {throw new Error('Reads must never post');}, openSheet: () => {}, targets: {summaryEl: root.querySelector('#statusLine'), rowsEl: root.querySelector('#sourceList'), tableBodyEl: root.querySelector('#sourceGrid'), statusEl: root.querySelector('#manualSyncStatus')}};
  const page = sync.mount(root, context); assert.equal(sync.mount(root, context), page); assert.equal(root.count('click'), 1);
  assert.throws(() => page.setTargets({rowsEl: other.querySelector('#sourceList')}), /outside its root/);
  const first = page.reload(), second = page.reload();
  reads[1].resolve(syncPayload); await second;
  const rows = root.querySelector('#sourceList').innerHTML;
  assert.match(rows, /&lt;固定数据&gt;/); assert.match(rows, /只读快照 · 核对/); assert.match(rows, /2026-10-29/);
  assert.match(root.querySelector('#sourceGrid').innerHTML, /123/);
  assert.match(root.querySelector('#statusLine').innerHTML, /正常<\/span><strong>3/);
  assert.match(root.querySelector('#statusLine').innerHTML, /待人工<\/span><strong>2/);
  assert.match(root.querySelector('#statusLine').innerHTML, /异常<\/span><strong>1/);
  reads[0].resolve({...syncPayload, rows: []}); await assert.rejects(first, error => error.name === 'AbortError');
  assert.equal(root.querySelector('#sourceList').innerHTML, rows, 'Older reloads must not replace newer render');
  const late = page.reload(); page.dispose(); root.querySelector('#sourceList').innerHTML = 'disposed';
  reads[2].reject(new Error('late unavailable')); await assert.rejects(late, error => error.name === 'AbortError');
  assert.equal(root.querySelector('#sourceList').innerHTML, 'disposed');
  assert.equal(root.count('click'), 0); assert.equal(win.count('pagehide'), 0);
  await page.reload(); assert.equal(reads.length, 3);
}
{
  const status = new Node(), write = deferred(); let after = 0, writes = 0, alive = true;
  const pending = sync.triggerSync(syncPayload.rows[0], {statusEl: status, post: (url, body) => {
    assert.equal(url, '/api/fixture/manual'); assert.equal(body.fixture, true); writes++; return write.promise;
  }, after: () => {after++;}, isActive: () => alive});
  assert.equal(status.textContent, '<固定数据> 刷新中...'); alive = false;
  write.resolve({success: true}); await pending;
  assert.equal(status.textContent, '<固定数据> 刷新中...'); assert.equal(after, 0); assert.equal(writes, 1);
  await sync.triggerSync(syncPayload.rows[0], {statusEl: status, post: async () => ({status: 'blocked', reason: '固定阻断原因'}), after: async () => {after++;}});
  assert.equal(after, 1); assert.equal(status.textContent, '<固定数据> 刷新失败：固定阻断原因');
  assert.equal(status.className, 'manualSyncStatus error');
}

// A child reload cancellation is independent of a valid quality result.
{
  const root = dataRoot(), cancelled = Object.assign(new Error('child replaced'), {name: 'AbortError'});
  const page = data.mount(root, {win: new Node(), location: {search: ''},
    get: async () => ({as_of: '2026-10-31', data_quality: quality}),
    mountSyncCenter: () => ({reload: async () => {throw cancelled;}, dispose() {}}),
  });
  await page.ready;
  assert.equal(root.querySelector('#asof').textContent, '2026-10-31');
  assert.match(root.querySelector('#dataQuality').innerHTML, /行动提示/);
  assert.equal(root.querySelector('#err').textContent, ''); page.dispose();
}
// An explicit POST failure remains visible if only its follow-up GET is replaced.
{
  const status = new Node(); let writes = 0;
  await sync.triggerSync(syncPayload.rows[0], {statusEl: status,
    post: async () => {writes++; return {status: 'blocked', reason: '固定服务端失败'};},
    after: async () => {throw Object.assign(new Error('read replaced'), {name: 'AbortError'});},
  });
  assert.equal(writes, 1); assert.equal(status.textContent, '<固定数据> 刷新失败：固定服务端失败');
  assert.equal(status.className, 'manualSyncStatus error');
}

// Legacy entry exports remain usable without automatic DOM boot in Node.
for (const [entry, exports] of [
  ['more.js', ['issueSummary', 'mount', 'dispose']],
  ['data_sources.js', ['issuesHtml', 'renderQuality', 'mount', 'dispose']],
  ['sync_center.js', ['fmtSyncTime', 'renderSyncSummary', 'renderWorkflow', 'renderSyncRows', 'renderSyncTable', 'triggerSync', 'loadSyncCenter', 'mount', 'dispose']],
]) {
  const module = await loadFeatureModule(publicRoot, entry);
  for (const name of exports) assert.equal(typeof module[name], 'function', `${entry} ${name}`);
}
console.log('Low-frequency feature modules: root isolation, exact read scope, pending guards, repeated mount/reload, teardown, legacy exports and manual write semantics passed.');
