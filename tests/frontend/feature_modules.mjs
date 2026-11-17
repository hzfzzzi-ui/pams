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
  for (const id of ['moreNavGrowthExplanationPanel', 'moreNavGrowthDetails', 'moreOrigin']) root.add(`#${id}`);
  return root;
}
const more = await loadFeatureModule(publicRoot, 'features/more/controller.js');
{
  const root = moreRoot(), win = new Node(), reads = [], rendered = [];
  const get = (url, options) => { const item = deferred(); reads.push({url, options, ...item}); return item.promise; };
  const context = {get, win, location: {origin: 'http://fixture.invalid'}, renderExplanation: (payload, panel) => {rendered.push(payload); panel.innerHTML = 'NAV fixture';}};
  const page = more.mount(root, context);
  assert.equal(more.mount(root, context), page);
  await page.ready;
  assert.equal(reads.length, 0);
  assert.equal(root.querySelector('#moreOrigin').textContent, 'http://fixture.invalid');
  const details = root.querySelector('#moreNavGrowthDetails'); details.open = true;
  details.dispatch('toggle'); details.dispatch('toggle');
  assert.equal(reads.length, 1, 'Repeated toggle must share the pending read');
  assert.equal(reads[0].url, '/api/portfolio/equity-bridge?start=2026-01-01&baseCurrency=CNY');
  assert.equal(reads[0].options.cache, false);
  const panel = root.querySelector('#moreNavGrowthExplanationPanel');
  page.dispose(); panel.innerHTML = 'after dispose';
  reads[0].resolve({fixture: true}); await settle();
  assert.equal(panel.innerHTML, 'after dispose'); assert.equal(rendered.length, 0);
  assert.equal(details.count('toggle'), 0); assert.equal(win.count('pagehide'), 0);
  details.dispatch('toggle'); assert.equal(reads.length, 1);
  details.open = false;
  const fresh = more.mount(root, context);
  win.dispatch('pagehide'); assert.equal(fresh.disposed, true);
  assert.equal(win.count('pagehide'), 0);
}

// Legacy entry exports remain usable without automatic DOM boot in Node.
const entry = await loadFeatureModule(publicRoot, 'more.js');
for (const name of ['mount', 'dispose']) assert.equal(typeof entry[name], 'function', name);
console.log('Low-frequency feature modules: root isolation, lazy reads, disposal and remount passed.');
