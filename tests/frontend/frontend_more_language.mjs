import assert from 'node:assert/strict';
import {readFile,mkdir,writeFile} from 'node:fs/promises';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
const {chromium}=await import(pathToFileURL(process.env.PAMS_FRONTEND_PLAYWRIGHT_MODULE).href);
const root=path.resolve(process.argv[2]),output=process.env.PAMS_MORE_ARTIFACT_DIR;
if(output)await mkdir(output,{recursive:true});
const browser=await chromium.launch({headless:true,channel:process.env.PAMS_FRONTEND_PLAYWRIGHT_CHANNEL||undefined});
const page=await browser.newPage({viewport:{width:390,height:844}}),errors=[],calls=[],reports=[];
let status='ok';
page.on('pageerror',error=>errors.push(error.message));
await page.route('**/*',async route=>{
 const req=route.request(),url=new URL(req.url());assert.equal(req.method(),'GET');assert.equal(url.origin,'http://language.invalid');
 if(url.pathname.startsWith('/api/')){
  calls.push(url.pathname);
  assert.ok(['/api/portfolio/data-quality','/api/portfolio/equity-bridge'].includes(url.pathname));
  if(status==='failed'||url.pathname.endsWith('/equity-bridge'))return route.fulfill({status:503,contentType:'application/json',body:'{}'});
  return route.fulfill({contentType:'application/json',body:JSON.stringify({data_quality:{summary:{status,error_count:0,warning_count:status==='ok'?0:2}}})});
 }
 const file=path.resolve(root,'.'+decodeURIComponent(url.pathname));assert.ok(file.startsWith(root+path.sep));
 try{
  let data=await readFile(file);
  if(url.pathname==='/investments.html')data=Buffer.from(data.toString().replace(/<script\b[^>]*>[\s\S]*?<\/script>/g,'').replace(/<link[^>]*href="https:[^>]*>/g,''));
  await route.fulfill({contentType:file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':file.endsWith('.html')?'text/html':'application/octet-stream',body:data});
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
  status='ok';const before=calls.length;await page.goto('http://language.invalid/more.html');await page.waitForFunction(()=>document.querySelector('.nav a.active'));
  await page.locator('#moreIssues').waitFor({state:'hidden'});assert.equal(await page.locator('#moreAdvanced').getAttribute('open'),null);
  const body=await style('body'),primary=await style('.morePrimary strong'),wrap=await style('.wrap');
  assert.equal(body.font,investment.body.font);assert.equal(body.background,investment.body.background);
  for(const key of ['font','size','weight','line','color'])assert.equal(primary[key],investment.name[key],JSON.stringify({width,key,primary,investment}));
  assert.equal(wrap.gutter,investment.wrap.gutter);assert.ok(wrap.width<=430);assert.equal(await page.locator('.moreHub').evaluate(n=>getComputedStyle(n).display),'block');
  assert.equal(await page.locator('.nav a').count(),3);assert.deepEqual(calls.slice(before),['/api/portfolio/data-quality']);
  await capture(width+'-home');
  await page.locator('#moreAdvanced>summary').click();await page.getByText('资产底表与映射',{exact:true}).waitFor();await page.mouse.move(0,0);
  const children=await page.locator('.moreAdvanced>.moreRow strong,.moreAdvanced>details>summary').evaluateAll(nodes=>nodes.map(node=>{const s=getComputedStyle(node);return {font:s.fontFamily,size:s.fontSize,weight:s.fontWeight,color:s.color,background:s.backgroundColor};}));
  assert.equal(children.length,4);for(const child of children){assert.equal(child.font,body.font);assert.equal(child.size,'14px');assert.equal(child.weight,'700');assert.equal(child.color,body.color);assert.equal(child.background,'rgba(0, 0, 0, 0)');}
  assert.equal(await page.locator('#moreAdvanced>summary').evaluate(n=>getComputedStyle(n).backgroundColor),'rgba(0, 0, 0, 0)');
  await capture(width+'-advanced');
  await page.locator('.moreSystem>summary').click();await page.locator('#moreOrigin').waitFor();assert.equal(await page.locator('#moreOrigin').textContent(),'http://language.invalid');
  await page.locator('#moreNavGrowthDetails>summary').click();await page.getByText('NAV增值解释加载失败：',{exact:false}).waitFor();
  assert.equal(calls.slice(before).filter(p=>p.endsWith('/equity-bridge')).length,1);await capture(width+'-audit-error');
  await page.locator('.morePrimary a').first().focus();await page.keyboard.press('Tab');await page.keyboard.press('Shift+Tab');assert.equal(await page.locator('.morePrimary a').first().evaluate(n=>getComputedStyle(n).outlineStyle),'solid');
  await page.locator('.morePrimary strong').first().evaluate(n=>n.textContent='投资账户与持仓资料导入配置确认');await capture(width+'-long-label');
  const label=await page.locator('.morePrimary strong').first().boundingBox(),arrow=await page.locator('.morePrimary a').first().locator('[aria-hidden]').boundingBox();assert.ok(label.x+label.width<=arrow.x);
  status='warning';await page.reload();await page.getByText('错误 0 · 警告 2',{exact:true}).waitFor();await capture(width+'-issues');
  status='failed';await page.reload();await page.getByText('数据状态暂不可用',{exact:true}).waitFor();await capture(width+'-unknown');
  reports.push({width,investment,more:{body,primary,wrap,children},states:['normal','advanced','audit-error','long-label','warning','unknown'],overflow:false});
 }
 assert.deepEqual(errors,[]);const report={reports,errors,productionRequests:0,writes:0,fonts:'shared system fallbacks; no external font download'};
 if(output)await writeFile(path.join(output,'proof.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report));
}finally{await browser.close();}
