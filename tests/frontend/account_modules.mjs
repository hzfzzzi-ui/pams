import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import vm from 'node:vm';
import {config, overview, matches, unmatched, imports, initialUrls, responseFor} from './fixtures/accounts_lifecycle.mjs';

const publicRoot = path.resolve(process.argv[2] || fileURLToPath(new URL('../../frontend/public', import.meta.url)));
async function load(entry) {
  const context = vm.createContext({URLSearchParams});
  const modules = new Map();
  function moduleAt(file) {
    if (!modules.has(file)) modules.set(file, new vm.SourceTextModule(readFileSync(file, 'utf8'), {context, identifier: file}));
    return modules.get(file);
  }
  const module = moduleAt(path.join(publicRoot, 'assets/js', entry));
  await module.link((specifier, parent) => moduleAt(path.resolve(path.dirname(parent.identifier), specifier.split('?')[0])));
  await module.evaluate();
  return module.namespace;
}
class Node {
  constructor() {this.innerHTML = ''; this.textContent = ''; this.value = ''; this.dataset = {}; this.style = {}; this.nodes = new Map(); this.listeners = new Map();}
  querySelector(selector) {if (!selector.startsWith('#')) return null; if (!this.nodes.has(selector)) this.nodes.set(selector, new Node()); return this.nodes.get(selector);}
  querySelectorAll() {return [];}
  addEventListener(name, listener) {if (!this.listeners.has(name)) this.listeners.set(name, new Set()); this.listeners.get(name).add(listener);}
  removeEventListener(name, listener) {this.listeners.get(name)?.delete(listener);}
  contains(node) {return node === this || [...this.nodes.values()].includes(node);}
  count(name) {return this.listeners.get(name)?.size || 0;}
}
const deferred = () => {let resolve, reject; const promise = new Promise((a, b) => {resolve = a; reject = b;}); return {resolve, reject, promise};};
const settle = () => new Promise(resolve => setImmediate(resolve));
const {createView} = await load('features/accounts/view.js');
{
  const root = new Node(), other = new Node();
  const state = {config, accountOverview: null, collapsedAccountGroups: new Set()};
  const view = createView(root, state);
  view.renderAccountCenter(overview);
  assert.equal(state.accountOverview, overview);
  assert.match(root.querySelector('#accountOverviewRows').innerHTML, /合成 &lt;人民币&gt;/);
  for (const value of ['1,000.00', '3,000.00', '-50.00', '88.25', '4,000.00', 'HKD', 'USD']) assert.ok(root.querySelector('#accountBalanceRows').innerHTML.includes(value), value);
  for (const value of ['合成账单', '核对合成金额', 'synthetic', '980.00', '合成备注', '合成日期过期', '平台总额 CNY 5,000.00 × 0.6', 'data-account-review="fixture/cny"', 'data-review-action="approve"', 'data-review-action="defer_missing"']) {
    assert.ok((root.querySelector('#accountOverviewRows').innerHTML + root.querySelector('#accountBalanceRows').innerHTML).includes(value), value);
  }
  assert.match(root.querySelector('#accountMobileHero').innerHTML, /待处理账户[\s\S]*?>3</);
  assert.match(root.querySelector('#accountKpis').innerHTML, /差异金额[\s\S]*?¥20.00/);
  assert.match(root.querySelector('#accountIssueQueue').innerHTML, /缺数据/);
  assert.equal(root.querySelector('#accountBalanceRows').innerHTML.includes('null'), false);
  assert.equal(other.querySelector('#accountOverviewRows').innerHTML, '');
  assert.equal(view.moneyOrDash(null), '—'); assert.equal(view.moneyAmountObj({amount: ''}), '—');
  assert.equal(view.balanceSummaryAmount([overview.accounts[0], overview.accounts[2]]), '<span class="muted">见子账户</span>');
  assert.equal(view.accountDiffAmount(overview.accounts[0]), 20);
  assert.equal(view.negativeAssetAccounts(overview.accounts).length, 0);
  root.dataset.accountFilter = 'missing';
  assert.deepEqual(Array.from(view.accountMobileRows(overview.accounts), row => row.account_code), ['fixture/hkd']);
  view.renderProfiles(); view.selectProfile('synthetic_hkd');
  assert.equal(root.querySelector('#profileCode').value, 'synthetic_hkd'); assert.equal(root.querySelector('#since').value, '2026-02-02');
  assert.equal(root.querySelector('#btnImport').href, 'account_import.html?kind=cash&account=fixture%2Fhkd');
  view.renderRelationships(); assert.match(root.querySelector('#relationshipList').innerHTML, /合成银行 → 合成券商/);
  view.renderTransferMatches(matches);
  for (const value of ['2026-10-29', '银行转账匹配', 'fixture/cny → fixture/hkd', 'HK$88.25', '95.0%', '合成匹配说明', '2026-10-28 合成转出 / 合成来源备注', '合成转入 / 合成去向备注']) assert.ok(root.querySelector('#transferMatchRows').innerHTML.includes(value), value);
  view.renderUnmatchedRows(unmatched);
  for (const value of ['2026-10-30', '缺成对流水', 'fixture/hkd', '-HK$12.50', '合成分类 / 合成子类', '合成摘要 合成描述', '合成缺口', '合成对方']) assert.ok(root.querySelector('#unmatchedRows').innerHTML.includes(value), value);
  view.renderImports(imports);
  for (const value of ['2026-10-30 12:00:00', '合成银行模板', 'fixture/cny', 'accounts.csv', '9', '¥1,000.00']) assert.ok(root.querySelector('#importRows').innerHTML.includes(value), value);
  for (const value of ['¥1,000.00', '¥120.00', '¥20.00', '+¥100.00', '2026-10-29', '余额来源: 持仓快照']) assert.ok(root.querySelector('#summaryCards').innerHTML.includes(value), value);
  assert.doesNotMatch(root.querySelector('#importRows').innerHTML, /\/synthetic\//);
}
const {mount} = await load('features/accounts/controller.js');
{
  const root = new Node(), win = new Node(), reads = [], urls = [];
  const location = {hash: '#balance', pathname: '/accounts.html', search: '?content=1'};
  const context = {win, location, replaceAddress: url => urls.push(url), get: url => {const item = deferred(); reads.push({url, ...item}); return item.promise;}, post: () => {throw new Error('No write during entry');}};
  const page = mount(root, context); assert.equal(mount(root, context), page);
  assert.equal(root.count('click'), 1); assert.equal(reads.length, 1);
  for (let i = 0; i < initialUrls.length; i++) {
    assert.equal(reads[i].url, initialUrls[i]); reads[i].resolve(responseFor(reads[i].url)); await settle();
  }
  await page.ready;
  assert.deepEqual(reads.map(read => read.url), initialUrls);
  assert.match(root.querySelector('#importRows').innerHTML, /accounts.csv/);
  root.querySelector('#since').value = '2026-08-08'; root.querySelector('#profileCode').value = 'synthetic_hkd';
  const snapshot = page.captureUiState(); assert.equal(snapshot.tab, 'balance'); assert.equal(snapshot.since, '2026-08-08');
  page.dispose(); assert.equal(root.count('click'), 0); assert.equal(win.count('pagehide'), 0);
  const restored = mount(root, {...context, restoreState: snapshot});
  for (let i = 5; i < 10; i++) {reads[i].resolve(responseFor(reads[i].url)); await settle();}
  await restored.ready;
  assert.equal(reads[8].url, '/api/bank-reconcile/unmatched-rows?since=2026-08-08&limit=500');
  assert.equal(root.querySelector('#since').value, '2026-08-08'); assert.equal(root.querySelector('#profileCode').value, 'synthetic_hkd');
  restored.dispose();
  const late = mount(root, context); late.dispose(); root.querySelector('#err').textContent = 'preserve';
  reads[10].reject(new Error('late config failure')); await late.ready;
  assert.equal(reads.length, 11); assert.equal(root.querySelector('#err').textContent, 'preserve');
}
const {createActions} = await load('features/accounts/actions.js');
{
  let alive = true; const events = [], writes = [];
  const view = {clearError: () => events.push('clear'), setMsg: text => events.push(text), renderTransferMatches: value => events.push(value)};
  const actions = createActions({isActive: () => alive, view,
    post: (url, body) => {const item = deferred(); writes.push({url, body, ...item}); return item.promise;},
    acceptOverview: value => events.push(value), loadUnmatchedRows: async () => events.push('unmatched'), loadImports: async () => events.push('imports'), loadAccountOverview: async () => events.push('overview'),
  });
  const review = actions.reviewAccount('fixture/cny', 'approve');
  assert.equal(writes[0].url, '/api/accounts/fixture%2Fcny/review');
  assert.equal(writes[0].body.action, 'approve'); assert.equal(writes[0].body.note, '账户核准中心确认当前余额');
  writes[0].resolve(overview); await review; assert.equal(events.at(-1), '确认核准完成：fixture/cny');
  const defer = actions.reviewAccount('fixture/hkd', 'defer_missing');
  assert.equal(writes[1].body.note, '账户核准中心暂缓当前缺数据项'); writes[1].resolve(overview); await defer;
  const matching = actions.runTransferMatching(); assert.equal(writes[2].url, '/api/bank-reconcile/transfer-matches/run');
  assert.equal(Object.keys(writes[2].body).length, 0); writes[2].resolve(matches); await matching;
  assert.deepEqual(events.slice(-4, -1), ['unmatched', 'imports', 'overview']);
  assert.equal(events.at(-1), '匹配完成：1 条关系。内部划转不会作为生活消费统计；支付通道映射用于去重。');
  const late = actions.runTransferMatching(); const length = events.length; alive = false;
  writes[3].resolve(matches); await late; assert.equal(events.length, length);
  await actions.runTransferMatching(); assert.equal(writes.length, 4);
}
console.log('Accounts modules: all displayed field fixtures, currencies/missing values, root isolation, startup order, restore input state, disposal and exact write payloads passed.');
