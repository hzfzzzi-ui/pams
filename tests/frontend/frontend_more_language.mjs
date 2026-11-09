import assert from 'node:assert/strict';
import {readFile,mkdir,writeFile} from 'node:fs/promises';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
const {chromium}=await import(pathToFileURL(process.env.PAMS_FRONTEND_PLAYWRIGHT_MODULE).href);
const root=path.resolve(process.argv[2]),output=process.env.PAMS_MORE_ARTIFACT_DIR;
if(output)await mkdir(output,{recursive:true});
const browser=await chromium.launch({headless:true,channel:process.env.PAMS_FRONTEND_PLAYWRIGHT_CHANNEL||undefined});
const page=await browser.newPage({viewport:{width:390,height:844}}),errors=[],calls=[],reports=[];
await page.addInitScript(() => Object.defineProperty(navigator, 'connection', {
  configurable: true, value: {saveData: true},
}));
let status='ok', accountUnavailable=false, reviewUnavailable=false, holdingsUnavailable=false;
const sync = {groups: [
  {code:'ccb',label:'合成银行',rows:[
    {label:'合成人民币',active:true,currency:'CNY',summary:{status:'normal',balance_amount:'18971.58',latest_trade_on:'2026-11-01',last_synced_at:'2026-11-07T09:33:00Z'}},
    {account_code:'fixture_card',label:'合成信用卡',currency:'CNY',summary:{status:'review',balance_amount:'-1286.40',
      review_reasons:[{kind:'duplicates',count:101}]}},
  ]},
  {code:'legacy',label:'合成旧账户',rows:[
    {account_code:'fixture_legacy',label:'很长的合成外币账户名称',currency:'MXN',summary:{status:'stale',balance_amount:null}},
  ]},
  {code:'ibkr',label:'合成券商',rows:[
    {account_code:'tonghuashun',active:true,label:'合成 A 股账户',currency:'CNY',summary:{status:'normal'}},
    {account_code:'ibkr',active:true,label:'合成海外券商',currency:'USD',summary:{status:'normal'}},
  ]},
]};
page.on('pageerror',error=>errors.push(error.message));
await page.route('**/*',async route=>{
 const req=route.request(),url=new URL(req.url());assert.equal(url.origin,'http://language.invalid');
 if(url.pathname.startsWith('/api/')){
  calls.push(url.pathname);
  assert.equal(req.method(),'GET');
  if(url.pathname==='/api/sync/accounts') return route.fulfill({status:accountUnavailable?503:200,contentType:'application/json',body:JSON.stringify(accountUnavailable?{}:sync)});
  if(url.pathname==='/api/holdings/by-account'){
   assert.equal(url.searchParams.get('base_currency'),'CNY');
   assert.equal(url.searchParams.get('include_portfolio_cash'),'true');
   return route.fulfill({status:holdingsUnavailable?503:200,contentType:'application/json',
    body:JSON.stringify({base_currency:'CNY',as_of:'2026-11-06',by_account:[
      {account_code:'tonghuashun',known_value_base:'1234.56',total_value_base:null,
        portfolio_value:{amount_base:'1434.56',cash_base:'200.00',as_of:'2026-11-05',cash_as_of:'2026-10-23',status:'cash_carried_forward'}},
      {account_code:'ibkr',known_value_base:'7890.12',total_value_base:null,
        portfolio_value:{amount_base:'6890.12',cash_base:'-1000.00',as_of:'2026-11-06',cash_as_of:'2026-11-06',status:'official_close'}},
    ]})});
  }
  if(url.pathname==='/api/sync/accounts/fixture_card/source-anomalies'){
   assert.equal(url.searchParams.get('kind'),'duplicates');
   if(reviewUnavailable)return route.fulfill({status:503,contentType:'application/json',body:'{}'});
   const offset=Number(url.searchParams.get('offset'));
   assert.ok([0,100].includes(offset));
   const rows=Array.from({length:offset?1:100},(_,i)=>({day:'2026-11-01',currency:'CNY',amount:-12.5,
     summary:'合成摘要',source_id:'fixture-source',filename:'合成账单.xls',source_position:{row:offset+i+2}}));
   return route.fulfill({contentType:'application/json',body:JSON.stringify({rows})});
  }
  assert.ok(['/api/portfolio/data-quality','/api/portfolio/equity-bridge'].includes(url.pathname),url.pathname);
  if(status==='failed'||url.pathname.endsWith('/equity-bridge'))return route.fulfill({status:503,contentType:'application/json',body:'{}'});
  return route.fulfill({contentType:'application/json',body:JSON.stringify({data_quality:{summary:{status,error_count:0,warning_count:status==='ok'?0:2}}})});
 }
 const file=path.resolve(root,'.'+decodeURIComponent(url.pathname));assert.ok(file.startsWith(root+path.sep));
 try{
  let data=await readFile(file);
  if(url.pathname==='/investments.html')data=Buffer.from(data.toString().replace(/<script\b[^>]*>[\s\S]*?<\/script>/g,'').replace(/<link[^>]*href="https:[^>]*>/g,''));
  await route.fulfill({contentType:file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':file.endsWith('.html')?'text/html':file.endsWith('.jpg')?'image/jpeg':'application/octet-stream',body:data});
 }catch{await route.abort();}
});
const style=selector=>page.locator(selector).first().evaluate(node=>{
 const s=getComputedStyle(node),r=node.getBoundingClientRect();
 return {font:s.fontFamily,size:s.fontSize,weight:s.fontWeight,line:s.lineHeight,color:s.color,background:s.backgroundColor,gutter:s.paddingLeft,width:r.width,height:r.height,spacing:s.letterSpacing};
});
const capture=async name=>{
 await page.mouse.move(0,0);await page.evaluate(()=>window.scrollTo({top:0,behavior:'instant'}));
 await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(resolve)));
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
 if(output)await page.screenshot({path:path.join(output,name+'.png'),fullPage:true,scale:'css'});
};
try{
 for(const width of [320,390,1280]){
  await page.setViewportSize({width,height:844});
  await page.goto('http://language.invalid/investments.html');
  await page.evaluate(()=>{
   const row=document.createElement('div');row.className='mobileHoldingText';const label=document.createElement('strong');label.textContent='标的名称';row.append(label);document.getElementById('holdings-smart-table').append(row);
  });
  const investment={body:await style('body'),name:await style('.mobileHoldingText strong'),wrap:await style('.wrap')};
  status='ok';const before=calls.length;await page.goto('http://language.invalid/more.html');await page.waitForFunction(()=>document.getElementById('moreOrigin')?.textContent);
  await page.locator('#moreIssues').waitFor({state:'hidden'});assert.equal(await page.locator('#moreAdvanced').getAttribute('open'),null);
  const center=page.locator('#account-center > summary');
  const account=page.locator('[data-institution="ccb"]'), child=page.getByRole('heading',{name:'合成人民币',exact:true});
  assert.equal(await page.locator('#account-center').getAttribute('open'),null);
  assert.equal(await page.locator('#accountSyncPanel').isVisible(),false);
  assert.equal(await page.locator('#moreImport').isVisible(),false);
  await center.click();
  await account.waitFor();
  const body=await style('body'),primary=await style('.accountInstitutionButton'),wrap=await style('.wrap');
  assert.equal(body.font,investment.body.font);assert.equal(body.background,investment.body.background);
  assert.equal(primary.font,investment.name.font);
  assert.equal(wrap.gutter,investment.wrap.gutter);assert.ok(wrap.width<=430);assert.equal(await page.locator('.moreHub').evaluate(n=>getComputedStyle(n).display),'block');
  assert.equal(await page.locator('.nav a').count(),0);
  assert.equal(await page.locator('#moreAssetManagement,[data-account-tab],a[href^="accounts.html"]').count(),0);
  await page.getByRole('link',{name:'导入资料',exact:true}).waitFor();
  assert.deepEqual(calls.slice(before).sort(),['/api/holdings/by-account','/api/portfolio/data-quality','/api/sync/accounts']);
  await child.waitFor({state:'visible'});
  assert.equal(await page.locator('.accountInstitutionButton').count(),3);
  assert.equal(await page.locator('.accountInstitutionPanel:visible').count(),1);
  assert.equal(await account.getAttribute('aria-pressed'),'true');
  assert.equal(await account.locator('[data-status="review"]').count(),1);
  await page.waitForFunction(() => document.querySelector('[data-institution="ccb"] img')?.naturalWidth === 100);
  assert.equal(await page.locator('[data-institution="legacy"] img').count(),0);
  assert.deepEqual(await page.locator('.accountSyncBadge').allTextContents(),['待核查','超月未同步']);
  assert.deepEqual(await page.locator('.accountSyncAmount').evaluateAll(nodes=>nodes.map(n=>n.dataset.sign)),['positive','negative','neutral','positive','positive']);
  assert.match(await page.locator('.accountSyncAmount').nth(1).textContent(), /^-.*1,286\.40$/);
  assert.equal(await page.locator('.accountSyncAmount').nth(2).textContent(),'—');
  assert.deepEqual(await page.locator('.accountSyncAmount').allTextContents().then(values=>values.slice(3)),['¥1,434.56','¥6,890.12']);
  assert.deepEqual(await page.locator('.accountSyncAmountWithLabel small').allTextContents(),['组合估值 · 2026-11-05','组合价值 · 2026-11-06']);
  assert.equal(await page.locator('#accountSyncPanel article :is(button,a,details,p):not(.accountSyncNameButton)').count(),1);
  const review=page.getByRole('button',{name:'合成信用卡：待核查'});
  const sheet=page.getByRole('dialog');
  await page.getByRole('button',{name:'合成人民币',exact:true}).click();
  assert.deepEqual(await sheet.locator('.accountSyncMetadata dd').allTextContents(),['2026-11-01','2026-11-07 17:33']);
  await page.keyboard.press('Escape');await sheet.waitFor({state:'hidden'});
  reviewUnavailable=true;
  await review.focus();await page.keyboard.press('Enter');
  await sheet.getByText('疑似重复 · 101 笔',{exact:true}).waitFor();
  await sheet.getByText('记录读取失败。',{exact:true}).waitFor();
  reviewUnavailable=false;
  await sheet.getByRole('button',{name:'重试'}).click();
  const original=sheet.getByRole('link',{name:'合成账单.xls · 第 2 行',exact:true});
  await original.waitFor();
  assert.equal(await original.getAttribute('href'),'/api/account-import/source-records/fixture-source/original');
  await sheet.getByRole('button',{name:'加载更多'}).click();
  await sheet.getByRole('link',{name:'合成账单.xls · 第 102 行',exact:true}).waitFor();
  assert.equal(await sheet.getByRole('link').count(),101);
  await page.keyboard.press('Escape');await sheet.waitFor({state:'hidden'});
  assert.equal(await review.evaluate(n=>n===document.activeElement),true);
  assert.equal(new URL(page.url()).pathname,'/more.html');
  await capture(width+'-account-open');
  await page.locator('[data-institution="legacy"]').click();
  await child.waitFor({state:'hidden'});
  await page.getByRole('heading',{name:'很长的合成外币账户名称'}).waitFor();
  await account.click();
  await child.waitFor({state:'visible'});
  await center.click();
  await child.waitFor({state:'hidden'});
  await center.click();
  await child.waitFor({state:'visible'});
  await capture(width+'-home');
  await page.locator('#moreAdvanced>summary').click();await page.getByText('数据诊断',{exact:true}).waitFor();await page.mouse.move(0,0);
  const children=await page.locator('.moreAdvanced>.moreRow strong,.moreAdvanced>details>summary').evaluateAll(nodes=>nodes.map(node=>{const s=getComputedStyle(node);return {font:s.fontFamily,size:s.fontSize,weight:s.fontWeight,color:s.color,background:s.backgroundColor};}));
  assert.equal(children.length,3);for(const child of children){assert.equal(child.font,body.font);assert.equal(child.size,'14px');assert.equal(child.weight,'600');assert.equal(child.color,body.color);assert.equal(child.background,'rgba(0, 0, 0, 0)');}
  assert.equal(await page.locator('#moreAdvanced>summary').evaluate(n=>getComputedStyle(n).backgroundColor),'rgba(0, 0, 0, 0)');
  await capture(width+'-advanced');
  await page.locator('.moreSystem>summary').click();await page.locator('#moreOrigin').waitFor();assert.equal(await page.locator('#moreOrigin').textContent(),'http://language.invalid');
  await page.locator('#moreNavGrowthDetails>summary').click();await page.getByText('NAV增值解释加载失败：',{exact:false}).waitFor();
  assert.equal(calls.slice(before).filter(p=>p.endsWith('/equity-bridge')).length,1);await capture(width+'-audit-error');
  await account.focus();await page.keyboard.press('Tab');await page.keyboard.press('Shift+Tab');assert.equal(await account.evaluate(n=>getComputedStyle(n).outlineStyle),'solid');
  await account.locator('.accountInstitutionLabel').evaluate(n=>n.textContent='合成的较长金融机构账户名称用于验证手机布局');await capture(width+'-long-label');
  assert.equal(await account.locator('.accountInstitutionLogo').isVisible(),true);
  await page.goto('http://language.invalid/more.html?account=fixture_legacy#account-center');
  await page.locator('[data-institution="legacy"][aria-pressed="true"]').waitFor();
  assert.equal(await page.locator('[data-account-code="fixture_legacy"]').isVisible(),true);
  await page.goto('http://language.invalid/more.html');
  status='warning';await page.reload();await page.getByText('错误 0 · 警告 2',{exact:true}).waitFor();await capture(width+'-issues');
  status='failed';await page.reload();await page.getByText('数据状态暂不可用',{exact:true}).waitFor();await capture(width+'-unknown');
  accountUnavailable=true;await page.reload();await center.click();await page.getByText('暂时无法读取账户状态。',{exact:true}).waitFor();
  accountUnavailable=false;await page.getByRole('button',{name:'重新读取账户状态'}).click();await account.waitFor();
  holdingsUnavailable=true;await page.reload();await center.click();await account.waitFor();
  assert.equal(await page.locator('.accountSyncAmount').first().textContent(),'¥18,971.58');
  assert.equal(await page.locator('.accountSyncAmount').last().textContent(),'—');
  holdingsUnavailable=false;
  reports.push({width,investment,more:{body,primary,wrap,children},states:['normal','advanced','audit-error','long-label','warning','unknown'],overflow:false});
 }
 assert.deepEqual(errors,[]);const report={reports,errors,productionRequests:0,writes:0,fonts:'shared system fallbacks; no external font download'};
 if(output)await writeFile(path.join(output,'proof.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report));
}finally{await browser.close();}
