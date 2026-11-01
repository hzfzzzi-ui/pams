// Offline browser contract: all data and financial writes are intercepted.
import assert from 'node:assert/strict';
import {readFile,mkdir} from 'node:fs/promises';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
const {chromium}=await import(pathToFileURL(process.env.PAMS_FRONTEND_PLAYWRIGHT_MODULE).href);
const browser=await chromium.launch({headless:true,channel:process.env.PAMS_FRONTEND_PLAYWRIGHT_CHANNEL||undefined});
const root=path.resolve('frontend/public'), output=process.env.PAMS_FRONTEND_ARTIFACT_DIR;
if(output) await mkdir(output,{recursive:true});
let batch,caps,publishes,calls,failPreview,uploads,holdPut,releasePut,holdCreate,releaseCreate,holdFile,releaseFile,holdPublish,releasePublish,publishOutcome,fileOutcome,createOutcome;
const defaults={authentication_required:false,schema_ready:true,publish_enabled:false,ocr_available:true,limits:{files:10,file_bytes:10485760}};
const fields=(kind='trade')=>({kind,business_date:'2026-10-20',asset_type:'stock',symbol:'600001',name:'合成标的',currency:'CNY',action:'buy',status:'confirmed',quantity:'1',price:'10.0100',gross_amount:'10.0100',fee_scope:'separate',fee_amount:'0',tax_amount:'0',net_cash:'-10.0100'});
const reset=()=>{batch=null;caps={...defaults};publishes=0;calls=[];uploads=[];failPreview=false;publishOutcome='success';fileOutcome='success';createOutcome='success';};
const accounts=[{account_code:'ths',profile:'tonghuashun',label:'同花顺',currency:'CNY',default_asset_type:'stock',kinds:['trade','holding'],trade_actions:{stock:['buy','sell','dividend','fee'],fund:['buy','sell','dividend','fee']}},
 {account_code:'licai',profile:'tencent_licaitong',label:'理财通',currency:'CNY',default_asset_type:'fund',kinds:['trade','holding'],trade_actions:{fund:['subscribe','redeem','dividend','fee']}},
 {account_code:'bank',profile:'statement',label:'银行账户',currency:'CNY',default_asset_type:'cash',kinds:['cash'],trade_actions:{cash:['income','expense','transfer','balance']}}];
const viewportWidth=Number(process.env.PAMS_FRONTEND_VIEWPORT_WIDTH || 390);
assert.ok([320,390,1280].includes(viewportWidth));
const context=await browser.newContext({viewport:{width:viewportWidth,height:844},hasTouch:true});
await context.addInitScript(()=>Object.defineProperty(navigator,'standalone',{value:true}));
const page=await context.newPage(), errors=[], reports=[];
page.on('pageerror',error=>errors.push(error.message));
await context.route('**/*',async route=>{
 const req=route.request(), url=new URL(req.url()), p=url.pathname;
 assert.equal(url.origin,'http://import.invalid');
 const send=(data,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
 if(p.startsWith('/api/')) {
  assert.ok(p.startsWith('/api/account-import/')); calls.push([req.method(),p]);
  if(p.endsWith('/capabilities')) return send(caps);
  if(p.endsWith('/context')) return send({accounts,counter_accounts:[{account_code:'bank',display_name:'银行账户'}]});
  if(p.endsWith('/assets')) return send({rows:[{asset_id:'a',symbol:'600001',name:'合成标的',asset_type:'stock',currency:'CNY'}]});
  if(p.endsWith('/batches') && req.method()==='GET') return send({rows:batch?[batch]:[]});
  if(p.endsWith('/batches') && req.method()==='POST') {if(holdCreate) await new Promise(resolve=>{releaseCreate=resolve;});
   if(createOutcome==='conflict') return send({detail:{code:'account_changed',message:'账户配置已变化'}},409);
   batch={batch_id:'b',revision:1,status:'draft',rows:[],files:[],...req.postDataJSON()};return send(batch);}
  if(p==='/api/account-import/batches/b' && req.method()==='GET') return send(batch);
  if(p==='/api/account-import/batches/b' && req.method()==='PUT') {
   if(holdPut) await new Promise(resolve=>{releasePut=resolve;});
   const body=req.postDataJSON(); assert.equal(body.revision,batch.revision);
   batch.rows=body.rows.map((row,index)=>{
    const old=batch.rows.find(item=>item.row_id===row.row_id), source=old?.source || (row.image_file_id?{origin:'ocr',file_id:row.image_file_id}:{origin:'manual'});
    if(old?.can_edit_fields===false) assert.deepEqual(row.fields,old.fields);
    return {...row,row_id:row.row_id||'r'+index,source,can_edit_fields:!source.file_id||source.origin==='ocr'};
   }); batch.revision++;return send(batch);
  }
  if(p.endsWith('/files') && req.method()==='POST') {if(holdFile) await new Promise(resolve=>{releaseFile=resolve;});
   if(fileOutcome==='unknown') return route.abort('connectionreset');
   const name=url.searchParams.get('filename'), image=/\.(png|jpe?g)$/i.test(name), id='f'+(batch.files.length+1);
   uploads.push(name); assert.ok(req.postDataBuffer().length);
   batch.files.push({file_id:id,filename:name,content_type:image?'image/png':'text/plain',metadata:{headers:image?[]:['日期','代码','净收付'],suggested_mapping:image?{}:{business_date:'日期',symbol:'代码',net_cash:'净收付'}}});
   const count=image?(caps.ocr_available?2:0):batch.kind==='cash'?2:1;
   for(let n=0;n<count;n++) batch.rows.push({row_id:id+'r'+n,fields:{...fields(batch.kind),name:`${name}记录${n+1}`,...(batch.kind==='cash'?{asset_type:'cash',action:n?'balance':'income',net_cash:'100.05'}:{})},source:{origin:image?'ocr':'file',file_id:id,raw_text:'合成原始记录',row:n+1},can_edit_fields:image,acknowledged:false,excluded:batch.kind==='cash'&&n===1});
   batch.revision++;return send(batch);
  }
  if(/\/files\/[^/]+$/.test(p) && req.method()==='GET') return route.fulfill({contentType:'image/png',body:process.env.PAMS_FRONTEND_TEST_IMAGE?await readFile(process.env.PAMS_FRONTEND_TEST_IMAGE):Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a6xkAAAAASUVORK5CYII=','base64')});
  if(p.endsWith('/review')) {const id=p.split('/').at(-2);assert.equal(req.postDataJSON().revision,batch.revision);const rows=batch.rows.filter(r=>r.source.file_id===id);if(!rows.length)return send({detail:{code:'image_empty',message:'空图不能确认'}},400);rows.forEach(r=>r.acknowledged=true);batch.revision++;return send(batch);}
  if(p.endsWith('/parse')) {const body=req.postDataJSON();assert.equal(body.kind,batch.kind);assert.deepEqual(body.mapping,{business_date:'日期',symbol:'代码',net_cash:'净收付'});batch.revision++;return send(batch);}
  if(p.endsWith('/preview')) {
   if(failPreview) return send({detail:{code:'source_changed',message:'来源资料已变化'}},409);
   assert.equal(req.postDataJSON().revision,batch.revision);
   const blocked=batch.rows.some(r=>r.source.origin==='ocr'&&!r.acknowledged&&!r.excluded);
   const rows=batch.rows.map(r=>({row_id:r.row_id,fields:r.fields,status:r.excluded?'excluded':blocked?'blocked':'ready',issues:[],before:null,after:r.fields}));
   return send({batch_id:'b',revision:batch.revision,preview_token:'p'.repeat(64),can_publish:!blocked,
    counts:{new:batch.rows.filter(r=>!r.excluded).length,duplicates:0,blocked:blocked?1:0,pending:0},rows,
    write_count:rows.filter(r=>r.status==='ready').length,write_plan:rows.filter(r=>r.status==='ready').map(r=>({row_id:r.row_id,table:batch.kind==='cash'?'pams_cashflow_fact':'pams_investment_trade_fact',operation:'insert',before:null,after:r.fields})),
    impact:{nav_estimate_status:'unavailable',nav_change:null,message:'测试不应展示'},
    warnings:['持仓、交易、现金独立核对；未上传部分不视为零。','资产底表待补充/核准：000001','已识别的券商入出金缺少银行对手账户证据','其他来源异常'],
    issue_groups:[{code:'counter_required',message:'券商入出金缺少银行对手账户证据',count:1}]});
  }
  if(p.endsWith('/publish')) {
   assert.equal(req.postDataJSON().revision,batch.revision);
   assert.equal(req.postDataJSON().preview_token,'p'.repeat(64));
   if(holdPublish) await new Promise(resolve=>{releasePublish=resolve;});
   if(publishOutcome==='unknown') return route.abort('connectionreset');
   if(publishOutcome==='conflict') return send({detail:{code:'revision_conflict',message:'服务器版本已更新'}},409);
   assert.equal(caps.publish_enabled,true);publishes++;batch.status='published';return send({batch});
  }
  throw new Error('Unexpected API '+req.method()+' '+p);
 }
 const file=path.resolve(root,'.'+p);assert.ok(file.startsWith(root+path.sep));
 try {await route.fulfill({contentType:file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'text/html',body:await readFile(file)});} catch {await route.abort();}
});
const ready=async()=>page.waitForFunction(()=>!document.getElementById('workspace').inert);
const open=async()=>{await page.goto('http://import.invalid/account_import.html');await page.waitForFunction(()=>!document.getElementById('workspace').hidden);await ready();};
const save=async()=>{await page.locator('#record-form [type=submit]').click();await page.locator('#editor').waitFor({state:'hidden'});await ready();};
const capture=async name=>{await page.evaluate(()=>window.scrollTo({top:0,left:0,behavior:'instant'}));await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(resolve)));assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);const head=await page.locator('.import-head').boundingBox();assert.ok(head.y>=0&&head.y<100,JSON.stringify({name,head,metrics:await page.evaluate(()=>({scroll:scrollY,height:innerHeight,doc:document.documentElement.scrollHeight,body:document.body.getBoundingClientRect().toJSON(),active:document.activeElement.id}))}));if(output)await page.screenshot({path:path.join(output,name+'.png'),fullPage:true});};
const controllerURL='/assets/js/features/account_import/controller.js?v=20261101-batch-groups';
const mounted=()=>page.evaluate(async url=>{const m=await import(url);return !!m.mount(document.body);},controllerURL);
const suspendRestore=()=>page.evaluate(()=>{
 window.dispatchEvent(new PageTransitionEvent('pagehide',{persisted:true}));
 window.dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}));
});
const fillManual=async()=>{
 await page.locator('#mode-manual').click();await page.locator('#add-row').click();
 for(const [key,value] of Object.entries({business_date:'2026-10-20',symbol:'600001',name:'合成标的',quantity:'12345678901234567890.0100',price:'10.0100',gross_amount:'10.0100',fee_amount:'0',net_cash:'-10.0100'})) await page.locator('#field-'+key).fill(value);
 await page.locator('#field-action').selectOption('buy');
};
const waitUntil=async(predicate,label)=>{
 const deadline=Date.now()+3000;
 while(!predicate()) {if(Date.now()>deadline) throw new Error('Timed out: '+label);await new Promise(resolve=>setTimeout(resolve,10));}
};
try {
 reset();await open();
 const beforeMount=calls.length;await mounted();await mounted();
 assert.equal(calls.length,beforeMount,'same root mount must be idempotent');
 await fillManual();await page.locator('#field-note').fill('原生离页取消保留');
 const beforeURL=page.url();let nativePrompt=0;
 page.once('dialog',async dialog=>{nativePrompt++;assert.equal(dialog.type(),'beforeunload');await dialog.dismiss();});
 await assert.rejects(page.goto('http://import.invalid/more.html'),/ERR_ABORTED|aborted|cancel/i);
 assert.equal(nativePrompt,1);assert.equal(page.url(),beforeURL);
 assert.equal(await page.locator('#field-note').inputValue(),'原生离页取消保留');
 const writesBeforeRestore=calls.filter(([method])=>method!=='GET').length;
 await suspendRestore();await page.locator('#editor').waitFor({state:'visible'});
 assert.equal(await page.locator('#field-note').inputValue(),'原生离页取消保留');
 assert.equal(await page.locator('#field-quantity').inputValue(),'12345678901234567890.0100');
 assert.equal(calls.filter(([method])=>method!=='GET').length,writesBeforeRestore);
 assert.equal(await page.evaluate(()=>history.state.step),1);
 const puts=calls.filter(([method])=>method==='PUT').length;
 await save();assert.equal(calls.filter(([method])=>method==='PUT').length,puts+1,'one handler after remount');
 assert.equal(batch.rows[0].fields.quantity,'12345678901234567890.0100');
 await page.locator('#next').click();await page.locator('#review').waitFor();await ready();
 assert.equal(new URL(page.url()).hash,'#review');
 const previewCalls=calls.filter(([,p])=>p.endsWith('/preview')).length;
 await suspendRestore();await page.locator('#review').waitFor();
 assert.equal(new URL(page.url()).hash,'#review');
 assert.equal(calls.filter(([,p])=>p.endsWith('/preview')).length,previewCalls);
 await page.goBack();await page.locator('#intake').waitFor();
 await page.goForward();await page.locator('#review').waitFor();
 assert.equal(calls.filter(([,p])=>p.endsWith('/preview')).length,previewCalls);

 // Mid-save exit retains local input but cannot claim the server rejected it.
 reset();await open();await fillManual();await page.locator('#field-note').fill('写入中本地输入');
 holdPut=true;await page.locator('#record-form [type=submit]').click();
 await waitUntil(()=>releasePut,'save request');
 const callsDuringSave=calls.filter(([method])=>method==='PUT').length;
 await suspendRestore();await page.locator('#pending-result').waitFor();
 assert.equal(await page.locator('#conflict').isVisible(),false,'pending outcome is not a fabricated 409');
 assert.equal(await page.locator('#publish').isDisabled(),true);
 page.once('dialog',dialog=>dialog.accept());await page.locator('#refresh-result').click();await ready();
 assert.equal(await page.locator('#pending-result').isVisible(),true,'early GET cannot unlock an unsettled PUT');
 assert.match(await page.locator('#notice').textContent(),/仍在处理中/);
 await page.getByRole('button',{name:'继续未保存的编辑',exact:true}).click();
 assert.equal(await page.locator('#field-note').inputValue(),'写入中本地输入');
 assert.equal(await page.locator('#record-form [type=submit]').isVisible(),false);
 await page.locator('#close-editor').click();
 holdPut=false;releasePut();releasePut=null;
 await page.waitForTimeout(50);
 assert.equal(calls.filter(([method])=>method==='PUT').length,callsDuringSave);
 assert.equal(await page.locator('#pending-result').isVisible(),true);
 page.once('dialog',dialog=>dialog.accept());await page.locator('#refresh-result').click();await ready();
 await page.locator('#pending-result').waitFor({state:'hidden'});
 assert.match(await page.locator('#batch-meta').textContent(),/版本 2/);
 assert.equal(calls.filter(([method])=>method==='PUT').length,callsDuringSave);
 assert.equal(batch.rows[0].fields.note,'写入中本地输入');

 // An accepted create may not have returned its ID before exit. Recovery reads
 // history and lets the user choose that batch; it never creates a replacement.
 reset();await open();await page.locator('#mode-manual').click();holdCreate=true;
 await page.locator('#add-row').click();
 await waitUntil(()=>releaseCreate,'create request');
 await suspendRestore();await page.locator('#pending-result').waitFor();
 holdCreate=false;releaseCreate();releaseCreate=null;await page.waitForTimeout(30);
 page.once('dialog',dialog=>dialog.accept());await page.locator('#refresh-result').click();await ready();
 assert.equal(await page.locator('#pending-result').isVisible(),true);
 assert.equal(calls.filter(([method,p])=>method==='POST'&&p.endsWith('/batches')).length,1);
 assert.equal(calls.some(([,p])=>p.includes('/undefined')||p.includes('/null')),false);
 await page.getByRole('button',{name:'打开批次',exact:true}).click();await ready();
 await page.locator('#pending-result').waitFor({state:'hidden'});

 // A rejected create has no batch ID to read. An explicit history refresh may
 // unlock a new user attempt after its definitive 409; it must not auto-create.
 reset();await open();await page.locator('#mode-manual').click();holdCreate=true;createOutcome='conflict';
 await page.locator('#add-row').click();await waitUntil(()=>releaseCreate,'rejected create');
 await suspendRestore();holdCreate=false;releaseCreate();releaseCreate=null;await page.waitForTimeout(30);
 page.once('dialog',dialog=>dialog.accept());await page.locator('#refresh-result').click();await ready();
 assert.equal(await page.locator('#pending-result').isVisible(),false);
 assert.equal(await page.locator('#add-row').isDisabled(),false);
 assert.match(await page.locator('#notice').textContent(),/新建请求已被服务器拒绝/);
 assert.equal(calls.filter(([method,p])=>method==='POST'&&p.endsWith('/batches')).length,1);

 // Two-file upload must stop after its already accepted first upload when the
 // page exits. Neither the first write nor the remaining file is replayed.
 reset();await open();holdFile=true;
 await page.locator('#files').setInputFiles([{name:'one.csv',mimeType:'text/csv',buffer:Buffer.from('one')},{name:'two.csv',mimeType:'text/csv',buffer:Buffer.from('two')}]);
 await waitUntil(()=>releaseFile,'file request');
 await suspendRestore();await page.locator('#pending-result').waitFor();
 holdFile=false;releaseFile();releaseFile=null;await page.waitForTimeout(30);
 assert.deepEqual(uploads,['one.csv']);
 assert.equal(calls.filter(([method,p])=>method==='POST'&&p.endsWith('/files')).length,1);
 page.once('dialog',dialog=>dialog.accept());await page.locator('#refresh-result').click();await ready();
 assert.equal(await page.locator('#file-list a').count(),1);

 // An unversioned upload with a lost response cannot be classified from a
 // later draft revision alone, because no revision guarded the original POST.
 reset();await open();holdFile=true;fileOutcome='unknown';
 await page.locator('#files').setInputFiles([{name:'unknown.csv',mimeType:'text/csv',buffer:Buffer.from('unknown')}]);
 await waitUntil(()=>releaseFile,'unknown file request');await suspendRestore();
 holdFile=false;releaseFile();releaseFile=null;await page.waitForTimeout(30);
 batch.revision++;
 page.once('dialog',dialog=>dialog.accept());await page.locator('#refresh-result').click();await ready();
 assert.equal(await page.locator('#pending-result').isVisible(),true);
 assert.match(await page.locator('#notice').textContent(),/尚未取得完成回执.*仅刷新草稿不足以确认/);
 assert.equal(await page.locator('#files').isDisabled(),true);
 assert.equal(calls.filter(([method,p])=>method==='POST'&&p.endsWith('/files')).length,1);
 batch.status='voided';
 page.once('dialog',dialog=>dialog.accept());await page.locator('#refresh-result').click();await ready();
 assert.equal(await page.locator('#pending-result').isVisible(),false);

 // A published record remains a read-only view after history restoration.
 reset();batch={batch_id:'b',revision:3,status:'published',account_code:'ths',kind:'trade',mode:'manual',completeness:'partial',rows:[{row_id:'r',fields:fields(),source:{origin:'manual'},acknowledged:true}],files:[]};
 await open();await page.locator('#history summary').click();await page.getByRole('button',{name:'打开批次',exact:true}).click();await ready();
 await page.getByRole('button',{name:'查看记录',exact:true}).click();
 await suspendRestore();await page.locator('#editor').waitFor({state:'visible'});
 assert.equal(await page.locator('#field-price').isDisabled(),true);
 assert.equal(await page.locator('#record-form [type=submit]').isVisible(),false);
 assert.equal(calls.some(([method])=>method!=='GET'),false);
 await page.locator('#close-editor').click();

 // The explicit publish gate still sends exactly one revision/token request;
 // restoring during that request only enables a user-initiated status read.
 reset();caps.publish_enabled=true;await open();await fillManual();await save();
 await page.locator('#next').click();await page.locator('#review').waitFor();await ready();
 holdPublish=true;await page.locator('#publish').click();await waitUntil(()=>releasePublish,'publish request');
 await suspendRestore();await page.locator('#pending-result').waitFor();
 // A stale GET before the original write settles must not unlock another
 // preview/publish, even when callers dispatch events on disabled controls.
 page.once('dialog',dialog=>dialog.accept());await page.locator('#refresh-result').click();await ready();
 assert.equal(batch.status,'draft');assert.equal(await page.locator('#pending-result').isVisible(),true);
 assert.match(await page.locator('#notice').textContent(),/仍在处理中/);
 assert.equal(await page.locator('#next').isDisabled(),true);
 assert.equal(await page.locator('#publish').isDisabled(),true);
 const beforeBlockedPreview=calls.filter(([,p])=>p.endsWith('/preview')).length;
 await page.evaluate(()=>document.getElementById('next').dispatchEvent(new MouseEvent('click',{bubbles:true})));
 await ready();
 await page.evaluate(()=>document.getElementById('publish').dispatchEvent(new MouseEvent('click',{bubbles:true})));
 await ready();
 assert.equal(calls.filter(([,p])=>p.endsWith('/preview')).length,beforeBlockedPreview);
 assert.equal(calls.filter(([,p])=>p.endsWith('/publish')).length,1);
 holdPublish=false;releasePublish();releasePublish=null;await page.waitForTimeout(30);
 assert.equal(publishes,1);assert.equal(calls.filter(([,p])=>p.endsWith('/publish')).length,1);
 page.once('dialog',dialog=>dialog.accept());await page.locator('#refresh-result').click();await ready();
 assert.equal(await page.locator('#pending-result').isVisible(),false);
 assert.equal(await page.locator('#add-row').isDisabled(),true);
 assert.equal(await page.locator('#publish').isDisabled(),true);
 assert.equal(publishes,1);

 // A settled network failure is still ambiguous. The unchanged draft cannot
 // prove that an accepted write failed; a terminal server result can recover.
 reset();caps.publish_enabled=true;await open();await fillManual();await save();
 await page.locator('#next').click();await page.locator('#review').waitFor();await ready();
 holdPublish=true;publishOutcome='unknown';await page.locator('#publish').click();await waitUntil(()=>releasePublish,'unknown publish');
 await suspendRestore();holdPublish=false;releasePublish();releasePublish=null;await page.waitForTimeout(40);
 page.once('dialog',dialog=>dialog.accept());await page.locator('#refresh-result').click();await ready();
 assert.equal(await page.locator('#pending-result').isVisible(),true);
 assert.match(await page.locator('#notice').textContent(),/结果不明.*原版本草稿不能证明未执行/);
 assert.equal(await page.locator('#next').isDisabled(),true);
 assert.equal(await page.locator('#publish').isDisabled(),true);
 assert.equal(calls.filter(([,p])=>p.endsWith('/publish')).length,1);
 batch.status='published';
 page.once('dialog',dialog=>dialog.accept());await page.locator('#refresh-result').click();await ready();
 assert.equal(await page.locator('#pending-result').isVisible(),false);
 assert.equal(await page.locator('#publish').isDisabled(),true);

 // An explicit 409 is a known rejection. Recovery reads the current revision,
 // then a separate user action may obtain a new preview and publish once.
 reset();caps.publish_enabled=true;await open();await fillManual();await save();
 await page.locator('#next').click();await page.locator('#review').waitFor();await ready();
 holdPublish=true;publishOutcome='conflict';await page.locator('#publish').click();await waitUntil(()=>releasePublish,'conflicting publish');
 await suspendRestore();holdPublish=false;releasePublish();releasePublish=null;await page.waitForTimeout(30);
 page.once('dialog',dialog=>dialog.accept());await page.locator('#refresh-result').click();await ready();
 assert.equal(await page.locator('#pending-result').isVisible(),false);
 assert.equal(calls.filter(([,p])=>p.endsWith('/publish')).length,1);
 publishOutcome='success';await page.locator('#next').click();await page.locator('#review').waitFor();await ready();
 await page.locator('#publish').click();await ready();
 assert.equal(publishes,1);assert.equal(calls.filter(([,p])=>p.endsWith('/publish')).length,2);

 // A late capabilities result cannot overwrite a newly mounted root, even if
 // a custom client ignores AbortSignal. No production network is involved.
 await page.evaluate(async url=>{
  const m=await import(url);m.dispose(document.body);
  window.late={};
  const old=m.mount(document.body,{client:(path,method,body,raw,signal)=>{
   window.late.signal=signal;return new Promise(resolve=>{window.late.resolve=resolve;});
  }});
  old.dispose();window.late.aborted=window.late.signal.aborted;
  const current=m.mount(document.body,{client:async()=>({schema_ready:false})});
  await current.ready;window.late.resolve({schema_ready:true});await old.ready;
 },controllerURL);
 assert.equal(await page.evaluate(()=>window.late.aborted),true);
 assert.equal(await page.locator('#workspace').isVisible(),false);
 assert.match(await page.locator('#notice').textContent(),/暂不可用/);

 // Root-scoped lookup: duplicate IDs outside the mount are not mutated.
 await page.evaluate(async url=>{
  const m=await import(url);m.dispose(document.body);
  const foreign=document.createElement('div');foreign.id='foreign';
  const outside=document.createElement('p');outside.id='notice';outside.textContent='outside';foreign.append(outside);
  const root=document.createElement('section');root.id='import-test-root';
  for(const child of [...document.body.children]) if(child.tagName!=='SCRIPT') root.append(child);
  document.body.append(foreign,root);
  const page=m.mount(root,{client:async()=>({schema_ready:false})});await page.ready;
  window.scoped={outside:outside.textContent,inside:root.querySelector('#notice').textContent};page.dispose();
 },controllerURL);
 assert.deepEqual(await page.evaluate(()=>window.scoped),{outside:'outside',inside:'账户导入暂不可用：暂存表尚未就绪或服务暂时无法连接。'});
 assert.deepEqual(errors,[]);
 console.log(JSON.stringify({sameRootIdempotent:true,nativeLeaveCancellation:true,exactDraftRestored:true,historyBackForward:true,noPreviewReplay:true,pendingWriteExplicitRefresh:true,earlyRefreshKeepsBarrier:true,unknownOutcomeKeepsBarrier:true,known409Recovers:true,unknownBatchRecovery:true,uploadChainNotReplayed:true,publishNotReplayed:true,lateReadSuppressed:true,rootScoped:true,productionRequests:0,errors}));
} finally {await browser.close();}
