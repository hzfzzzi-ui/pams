// Offline lifecycle test: all API responses are synthetic and every write is blocked.
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
const {chromium}=await import(pathToFileURL(process.env.PAMS_FRONTEND_PLAYWRIGHT_MODULE).href);
const browser=await chromium.launch({headless:true,channel:process.env.PAMS_FRONTEND_PLAYWRIGHT_CHANNEL||undefined});
const publicRoot=path.resolve(process.argv[2] || 'frontend/public');
const context=await browser.newContext({viewport:{width:390,height:844}}),page=await context.newPage();
const errors=[],writes=[];
page.on('pageerror',error=>errors.push(error.message));
await context.route('**/*',async route=>{
 const request=route.request(),url=new URL(request.url());
 assert.equal(url.origin,'http://holdings.invalid');
 if(request.method()!=='GET') {writes.push(url.pathname);return route.fulfill({status:405,body:'{}'});}
 if(!/\.(?:js|css|html)$/.test(url.pathname)) return route.fulfill({contentType:'application/json',body:JSON.stringify({rows:[{date:'2026-10-20',value:120,market_value:120,net_flow:0,return_rate:0.2,netWorth:120}],as_of:'2026-10-20'})});
 const file=path.resolve(publicRoot,'.'+url.pathname);assert.ok(file.startsWith(publicRoot+path.sep));
 let body=await readFile(file,'utf8');
 if(file.endsWith('.html'))body=body.replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi,'');
 await route.fulfill({contentType:file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'text/html',body});
});
try {
 await page.goto('http://holdings.invalid/investments.html?content=1');
 const result=await page.evaluate(async()=>{
  const base='/assets/js/features/investment/holdings/';
  const list=await import(base+'list_controller.js?v=20261031-holdings-v1');
  const summary=await import(base+'summary_controller.js?v=20261031-holdings-v1');
  const closed=await import(base+'closed_positions.js?v=20261031-holdings-v1');
  const assert=(condition,message)=>{if(!condition)throw new Error(message);};
  const root=document.body, listeners=new Set(),timers=new Set(),observers=new Set(),charts=[];
  // The legacy distribution canvas is optional in today's page; exercise its
  // supported renderer explicitly without adding it to production markup.
  const distribution=document.createElement('canvas');distribution.id='categoryPieChart';root.append(distribution);
  // Category details remain hidden in production markup; its retained legacy
  // renderer is exercised here explicitly, without restoring that old UI.
  document.querySelector('#category-detail-shell').hidden=false;
  let shows=0,closes=0,reads=[],deferredReads=null,gate=null;
  const trackEvents=target=>new Proxy(target,{get(target,key){
   if(key==='addEventListener')return(type,callback,options)=>{target.addEventListener(type,callback,options);listeners.add({target,type,callback});};
   if(key==='removeEventListener')return(type,callback,options)=>{target.removeEventListener(type,callback,options);for(const entry of listeners)if(entry.target===target&&entry.type===type&&entry.callback===callback)listeners.delete(entry);};
   const value=Reflect.get(target,key,target);return typeof value==='function'?value.bind(target):value;
  }});
  class Observer extends MutationObserver {
   constructor(callback){super(callback);observers.add(this);}
   disconnect(){observers.delete(this);super.disconnect();}
  }
  class Chart {
   constructor(canvas,config){this.canvas=canvas;this.data=config.data;this.options=config.options;this.destroyed=false;charts.push(this);}
   destroy(){this.destroyed=true;}
   update(){}
  }
  const eventHost=trackEvents(window);
  const host=new Proxy(window,{get(target,key){
   if(key==='MutationObserver')return Observer;
   if(key==='matchMedia')return query=>trackEvents(window.matchMedia(query));
   if(key==='setTimeout')return(callback,delay)=>{const timer=window.setTimeout(()=>{timers.delete(timer);callback();},delay);timers.add(timer);return timer;};
   if(key==='clearTimeout')return timer=>{timers.delete(timer);window.clearTimeout(timer);};
   return Reflect.get(eventHost,key,eventHost);
  }});
  const row={symbol:'TEST',name:'合成持仓',major_category:'A股',minor_category:'A股',account:'tonghuashun',currency:'CNY',market_value_cny:120,cost_cny:100,holding_pnl_cny:20,pnl_cny:20,realized_trade_pnl_cny:0,daily_pnl_cny:2,daily_total_pnl_cny:2,quantity:10,current_price:12,previous_price:11.8,valuation_updated_at:'2026-10-20T08:00:00Z'};
  const payload=url=>{
   if(url.startsWith('/holdings/latest'))return{as_of:'2026-10-20',rows:[row]};
   if(url.startsWith('/holdings/by-category/'))return{as_of:'2026-10-20',rows:url.includes('minor_category=A股&')?[row]:[]};
   if(url.includes('/fx-rates/'))return{rates:[]};
   if(url.includes('/accounts/'))return{accounts:[]};
   if(url.startsWith('/summary2/'))return{as_of:'2026-10-20',totals:{market_value:120,pnl:20,daily_pnl:2}};
   if(url.startsWith('/investment/section-return-series'))return{rows:[{date:'2026-10-20',value:120,market_value:120,net_flow:0,return_rate:0.2}]};
   return null;
  };
  const get=(url,options={})=>{
   reads.push({url,signal:options.signal});
   return deferredReads?new Promise(resolve=>deferredReads.push(()=>resolve(structuredClone(payload(url))))):Promise.resolve(structuredClone(payload(url)));
  };
  const showDetails=(_title,html)=>{shows++;let body=document.getElementById('test-sheet');if(!body){body=document.createElement('div');body.id='test-sheet';document.body.append(body);}body.innerHTML=html;return body;};
  const closeDetails=()=>{closes++;document.getElementById('test-sheet')?.remove();};
  const config={get,host,ChartClass:Chart,showDetails,closeDetails,bootstrap(){},
   afterInvestmentNav:task=>gate?gate.then(task):Promise.resolve().then(task),trackInvestmentHoldings:promise=>promise,
   loadInvestmentSummaryFresh:options=>get('/summary2/latest?baseCurrency=CNY',options)};
  const before=document.querySelector('#mobileHoldingList').innerHTML;
  let release;gate=new Promise(resolve=>{release=resolve;});
  const waiting=summary.mount(root,config);waiting.dispose();release();await waiting.ready;gate=null;
  assert(reads.length===0,'disposed startup must issue no fourteen-read bundle');
  assert(listeners.size===0&&timers.size===0&&observers.size===0,'startup disposal must clean all lifetime resources');

  deferredReads=[];reads=[];
  const lateList=list.mount(root,config),lateSummary=summary.mount(root,config);
  await Promise.resolve();await Promise.resolve();
  assert(reads.length===18,'four list plus fourteen summary dependencies must remain');
  lateList.dispose();lateSummary.dispose();
  assert(reads.every(read=>read.signal?.aborted),'all owned reads including summary must cancel');
  for(const resolve of deferredReads)resolve();deferredReads=null;
  await Promise.all([lateList.ready,lateSummary.ready]);
  assert(document.querySelector('#mobileHoldingList').innerHTML===before,'late data must not render');
  assert(listeners.size===0&&timers.size===0&&observers.size===0,'late disposal must clean lifetime resources');

  reads=[];
  for(let round=0;round<2;round++){
   const a=list.mount(root,config),b=summary.mount(root,config);
   assert(list.mount(root,config)===a&&summary.mount(root,config)===b,'same root mount must be idempotent');
   await Promise.all([a.ready,b.ready]);
   assert(document.querySelectorAll('[data-mobile-holding-index]').length===1,'full synthetic list is present');
   const groupChip=document.querySelector('#mobileHoldingFilterChips [data-group]:not([data-group="全部"])');
   assert(groupChip,'synthetic holding group is available');
   const selectedGroup=groupChip.dataset.group;groupChip.click();
   assert(a.snapshot().group===selectedGroup,'a valid group chip updates the owned selection');
   const removedGroup=document.querySelector('#mobileHoldingFilterChips [data-group].active');
   removedGroup.dataset.group='已移除的固定测试分组';removedGroup.click();
   assert(a.snapshot().group==='全部'&&document.querySelector('#holdingGroupFilter').value==='全部','an unavailable group falls back to all holdings');
   const clicks=shows;document.querySelector('[data-mobile-holding-index]').click();assert(shows===clicks+1,'one detail handler after remount');
   document.querySelector('[data-invest-tab="analysis"]').click();
   document.querySelector('#category-detail-shell').open=true;
   document.querySelector('[data-category-target="section-a-stock-equity"]').click();
   await new Promise(resolve=>window.setTimeout(resolve,250));
   assert(charts.some(chart=>chart.canvas?.id==='categoryPieChart'&&!chart.destroyed),'distribution chart is owned');
   assert(charts.some(chart=>chart.canvas?.id==='chartAStockEquity'&&!chart.destroyed),'category chart is owned '+JSON.stringify({tab:root.dataset.investTab,hidden:document.querySelector('#category-detail-shell').hidden,open:document.querySelector('#category-detail-shell').open,sectionHidden:document.querySelector('#section-a-stock-equity').hidden,charts:charts.map(chart=>chart.canvas?.id),paths:reads.map(read=>read.url)}));
   const state=b.snapshot();assert(state.category==='section-a-stock-equity'&&state.categoryOpen,'category selection is preserved');
   a.dispose();b.dispose();
   assert(listeners.size===0&&timers.size===0&&observers.size===0,'listeners/media/observers/timers must release');
   assert(charts.every(chart=>chart.destroyed),'all list/summary/category/main charts must destroy');
   const count=shows;document.querySelector('[data-mobile-holding-index]').click();assert(shows===count,'disposed detail cannot open');
  }
  // One shared URL owner, including preserving all other query/hash values.
  const shell=document.createElement('div');shell.innerHTML='<input type="checkbox" class="showClosedPositionsToggle"><input type="checkbox" class="showClosedPositionsToggle">';root.append(shell);
  let href='http://holdings.invalid/investments.html?content=1&keep=ok#holdings-smart-table',moves=0;
  const fakeHost={location:{search:'?content=1&keep=ok',get href(){return href;},set href(value){moves++;href=value;}}};
  const one=closed.acquireClosedPositions(shell,fakeHost),two=closed.acquireClosedPositions(shell,fakeHost),checkbox=shell.firstElementChild;
  checkbox.checked=true;checkbox.dispatchEvent(new Event('change'));assert(moves===1&&href.includes('keep=ok')&&href.includes('show_closed=1')&&href.endsWith('#holdings-smart-table'),'show_closed must bind once and retain address');
  one.release();checkbox.checked=false;checkbox.dispatchEvent(new Event('change'));assert(moves===2,'remaining owner retains binding');
  two.release();checkbox.dispatchEvent(new Event('change'));assert(moves===2,'last owner releases binding');shell.remove();
  return{readDependencyCount:18,mountRounds:2,lateReadSuppressed:true,disposedStartupReads:0,rootOwnedDetails:shows,closedBinding:true,listeners:listeners.size,timers:timers.size,observers:observers.size,allChartsDestroyed:charts.every(chart=>chart.destroyed)};
 });
 assert.deepEqual(errors,[]);assert.deepEqual(writes,[]);console.log(JSON.stringify({...result,pageErrors:0,writeAttempts:0,productionRequests:0}));
} finally {await browser.close();}
