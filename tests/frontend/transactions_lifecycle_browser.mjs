import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import * as fixtures from './fixtures/transactions_lifecycle.mjs';

const {chromium} = await import(pathToFileURL(process.env.PAMS_FRONTEND_PLAYWRIGHT_MODULE).href);
const publicRoot = path.resolve(process.argv[2]);
const markup = `<div id="err" style="display:none"></div><button id="btnReload">刷新</button><button id="btnReset">重置</button><button id="btnCreateTrade">手动录入</button>
<div id="manualTradeEntry" hidden><select id="tradeAccount"></select><input type="date" id="tradeDate"><input id="tradeName"><datalist id="tradeNameList"></datalist></div>
<span id="kpiCount"></span><span id="kpiBuy"></span><span id="kpiSell"></span><span id="kpiFees"></span><span id="ledgerMeta"></span><div id="transactionSummaryPanel"></div>
<div id="trade-attribution-analysis" hidden><div id="tradeAttributionPanel"></div></div><button data-trade-review="outside-list">查看合成额外交易</button>`;
const style = `<style>[hidden]{display:none!important}button,input,select{min-height:36px}section{border-bottom:1px solid #777}.tradeReviewTag{display:inline-flex;gap:6px;margin:4px}.tradeReviewFieldGrid{display:grid;gap:8px}.tradeReviewField{display:grid}.appBottomSheet{position:fixed;inset:0;z-index:10}.appBottomSheetScrim{position:absolute;inset:0;background:#0008}.appBottomSheetPanel{position:absolute;inset:8% 0 0;background:#222;color:white;padding:12px;overflow:auto}.appBottomSheetBody{padding-bottom:20px}body.sheet-open{overflow:hidden}.tradeReviewAssetFloatLayer{position:fixed;inset:0;z-index:9;background:#222;color:white;overflow:auto}.tradeReviewAssetFloatScrim{display:none}.tradeReviewAssetTile{position:relative!important;min-width:130px;min-height:100px}.tradeReviewSaveBtn{display:block}</style>`;
const data = JSON.stringify({options: fixtures.options, ledger: fixtures.ledger, attribution: fixtures.attribution, campaigns: fixtures.campaigns});
const browser = await chromium.launch({headless: true, channel: process.env.PAMS_FRONTEND_PLAYWRIGHT_CHANNEL || undefined});
const page = await browser.newPage({viewport: {width: 430, height: 900}});
const errors = [], unexpected = [], entryReads = [];
let entryMode = false;
page.on('pageerror', error => errors.push(error.message));
await page.route('**/*', async route => {
  const request = route.request(), url = new URL(request.url());
  if (url.origin !== 'http://transactions-fixture.invalid' || request.method() !== 'GET') {unexpected.push(`${request.method()} ${url.href}`); return route.abort();}
  if (url.pathname.startsWith('/api/')) {
    const payloads = {'/api/investment-transactions/options': fixtures.options, '/api/investment-transactions': fixtures.ledger,
      '/api/investment-transactions/attribution/summary': fixtures.attribution, '/api/investment-transactions/campaigns': fixtures.campaigns};
    if (!entryMode || !payloads[url.pathname]) {unexpected.push(url.href); return route.abort();}
    entryReads.push(url.pathname+url.search); return route.fulfill({json: payloads[url.pathname]});
  }
  if (url.pathname === '/entry.html') return route.fulfill({contentType: 'text/html', body: `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>固定交易入口测试</title>${style}<body>${markup}<script type="module" src="/assets/js/transactions.js?v=20261031-transactions-v1"></script></body></html>`});
  if (url.pathname === '/') return route.fulfill({contentType: 'text/html', body: `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>固定交易生命周期测试</title>${style}<body><h1>固定交易生命周期测试</h1><div id="roots"></div>
<script type="module">
import {mount} from '/assets/js/features/transactions/controller.js';
import {openBottomSheet, closeBottomSheet} from '/assets/js/ui.js';
window.fixtures = ${data}; window.markup = ${JSON.stringify(markup)};
window.reads = []; window.details = []; window.writes = []; window.destinations = []; window.pending = []; window.hold = false;
window.get = (url, options) => {
  reads.push({url, options});
  if(hold) return new Promise((resolve,reject) => pending.push({resolve,reject}));
  if(/\\/[^/]+\\/attribution$/.test(url)) return new Promise((resolve,reject) => details.push({url,resolve,reject}));
  if(url.includes('/options')) return Promise.resolve(fixtures.options);
  if(url.includes('/attribution/summary')) return Promise.resolve(fixtures.attribution);
  if(url.includes('/campaigns')) return Promise.resolve(fixtures.campaigns);
  return Promise.resolve(fixtures.ledger);
};
window.makeRoot = id => {
  const root = document.createElement('section'); root.id = id; root.innerHTML = markup;
  const add = root.addEventListener.bind(root), remove = root.removeEventListener.bind(root); root.listeners = new Map();
  root.addEventListener = (name,listener,opts) => {if(!root.listeners.has(name))root.listeners.set(name,new Set());root.listeners.get(name).add(listener);add(name,listener,opts);};
  root.removeEventListener = (name,listener,opts) => {root.listeners.get(name)?.delete(listener);remove(name,listener,opts);};
  document.getElementById('roots').append(root);return root;
};
window.context = {get, post: (url,payload) => new Promise((resolve,reject)=>writes.push({url,payload,resolve,reject})), openSheet:openBottomSheet, closeSheet:closeBottomSheet, navigate:url=>destinations.push(url)};
window.mountTransactions = mount; window.rootA=makeRoot('a');window.rootB=makeRoot('b');window.transactions=mount(rootA,context);window.loaded=true;
</script></body></html>`});
  const file = path.resolve(publicRoot, `.${url.pathname}`);
  assert.ok(file.startsWith(publicRoot+path.sep));
  return route.fulfill({contentType: 'text/javascript', body: await readFile(file)});
});
try {
  await page.goto('http://transactions-fixture.invalid/'); await page.waitForFunction(()=>window.loaded); await page.evaluate(()=>transactions.ready);
  assert.equal(await page.locator('#a .tradeLedgerItem').count(),10);
  assert.equal(await page.locator('#b .tradeLedgerItem').count(),0);
  assert.equal(await page.evaluate(()=>mountTransactions(rootA,context)===transactions),true);
  assert.equal(await page.evaluate(()=>rootA.listeners.get('click').size),1);
  assert.equal(await page.evaluate(()=>reads.filter(item=>item.url.includes('/options')).length),1);
  await page.locator('#b #btnCreateTrade').click(); assert.equal(await page.evaluate(()=>destinations.length),0);
  await page.locator('#a #btnToggleAllTrades').click(); assert.equal(await page.locator('#a .tradeLedgerItem').count(),12);
  assert.match(await page.locator('#a #transactionSummaryPanel').textContent(),/买入/); assert.match(await page.locator('#a #transactionSummaryPanel').textContent(),/卖出/);
  await page.locator('#a #btnOpenTradeForm').click(); assert.deepEqual(await page.evaluate(()=>destinations),['account_import.html?kind=trade&mode=manual']);
  assert.equal(await page.locator('#a #manualTradeEntry').evaluate(el=>el.hidden),true);
  await page.locator('#a #btnToggleTradeFilters').click();
  await page.locator('#filterSheetStart').fill('2026-01-01'); await page.locator('#filterSheetSide').selectOption('sell'); await page.locator('#filterSheetSearch').fill('合成筛选');
  await page.locator('#tradeFilterForm button[type="submit"]').click();
  await page.waitForFunction(()=>reads.some(item=>item.url.includes('side=sell')&&item.url.includes('search=')));
  assert.equal(await page.locator('#appBottomSheet').count(),0); assert.equal(await page.locator('#a .tradeLedgerItem').count(),10);

  await page.evaluate(()=>{rootA.querySelector('#trade-attribution-analysis').hidden=false;window.dispatchEvent(new Event('pams:load-trade-attribution'));});
  await page.locator('#a [data-asset-heat-select="fixture-a"]').waitFor();
  assert.equal(await page.locator('#a [data-asset-heat-select]').count(),2);
  await page.locator('#a [data-asset-heat-select="fixture-a"]').click();
  assert.match(await page.locator('#a [data-asset-heat-detail="fixture-a"]').textContent(),/合成资产 A/);
  assert.match(await page.locator('#a [data-asset-heat-detail="fixture-a"]').textContent(),/50/);
  await page.locator('#a .tradeReviewAssetFloatClose').click();
  assert.equal(await page.locator('#a [data-asset-heat-float]').evaluate(el=>el.hidden),true);
  await page.getByText('高影响动作',{exact:true}).click();
  await page.locator('#a [data-direction-filter-toggle]').click(); await page.locator('#a [data-direction-filter-value="sell"]').click();
  assert.equal(await page.locator('#a [data-direction-side="buy"]').evaluate(el=>el.hidden),true);
  assert.equal(await page.locator('#a [data-direction-side="sell"]').evaluate(el=>el.hidden),false);

  await page.locator('#a .tradeLedgerItem [data-trade-review="1"]').click(); await page.waitForFunction(()=>details.length===1);
  await page.keyboard.press('Escape');
  await page.locator('#a .tradeLedgerItem [data-trade-review="2"]').click(); await page.waitForFunction(()=>details.length===2);
  await page.evaluate(payload=>details[1].resolve(payload),fixtures.tradeAttribution(2,'当前 B 结论'));
  await page.getByText('当前 B 结论',{exact:true}).waitFor();
  await page.evaluate(payload=>details[0].resolve(payload),fixtures.tradeAttribution(1,'过期 A 结论'));
  await page.waitForTimeout(20);
  assert.equal(await page.locator('#tradeReviewForm').getAttribute('data-trade-id'),'2');
  assert.equal(await page.getByText('过期 A 结论',{exact:true}).count(),0);
  await page.keyboard.press('Escape');
  await page.locator('#a > [data-trade-review="outside-list"]').click(); await page.waitForFunction(()=>details.length===3);
  await page.locator('#a .tradeLedgerItem [data-trade-review="1"]').click(); await page.waitForFunction(()=>details.length===4);
  await page.evaluate(payload=>details[2].resolve(payload),fixtures.tradeAttribution('outside-list','过期额外交易'));
  await page.waitForTimeout(20); assert.equal(await page.locator('#tradeReviewForm').getAttribute('data-trade-id'),'1');
  await page.evaluate(payload=>details[3].resolve(payload),fixtures.tradeAttribution(1,'当前 A 结论'));

  await page.locator('input[name="reviewCurrentIdentity"][value="value"]').check();
  await page.locator('input[name="reviewHoldingPeriod"][value="6-12个月"]').check();
  await page.locator('#reviewNote').fill('合成浏览器备注');
  await page.locator('#reviewPlanDetails > summary').click();
  await page.locator('#reviewMaxLossPct').fill('5.5'); await page.locator('#reviewNextReviewDate').fill('2026-12-01'); await page.locator('#reviewIdentityConfidence').selectOption('high');
  for(const [id,text] of Object.entries({reviewBuyLogic:'合成逻辑',reviewAddCondition:'合成加仓',reviewReduceCondition:'合成减仓',reviewSellCondition:'合成退出'})) await page.locator('#'+id).fill(text);
  await page.locator('#reviewTagDetails > summary').click(); await page.locator('.tradeReviewMoreTags > summary').click();
  await page.locator('input[name="reviewTag"][value="disciplined"]').check(); await page.locator('input[name="reviewTag"][value="research_complete"]').check();
  await page.locator('#reviewTransitionType').selectOption('research_update'); await page.locator('input[name="identityTransitionTrigger"][value="new_evidence"]').check(); await page.locator('#reviewTransitionEvidence').fill('合成新证据');
  await page.locator('#tradeReviewForm').evaluate(form=>{form.requestSubmit();form.requestSubmit();}); await page.waitForFunction(()=>writes.length===1);
  const first=await page.evaluate(()=>({url:writes[0].url,payload:writes[0].payload}));
  assert.equal(first.url,'/api/investment-transactions/1/review');
  assert.deepEqual(first.payload,{buy_reason:'research',sell_reason:null,review_note:'合成浏览器备注',review_tags:['disciplined','research_complete'],initial_position_identity:'allocation',current_position_identity:'value',identity_confidence:'high',intended_holding_period:'6-12个月',buy_logic:'合成逻辑',add_condition:'合成加仓',reduce_condition:'合成减仓',sell_condition:'合成退出',max_acceptable_loss_pct:5.5,max_position_pct:12,next_review_date:'2026-12-01',sell_rule_type:'value_thesis',identity_transition_type:'research_update',identity_transition_triggers:['new_evidence'],identity_transition_evidence:'合成新证据'});
  await page.evaluate(()=>writes[0].reject(new Error('409 合成冲突')));
  await page.waitForFunction(()=>document.querySelector('#a #err').textContent.includes('409'));
  assert.equal(await page.locator('#a #err').isVisible(), true);
  assert.equal(await page.locator('#b #err').textContent(), '');
  assert.equal(await page.locator('#reviewNote').inputValue(),'合成浏览器备注');
  await page.waitForFunction(()=>!document.querySelector('#tradeReviewForm button[type="submit"]').disabled);
  await page.locator('#tradeReviewForm button[type="submit"]').click(); await page.waitForFunction(()=>writes.length===2);
  const readsBeforeLate=await page.evaluate(()=>reads.length);
  await page.keyboard.press('Escape'); await page.locator('#a .tradeLedgerItem [data-trade-review="2"]').click(); await page.waitForFunction(()=>details.length===5);
  await page.evaluate(()=>writes[1].resolve({success:true})); await page.waitForTimeout(20);
  assert.equal(await page.locator('#tradeReviewForm').getAttribute('data-trade-id'),'2');
  assert.equal(await page.evaluate(()=>reads.length),readsBeforeLate+1);
  await page.locator('#tradeReviewForm').evaluate(form=>{form.requestSubmit();form.requestSubmit();}); await page.waitForFunction(()=>writes.length===3);
  await page.evaluate(()=>writes[2].resolve({success:true})); await page.waitForFunction(()=>!document.getElementById('tradeReviewForm'));
  await page.waitForFunction(()=>reads.filter(item=>item.url.includes('/options')).length===2);
  assert.equal(await page.evaluate(()=>reads.at(-2).url),'/api/investment-transactions/options');
  assert.match(await page.evaluate(()=>reads.at(-1).url),/^\/api\/investment-transactions\?/);
  await page.evaluate(payload=>details[4].resolve(payload),fixtures.tradeAttribution(2,'已关闭 B 的旧结论'));
  await page.evaluate(()=>transactions.dispose());
  assert.equal(await page.evaluate(()=>[...rootA.listeners.values()].reduce((sum,set)=>sum+set.size,0)),0);
  const afterDispose=await page.evaluate(()=>reads.length);
  await page.evaluate(()=>window.dispatchEvent(new Event('pams:load-trade-attribution'))); await page.waitForTimeout(20);
  assert.equal(await page.evaluate(()=>reads.length),afterDispose);
  await page.evaluate(()=>{hold=true;window.late=mountTransactions(rootA,context);late.dispose();rootA.querySelector('#err').textContent='新页面';pending[0].reject(new Error('过期请求'));});
  await page.evaluate(()=>late.ready); assert.equal(await page.locator('#a #err').textContent(),'新页面');

  entryMode=true;
  await page.addInitScript(()=>Object.defineProperty(navigator,'connection',{configurable:true,value:{saveData:true}}));
  await page.goto('http://transactions-fixture.invalid/entry.html');
  await page.evaluate(async()=>{window.entry=await import('/assets/js/transactions.js?v=20261031-transactions-v1');await entry.mount().ready;});
  await page.locator('#btnToggleTradeFilters').click(); await page.locator('#filterSheetSearch').fill('已应用筛选'); await page.locator('#tradeFilterForm button[type="submit"]').click();
  await page.locator('#btnToggleAllTrades').click(); assert.equal(await page.locator('.tradeLedgerItem').count(),12);
  await page.locator('#btnToggleTradeFilters').click(); await page.locator('#filterSheetSearch').fill('未应用草稿'); await page.locator('#filterSheetStart').fill('2026-05-05');
  await page.evaluate(()=>{window.dispatchEvent(new PageTransitionEvent('pagehide',{persisted:true}));window.dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}));});
  await page.evaluate(()=>entry.mount().ready);
  assert.equal(await page.locator('.tradeLedgerItem').count(),12);
  assert.equal(await page.locator('#filterSheetSearch').inputValue(),'未应用草稿'); assert.equal(await page.locator('#filterSheetStart').inputValue(),'2026-05-05');
  assert.equal(await page.evaluate(()=>entry.mount().captureUiState().filters.search),'已应用筛选');
  assert.equal(await page.locator('#manualTradeEntry').evaluate(el=>el.hidden),true);
  assert.deepEqual(unexpected,[]); assert.deepEqual(errors,[]);
  console.log('Real browser transactions: 10/all rows, scoped filters/import navigation, asset detail/directions, old A/new B races, exact 19-field save, conflict retention, no stale save replay and persisted ordinary state restoration passed.');
}finally{await browser.close();}
