import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
const {chromium}=await import(pathToFileURL(process.env.PAMS_FRONTEND_PLAYWRIGHT_MODULE).href);
const browser=await chromium.launch({headless:true});
const page=await browser.newPage({viewport:{width:390,height:844}});
const root=path.resolve('frontend/public'), calls=[], errors=[];
let source=null, kind='cash', publishes=0;
page.on('pageerror',e=>errors.push(e.message));
await page.route('**/*',async route=>{
 const req=route.request(),url=new URL(req.url()),p=url.pathname;
 assert.equal(url.origin,'http://import.invalid');
 const send=(data,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
 if(p.startsWith('/api/')) {
  calls.push([req.method(),p]);
  if(p.endsWith('/capabilities')) return send({schema_ready:false,source_record_schema_ready:true,source_record_publish_enabled:true,limits:{files:10,file_bytes:10485760}});
  if(p.endsWith('/source-records/context')) return send({accounts:[{account_code:'bank',label:'合成账户',currency:'CNY',default_asset_type:'cash',kinds:['cash','trade','holding'],trade_actions:{cash:['income'],stock:['buy']}}],counter_accounts:[]});
  if(p.endsWith('/source-records') && req.method()==='GET') return send({sources:source?[source]:[]});
  if(p.endsWith('/source-records') && req.method()==='POST') {
   const fields=kind==='cash'?{business_date:'2026-01-02',action:'income',currency:'CNY',net_cash:'12.34'}:{business_date:'2026-01-02',action:'buy',currency:'CNY',symbol:'000001',name:'合成股票',quantity:'100',price:'10',gross_amount:'1000',net_cash:'-1000'};
   if(kind==='holding') Object.assign(fields,{asset_type:'cash',action:'balance',net_cash:'12.34'});
   source={source_id:'s',account_code:'bank',document_type:kind,acquisition_method:'file',status:'draft',revision:2,filename:'synthetic.csv',file_format:'csv',items:[{source_item_id:'i',parse_method:'template',parsed_fields:fields,review_status:'pending',raw_fields:{source:{row:1}},source_position:{row:1}}]};
   return send({source_id:'s',revision:2});
  }
  if(p.endsWith('/source-records/s') && req.method()==='GET') return send(source);
  if(p.endsWith('/source-records/s/reviews')) {const body=req.postDataJSON();assert.equal(body.revision,source.revision);source.items[0].parsed_fields=body.items[0].parsed_fields;source.items[0].review_status=body.items[0].review_status;source.revision++;return send({revision:source.revision});}
  if(p.endsWith('/cash-preview')) {const ready=source.items[0].review_status==='confirmed';return send({revision:source.revision,counts:{ready:ready?1:0,pending:ready?0:1,duplicate:0,invalid:0},rows:[{source_item_id:'i',status:ready?'ready':'pending'}],can_publish:ready,can_publish_ready:false,preview_token:ready?'a'.repeat(64):null});}
  if(p.endsWith('/trade-preview')) return send({revision:source.revision,items:[{source_item_id:'i'}],preview_token:'b'.repeat(64)});
  if(p.endsWith('/holding-preview')) {
   assert.deepEqual(req.postDataJSON().scope,{revision:source.revision,as_of:'2026-01-02',member_count:1,cash_amount:'12.34',note:'合成范围',confirmed:true});
   return send({revision:source.revision,holdings:[{source_item_id:'i'}],preview_token:'c'.repeat(64)});
  }
  if(p.endsWith('/holding-publish')) assert.equal(req.postDataJSON().scope.note,'合成范围');
  if(p.endsWith('/cash-publish')||p.endsWith('/trade-publish')||p.endsWith('/holding-publish')) {assert.equal(req.postDataJSON().revision,source.revision);assert.equal(source.items[0].review_status,'confirmed');source.status='published';publishes++;return send({published:1});}
  throw new Error('Unexpected API '+req.method()+' '+p);
 }
 const file=path.resolve(root,'.'+p);assert.ok(file.startsWith(root+path.sep));
 try{return await route.fulfill({contentType:file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'text/html',body:await readFile(file)});}catch{return route.abort();}
});
try {
 for(kind of ['cash','trade','holding']) {
  source=null;await page.goto('http://import.invalid/account_import.html');
  await page.waitForFunction(()=>!document.querySelector('#workspace').hidden&&!document.querySelector('#workspace').inert);
  await page.locator('#files').setInputFiles({name:'synthetic.csv',mimeType:'text/csv',buffer:Buffer.from('synthetic')});
  await page.waitForFunction(()=>document.querySelector('#batch-meta').textContent.includes('草稿'));
  if(kind==='holding') {
   await page.locator('#completeness').selectOption('complete');
   await page.locator('#holding-scope-date').fill('2026-01-02');
   await page.locator('#holding-scope-count').fill('1');
   await page.locator('#holding-scope-cash').fill('12.34');
   await page.locator('#holding-scope-note').fill('合成范围');
   await page.locator('#holding-scope-confirm').check();
  }
  await page.locator('#next').click();await page.locator('#review').waitFor({state:'visible'});
  assert.equal(await page.locator('#publish').isDisabled(),true);
  await page.locator('#preview-rows button').first().click();await page.locator('#editor').waitFor({state:'visible'});
  await page.locator('#record-form [type=submit]').click();await page.locator('#editor').waitFor({state:'hidden'});
  await page.waitForFunction(()=>!document.querySelector('#publish').disabled);
  await page.locator('#publish').click();await page.waitForFunction(()=>document.querySelector('#notice').textContent.includes('已发布'));
  assert.equal(source.status,'published');
 }
 assert.equal(publishes,3);assert.deepEqual(errors,[]);
 assert.ok(calls.every(([,p])=>!p.includes('/batches')));
} finally {await browser.close();}
