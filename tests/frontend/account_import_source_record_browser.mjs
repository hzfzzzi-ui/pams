import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
const {chromium}=await import(pathToFileURL(process.env.PAMS_FRONTEND_PLAYWRIGHT_MODULE).href);
const browser=await chromium.launch({headless:true,channel:process.env.PAMS_FRONTEND_PLAYWRIGHT_CHANNEL||undefined});
const page=await browser.newPage({viewport:{width:390,height:844}});
const root=path.resolve('frontend/public'), calls=[], errors=[];
let source=null, kind='cash', publishes=0, previewUnavailable=true, ocrAvailable=true;
let mixed = null;
page.on('pageerror',e=>errors.push(e.message));
await page.route('**/*',async route=>{
 const req=route.request(),url=new URL(req.url()),p=url.pathname;
 assert.equal(url.origin,'http://import.invalid');
 const send=(data,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
 if(p.startsWith('/api/')) {
  calls.push([req.method(),p]);
  if(p.endsWith('/capabilities')) return send({schema_ready:false,source_record_schema_ready:true,source_record_publish_enabled:true,source_record_ocr_available:ocrAvailable,limits:{files:10,file_bytes:10485760}});
  if(p.endsWith('/source-records/context')) return send({accounts:[
   {account_code:'bank_other',label:'合成另一张卡',group_code:'group',group_label:'合成银行',kinds:['cash']},
   {account_code:'bank',label:'旧名称',selection_label:'合成 4209 储蓄卡',group_code:'group',group_label:'合成银行',currency:'CNY',default_asset_type:'cash',kinds:['cash','trade','holding'],trade_actions:{cash:['income'],stock:['buy']}},
   {account_code:'tencent_licaitong',label:'腾讯理财通',group_code:'tencent',group_label:'腾讯',kinds:['trade','holding'],import_modes:['ocr']},
   ...(mixed ? [{account_code:'tencent_licaitong_current_plus',label:'理财通活期+',kinds:['cash'],import_modes:['ocr']}] : []),
  ],counter_accounts:[]});
  if (mixed) {
   const account=url.searchParams.get('account_code'), filename=url.searchParams.get('filename');
   if (p.endsWith('/detect')) {
    assert.equal(url.searchParams.get('auto_account'),'true');
    if (filename==='unknown.png') return send({kind:null,account_code:null,status:'unknown'});
    const cash=filename==='cash.png';
    return send({kind:cash?'cash':'trade',account_code:cash?'tencent_licaitong_current_plus':'tencent_licaitong',status:'detected'});
   }
   if (p.endsWith('/source-records') && req.method()==='GET') return send({sources:mixed.filter(item=>item.account_code===account)});
   if (p.endsWith('/source-records') && req.method()==='POST') {
    const cash=filename==='cash.png';
    assert.equal(account,cash?'tencent_licaitong_current_plus':'tencent_licaitong');
    assert.equal(url.searchParams.get('document_type'),cash?'cash':'trade');
    const item={source_id:cash?'cash-draft':'fund-draft',account_code:account,document_type:cash?'cash':'trade',acquisition_method:'screenshot',
      status:'draft',revision:1,filename,file_format:'png',items:[]};
    mixed.push(item);
    return send({source_id:item.source_id,revision:1});
   }
   if (p.includes('/source-records/') && req.method()==='GET') return send(mixed.find(item=>p.endsWith('/'+item.source_id)));
  }
  if(p.endsWith('/source-records') && req.method()==='GET') {
   const account=url.searchParams.get('account_code');
   if (account) assert.equal(account,'tencent_licaitong');
   return send({sources:source?[source]:[]});
  }
  if(p.endsWith('/source-records/s/original')) return route.fulfill({contentType:'image/png',body:Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==','base64')});
  if(p.endsWith('/detect')) return send({kind:null,status:'unknown'});
  if(p.endsWith('/source-records') && req.method()==='POST') {
   if(url.searchParams.get('account_code')==='tencent_licaitong') {
    assert.equal(url.searchParams.get('document_type'),'trade');
    source={source_id:'s',account_code:'tencent_licaitong',document_type:'trade',acquisition_method:'screenshot',
     status:'draft',revision:1,filename:'synthetic.png',file_format:'png',items:[]};
    return send({source_id:'s',revision:1});
   }
   assert.equal(url.searchParams.get('account_code'),'bank');
   const fields=kind==='cash'?{business_date:'2026-01-02',action:'income',currency:'CNY',net_cash:'12.34'}:{business_date:'2026-01-02',action:'buy',currency:'CNY',symbol:'000001',name:'合成股票',quantity:'100',price:'10',gross_amount:'1000',net_cash:'-1000'};
   if(kind==='holding') Object.assign(fields,{asset_type:'cash',action:'balance',net_cash:'12.34'});
   source={source_id:'s',account_code:'bank',document_type:kind,acquisition_method:'file',status:'draft',revision:2,filename:'synthetic.csv',file_format:'csv',items:[{source_item_id:'i',parse_method:'template',parsed_fields:fields,review_status:'pending',raw_fields:{source:{row:1}},source_position:{row:1}}]};
   return send({source_id:'s',revision:2});
  }
  if(p.endsWith('/source-records/s') && req.method()==='GET') return send(source);
  if(p.endsWith('/source-records/s/reviews')) {const body=req.postDataJSON();assert.equal(body.revision,source.revision);source.items[0].parsed_fields=body.items[0].parsed_fields;source.items[0].review_status=body.items[0].review_status;source.revision++;return send({revision:source.revision});}
  if(p.endsWith('/cash-preview')) {
   if(previewUnavailable) { previewUnavailable=false; return send({detail:{message:'合成预检暂不可用'}},503); }
   const ready=source.items[0].review_status==='confirmed';return send({revision:source.revision,counts:{ready:ready?1:0,pending:ready?0:1,duplicate:0,invalid:0},rows:[{source_item_id:'i',status:ready?'ready':'pending'}],can_publish:ready,can_publish_ready:false,preview_token:ready?'a'.repeat(64):null});
  }
  if(p.endsWith('/trade-preview')) return send({revision:source.revision,items:[{source_item_id:'i'}],rows:[{source_item_id:'i',status:'ready'}],can_publish:true,preview_token:'b'.repeat(64)});
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
  source=null;await page.goto('http://import.invalid/account_import.html'+(kind==='cash'?'':'?account=bank'));
  await page.waitForFunction(()=>!document.querySelector('#workspace').hidden&&!document.querySelector('#workspace').inert);
  const picker=page.getByLabel('子账户',{exact:true});
  if(kind==='cash') {
   assert.deepEqual(await picker.locator('optgroup[label="合成银行"] option').allTextContents(),['合成另一张卡','合成 4209 储蓄卡']);
   await picker.selectOption({label:'合成 4209 储蓄卡'});
  }
  assert.equal(await picker.inputValue(),'bank');
  const uploadStart=calls.length;
  assert.equal(await page.locator('#save-draft').isVisible(),false);
  await page.locator('#files').setInputFiles({name:'synthetic.csv',mimeType:'text/csv',buffer:Buffer.from('synthetic')});
  if(kind==='cash') {
   await page.getByText('合成预检暂不可用',{exact:true}).waitFor();
   assert.equal(await page.locator('#intake').isVisible(),true);
   await page.locator('#next').click();
  }
  await page.locator('#review').waitFor({state:'visible'});
  assert.equal(source.status,'draft');
  assert.ok(calls.slice(uploadStart).every(([,p])=>!p.endsWith('/reviews')&&!p.endsWith('-publish')));
  assert.equal(calls.slice(uploadStart).filter(([method,p])=>method==='POST'&&p.endsWith('/source-records')).length,1);
  if(kind==='holding') {
   await page.locator('#back').click();
   await page.locator('#intake').waitFor({state:'visible'});
   await page.locator('#completeness').selectOption('complete');
   await page.locator('#holding-scope-date').fill('2026-01-02');
   await page.locator('#holding-scope-count').fill('1');
   await page.locator('#holding-scope-cash').fill('12.34');
   await page.locator('#holding-scope-note').fill('合成范围');
   await page.locator('#holding-scope-confirm').check();
   await page.locator('#next').click();await page.locator('#review').waitFor({state:'visible'});
  }
  assert.equal(await page.locator('#publish').isDisabled(),true);
  await page.locator('#preview-rows button').first().click();await page.locator('#editor').waitFor({state:'visible'});
  await page.locator('#record-form [type=submit]').click();await page.locator('#editor').waitFor({state:'hidden'});
  try { await page.waitForFunction(()=>!document.querySelector('#publish').disabled, null, {timeout:8000}); }
  catch (error) { throw new Error(`${kind}: ${await page.locator('#notice').textContent()}; ${JSON.stringify(calls.slice(-8))}`, {cause:error}); }
  await page.locator('#publish').click();await page.waitForFunction(()=>document.querySelector('#notice').textContent.includes('已发布'));
  assert.equal(source.status,'published');
 }
 const scoped='http://import.invalid/account_import.html?account=tencent_licaitong&scope=account&mode=manual';
 for (ocrAvailable of [true,false]) {
  const before=calls.length;
  await page.goto(scoped);
  await page.waitForFunction(()=>!document.querySelector('#workspace').hidden&&!document.querySelector('#workspace').inert);
  assert.equal(await page.locator('#account').inputValue(),'tencent_licaitong');
  assert.equal(await page.locator('#account').isDisabled(),true);
  assert.equal(await page.locator('#account option').count(),1);
  assert.equal(await page.locator('#mode-upload').isDisabled(),true);
  assert.equal(await page.locator('#mode-manual').isDisabled(),true);
  assert.equal(await page.locator('#mode-ocr').isEnabled(),ocrAvailable);
  assert.equal(await page.locator('#screenshot-area').isVisible(),ocrAvailable);
  assert.equal(await page.locator('#batch-list .import-history-row').count(),0);
  assert.ok(calls.slice(before).every(([method])=>method==='GET'));
 }
 ocrAvailable=true; source=null;
 await page.goto('http://import.invalid/account_import.html?account=tencent_licaitong&scope=account');
 await page.waitForFunction(()=>!document.querySelector('#workspace').hidden&&!document.querySelector('#workspace').inert);
 const beforeScreenshot=calls.length;
 await page.locator('#images').setInputFiles({name:'synthetic.png',mimeType:'image/png',buffer:Buffer.from('synthetic screenshot')});
 await page.locator('#kind-prompt').waitFor({state:'visible'});
 assert.equal(source,null);
 await page.locator('#kind').selectOption('trade');
 await page.getByText('文件已暂存，请核对识别结果。',{exact:true}).waitFor();
 assert.equal(source.status,'draft');
 assert.equal(source.acquisition_method,'screenshot');
 assert.equal(await page.locator('#account').inputValue(),'tencent_licaitong');
 assert.equal(await page.locator('#kind-prompt').isVisible(),false);
 await page.locator('#next').click();
 await page.locator('#image-review').waitFor({state:'visible'});
 await page.getByText('本图未识别出记录，请对照原图补充；空图不能确认。',{exact:true}).waitFor();
 await page.locator('#confirm-image').click();
 await page.waitForFunction(()=>!document.querySelector('#workspace').inert);
 assert.equal(await page.locator('#image-review').isVisible(),true);
 assert.equal(calls.slice(beforeScreenshot).filter(([method,p])=>method==='POST'&&p.endsWith('/source-records')).length,1);
 assert.ok(calls.slice(beforeScreenshot).every(([,p])=>!p.endsWith('-publish')));
 mixed=[];
 await page.goto(scoped);
 await page.waitForFunction(()=>!document.querySelector('#workspace').hidden&&!document.querySelector('#workspace').inert);
 const mixedStart=calls.length;
 await page.locator('#images').setInputFiles(['cash','unknown','fund'].map(name=>({name:name+'.png',mimeType:'image/png',buffer:Buffer.from('synthetic')})));
 try { await page.getByText(/已暂存 2 份.*未暂存 1 份.*unknown\.png/).waitFor({timeout:8000}); }
 catch (error) { throw new Error(`mixed: ${await page.locator('#notice').textContent()}; ${JSON.stringify(calls.slice(mixedStart))}`, {cause:error}); }
 assert.equal(mixed.length,2);
 assert.equal(await page.locator('#batch-list .import-history-row').count(),2);
 await page.getByRole('button',{name:/打开批次：理财通活期/}).click();
 await page.waitForFunction(()=>document.querySelector('#account').value==='tencent_licaitong_current_plus');
 assert.ok(calls.slice(mixedStart).every(([,p])=>!p.endsWith('-publish')));
 assert.equal(publishes,3);assert.deepEqual(errors,[]);
 assert.ok(calls.every(([,p])=>!p.includes('/batches')));
} finally {await browser.close();}
