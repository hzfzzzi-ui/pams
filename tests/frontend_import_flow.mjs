// Offline browser contract: all data and financial writes are intercepted.
import assert from 'node:assert/strict';
import {readFile,mkdir} from 'node:fs/promises';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
const {chromium}=await import(pathToFileURL(process.env.PAMS_FRONTEND_PLAYWRIGHT_MODULE).href);
const browser=await chromium.launch({headless:true,channel:process.env.PAMS_FRONTEND_PLAYWRIGHT_CHANNEL||undefined});
const root=path.resolve('frontend/public'), output=process.env.PAMS_FRONTEND_ARTIFACT_DIR;
if(output) await mkdir(output,{recursive:true});
let batch,caps,publishes,calls,failPreview,uploads;
const defaults={authentication_required:false,schema_ready:true,publish_enabled:false,ocr_available:true,limits:{files:10,file_bytes:10485760}};
const fields=(kind='trade')=>({kind,business_date:'2026-10-20',asset_type:'stock',symbol:'600001',name:'合成标的',currency:'CNY',action:'buy',status:'confirmed',quantity:'1',price:'10.0100',gross_amount:'10.0100',fee_scope:'separate',fee_amount:'0',tax_amount:'0',net_cash:'-10.0100'});
const reset=()=>{batch=null;caps={...defaults};publishes=0;calls=[];uploads=[];failPreview=false;};
const accounts=[{account_code:'ths',profile:'tonghuashun',label:'同花顺',currency:'CNY',default_asset_type:'stock',kinds:['trade','holding'],trade_actions:{stock:['buy','sell','dividend','fee'],fund:['buy','sell','dividend','fee']}},
 {account_code:'licai',profile:'tencent_licaitong',label:'理财通',currency:'CNY',default_asset_type:'fund',kinds:['trade','holding'],trade_actions:{fund:['subscribe','redeem','dividend','fee']}},
 {account_code:'bank',profile:'statement',label:'银行账户',currency:'CNY',default_asset_type:'cash',kinds:['cash'],trade_actions:{cash:['income','expense','transfer','balance']}}];
const context=await browser.newContext({viewport:{width:390,height:844},hasTouch:true});
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
  if(p.endsWith('/batches') && req.method()==='POST') {batch={batch_id:'b',revision:1,status:'draft',rows:[],files:[],...req.postDataJSON()};return send(batch);}
  if(p==='/api/account-import/batches/b' && req.method()==='GET') return send(batch);
  if(p==='/api/account-import/batches/b' && req.method()==='PUT') {
   const body=req.postDataJSON(); assert.equal(body.revision,batch.revision);
   batch.rows=body.rows.map((row,index)=>{
    const old=batch.rows.find(item=>item.row_id===row.row_id), source=old?.source || (row.image_file_id?{origin:'ocr',file_id:row.image_file_id}:{origin:'manual'});
    if(old?.can_edit_fields===false) assert.deepEqual(row.fields,old.fields);
    return {...row,row_id:row.row_id||'r'+index,source,can_edit_fields:!source.file_id||source.origin==='ocr'};
   }); batch.revision++;return send(batch);
  }
  if(p.endsWith('/files') && req.method()==='POST') {
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
  if(p.endsWith('/publish')) {assert.equal(caps.publish_enabled,true);publishes++;batch.status='published';return send({batch});}
  throw new Error('Unexpected API '+req.method()+' '+p);
 }
 const file=path.resolve(root,'.'+p);assert.ok(file.startsWith(root+path.sep));
 try {await route.fulfill({contentType:file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'text/html',body:await readFile(file)});} catch {await route.abort();}
});
const ready=async()=>page.waitForFunction(()=>!document.getElementById('workspace').inert);
const open=async()=>{await page.goto('http://import.invalid/account_import.html');await page.waitForFunction(()=>!document.getElementById('workspace').hidden);await ready();};
const save=async()=>{await page.locator('#record-form [type=submit]').click();await page.locator('#editor').waitFor({state:'hidden'});await ready();};
const capture=async name=>{await page.evaluate(()=>window.scrollTo({top:0,left:0,behavior:'instant'}));await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(resolve)));assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);const head=await page.locator('.import-head').boundingBox();assert.ok(head.y>=0&&head.y<100,JSON.stringify({name,head,metrics:await page.evaluate(()=>({scroll:scrollY,height:innerHeight,doc:document.documentElement.scrollHeight,body:document.body.getBoundingClientRect().toJSON(),active:document.activeElement.id}))}));if(output)await page.screenshot({path:path.join(output,name+'.png'),fullPage:true});};
try {
 reset();caps.schema_ready=false;await page.goto('http://import.invalid/account_import.html');await page.getByText('账户导入暂不可用',{exact:false}).waitFor();assert.equal(calls.length,1);
 caps.schema_ready=true;await page.locator('#retry').click();await page.locator('#workspace').waitFor();await ready();
 assert.equal(await page.locator('#unlock-form, input[type=password]').count(),0);assert.equal(calls.some(([,p])=>p.endsWith('/unlock')),false);
 for(const width of [320,390,1280]) {
  await page.setViewportSize({width,height:844});reset();await open();
  assert.equal(await page.locator('nav').count(),0);
  await page.locator('#kind').selectOption('cash');assert.deepEqual(await page.locator('#account option').evaluateAll(nodes=>nodes.map(n=>n.value)),['bank']);
  await page.locator('#kind').selectOption('trade');await page.locator('#mode-manual').click();await page.locator('#add-row').click();
  await page.locator('#field-business_date').fill('2026-10-20');await page.locator('#field-symbol').fill('600001');await page.locator('#field-name').fill('手动<img src=x onerror=alert(1)>');
  await page.locator('#field-action').selectOption('buy');await page.locator('#field-fee_scope').selectOption('separate');
  for(const [key,value] of Object.entries({quantity:'1',price:'10.0100',gross_amount:'10.0100',fee_amount:'0',tax_amount:'0',net_cash:'-10.0100'})) await page.locator('#field-'+key).fill(value);
  await save();assert.equal(batch.rows[0].fields.net_cash,'-10.0100');assert.equal(batch.rows[0].fields.fee_amount,'0');
  await page.locator('#rows button').click();await page.locator('#field-note').fill('未保存输入');await page.locator('#close-editor').click();await page.locator('#save-draft').click();assert.equal(await page.locator('#field-note').inputValue(),'未保存输入');await save();
  await page.locator('#next').click();await page.locator('#review').waitFor();await ready();assert.equal(await page.locator('#publish').isDisabled(),true);assert.equal(publishes,0);
  await page.getByText('仅预览',{exact:true}).waitFor();await page.getByText('资金待关联 · 1 条',{exact:true}).waitFor();await page.getByText('其他来源异常',{exact:true}).waitFor();
  assert.equal(await page.getByText('资产底表待补充',{exact:false}).count(),0);assert.equal(await page.getByText('NAV 影响暂不可用',{exact:false}).count(),0);await capture(width+'-manual-review');
  await page.evaluate(()=>{const target=document.querySelector('.import-head h1');for(const [type,x] of [['touchstart',4],['touchmove',100],['touchend',110]]) {const touch=new Touch({identifier:1,target,clientX:x,clientY:70});target.dispatchEvent(new TouchEvent(type,{bubbles:true,cancelable:true,touches:type==='touchend'?[]:[touch],changedTouches:[touch]}));}});await page.locator('#intake').waitFor();assert.equal(batch.rows.length,1);
  failPreview=true;await page.locator('#next').click();await page.locator('#conflict').waitFor();assert.equal(batch.rows[0].fields.note,'未保存输入');
  reset();await open();await page.locator('#mode-ocr').click();await page.locator('#images').setInputFiles([{name:'一.png',mimeType:'image/png',buffer:Buffer.from('png1')},{name:'二.png',mimeType:'image/png',buffer:Buffer.from('png2')}]);
  await page.waitForFunction(()=>document.getElementById('file-list').querySelectorAll('a').length===2&&!document.getElementById('workspace').inert);
  await page.locator('#next').click();await page.locator('#image-review').waitFor();await ready();assert.equal(await page.locator('#image-records article').count(),2);
  await page.locator('#image-original img').click();await page.locator('#image-viewer').waitFor();await page.locator('#close-image-viewer').click();
  await page.getByRole('button',{name:'修改本条记录',exact:true}).first().click();await page.locator('#field-name').fill('本图校正');await save();assert.equal(await page.locator('#image-review').isVisible(),true);
  await capture(width+'-image-review');await page.locator('#confirm-image').click();await page.getByText('截图 2 / 2',{exact:true}).waitFor();await ready();assert.equal(batch.rows[0].acknowledged,true);assert.equal(batch.rows[2].acknowledged,false);assert.equal(publishes,0);
  await page.locator('#previous-image').click();await page.getByText('截图 1 / 2',{exact:true}).waitFor();await page.locator('#confirm-image').click();await ready();await page.locator('#confirm-image').click();await page.locator('#review').waitFor();await ready();assert.equal(batch.rows.length,4);assert.equal(publishes,0);await capture(width+'-images-summary');
  reset();await open();await page.locator('#files').setInputFiles({name:'原始.xls',mimeType:'application/vnd.ms-excel',buffer:Buffer.from('native synthetic table')});
  await page.waitForFunction(()=>document.getElementById('file-list').querySelectorAll('a').length===1&&!document.getElementById('workspace').inert);
  await page.getByRole('button',{name:'配置字段',exact:true}).click();
  await page.getByRole('button',{name:'保存映射并重新识别',exact:true}).click();assert.equal(calls.filter(c=>c[1].endsWith('/parse')).length,0);
  await page.getByRole('button',{name:'确认替换并重新识别',exact:true}).click();await ready();await page.locator('#next').click();await page.locator('#review').waitFor();await ready();
  await page.getByRole('button',{name:'查看 / 处理',exact:true}).click();assert.equal(await page.locator('#field-gross_amount').isDisabled(),true);await page.locator('#record-form [type=submit]').click();await page.locator('#editor').waitFor({state:'hidden'});await ready();assert.equal(batch.rows[0].fields.gross_amount,'10.0100');await capture(width+'-file-review');
  await page.getByRole('button',{name:'排除此条',exact:true}).click();await ready();assert.equal(batch.rows[0].excluded,true);assert.equal(publishes,0);
  reports.push({width,manual:true,ocr_images:2,ocr_records:4,file_readonly:true,layout_overflow:false,publishes});
 }
 reset();await open();await page.locator('#kind').selectOption('cash');await page.locator('#files').setInputFiles({name:'bank.csv',mimeType:'text/csv',buffer:Buffer.from('synthetic separate balance')});
 await page.waitForFunction(()=>document.getElementById('file-list').querySelector('a')&&!document.getElementById('workspace').inert);await page.locator('#next').click();await page.locator('#review').waitFor();await ready();
 await page.getByRole('heading',{name:'账单流水',exact:true}).waitFor();await page.getByRole('heading',{name:'期末余额 · 独立确认',exact:true}).waitFor();assert.equal(batch.rows[1].excluded,true);assert.equal(publishes,0);await capture('bank-separate-balance');
 reset();caps.ocr_available=false;await open();await page.locator('#mode-ocr').click();await page.locator('#images').setInputFiles({name:'empty.png',mimeType:'image/png',buffer:Buffer.from('synthetic empty OCR')});
 await page.waitForFunction(()=>document.getElementById('file-list').querySelector('a')&&!document.getElementById('workspace').inert);
 await page.locator('#next').click();await page.locator('#image-review').waitFor();await ready();assert.equal(batch.rows.length,0);
 await page.getByText('本图未识别出记录，请对照原图补充；空图不能确认。',{exact:true}).waitFor();await page.locator('#confirm-image').click();await ready();assert.equal(await page.locator('#image-review').isVisible(),true);assert.equal(publishes,0);
 await page.getByRole('button',{name:'补充本图记录',exact:true}).click();await page.locator('#field-business_date').fill('2026-10-20');await page.locator('#field-symbol').fill('600001');await page.locator('#field-action').selectOption('buy');await save();
 assert.equal(batch.rows[0].source.file_id,'f1');assert.equal(batch.rows[0].source.origin,'ocr');await page.locator('#confirm-image').click();await page.locator('#review').waitFor();await ready();assert.equal(batch.rows[0].acknowledged,true);assert.equal(publishes,0);
 await page.goBack();await page.locator('#image-review').waitFor();assert.equal(batch.rows.length,1);
 reset();caps.publish_enabled=true;await open();await page.locator('#mode-manual').click();await page.locator('#add-row').click();await page.locator('#field-business_date').fill('2026-10-20');await page.locator('#field-action').selectOption('buy');await save();
 await page.locator('#next').click();await page.locator('#review').waitFor();await ready();await page.locator('#publish').click();await page.waitForFunction(()=>document.getElementById('batch-meta').textContent.includes('已发布'));assert.equal(publishes,1);assert.equal(await page.locator('#add-row').isDisabled(),true);
 assert.deepEqual(errors,[]);console.log(JSON.stringify({reports,errors,productionRequests:0}));
} finally {await browser.close();}
