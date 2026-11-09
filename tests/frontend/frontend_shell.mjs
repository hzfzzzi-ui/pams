// Isolated shell regression. API failure fixtures exercise navigation independently of assets.
import assert from 'node:assert/strict';
import {readFile, mkdir} from 'node:fs/promises';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {createServer} from 'node:http';
const {chromium} = await import(pathToFileURL(process.env.PAMS_FRONTEND_PLAYWRIGHT_MODULE).href);
const browser = await chromium.launch({headless:true, channel:process.env.PAMS_FRONTEND_PLAYWRIGHT_CHANNEL || undefined});
const root = path.resolve(process.argv[2] || 'frontend/public');
const output = process.env.PAMS_FRONTEND_ARTIFACT_DIR;
if (output) await mkdir(output, {recursive:true});
const context = await browser.newContext();
let delayAccountLoad = false;
let releaseAccountLoad;
let workflowFixtures = false;
const server = createServer(async (req,res) => {
  const url = new URL(req.url,'http://localhost');
  if (/\/(investments|ledger|more|accounts|data_sources|account_import)\.html$/.test(url.pathname) && url.searchParams.get('content') !== '1') {
    res.writeHead(302,{location:`/app.html?view=${url.pathname.slice(1)}&${url.searchParams}`});res.end();return;
  }
  const file=path.resolve(root,'.'+url.pathname);
  try {
    assert.ok(file.startsWith(root+path.sep));
    let body=await readFile(file);
    if (delayAccountLoad && url.pathname === '/accounts.html') {
      // A non-blocking image keeps load pending after the module normalizes its hash.
      body = Buffer.from(body.toString().replace('</body>', '<img hidden src="/pending-account-load"></body>'));
      delayAccountLoad = false;
    }
    res.writeHead(200,{'content-type':({'.html':'text/html; charset=utf-8','.js':'application/javascript; charset=utf-8','.css':'text/css; charset=utf-8'})[path.extname(file)] || 'application/octet-stream'});res.end(body);
  } catch { res.writeHead(503,{'content-type':'application/json'});res.end('{"detail":"Fixed unavailable fixture"}'); }
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const base=`http://127.0.0.1:${server.address().port}`;
await context.route('**/*', async route => {
  const url = new URL(route.request().url());
  if (workflowFixtures && url.hostname === '127.0.0.1') {
    const accounts = [{account_code:'fixture', display_name:'示例储蓄卡', currency:'CNY'}];
    const recentRows = Array.from({length:12}, (_, index) => ({
      id:index + 1, account_code:'fixture', transaction_date:'2026-10-29', amount:-20,
      amount_cny:-20, currency:'CNY', summary:`示例流水 ${index + 1}`, flow_type:'expense',
    }));
    const fixtures = {
      '/api/sync/accounts': {groups:[{label:'示例银行', rows:[{...accounts[0], label:'示例储蓄卡', active:true, can_import:true, summary:{status:'normal'}}]}]},
      '/api/portfolio/data-quality': {data_quality:{summary:{status:'warning',warning_count:1,error_count:0},checks:[
        {id:'account_balance_coverage',status:'attention',details:{missing_accounts:['fixture']}}]}},
      '/api/accounts/overview': {accounts},
      '/api/ledger/workbench': {periods:{}, recent_rows_ytd:recentRows},
      '/api/bank-reconcile/config': {profiles:{fixture:{account_code:'fixture',label:'示例模板'}}},
      '/api/bank-reconcile/imports': {},
      '/api/bank-reconcile/transfer-matches': {},
      '/api/bank-reconcile/unmatched-rows': {},
      '/api/account-import/capabilities': {schema_ready:true},
      '/api/account-import/context': {accounts:[{...accounts[0],label:'示例储蓄卡',kinds:['cash']}]},
      '/api/account-import/batches': {batches:[]},
    };
    if (Object.hasOwn(fixtures,url.pathname)) return route.fulfill({
      json:fixtures[url.pathname], headers:{'access-control-allow-origin':base},
    });
  }
  if (url.origin === base && url.pathname === '/pending-account-load') {
    await new Promise(resolve => { releaseAccountLoad = resolve; });
    return route.fulfill({status: 204, body: ''});
  }
  if(url.origin==='http://unavailable.invalid')return route.fulfill({contentType:'text/html',body:'<!doctype html><title>Isolated external document</title><p>Fixed test document</p>'});
  if(url.origin===base)return route.continue();
  if(url.hostname==='127.0.0.1')return route.fulfill({status:503,contentType:'application/json',body:'{"detail":"Fixed unavailable fixture"}'});
  return route.abort();
});
const page = await context.newPage();
const session = await context.newCDPSession(page);
await session.send('Emulation.setEmulatedMedia', {
  features: [{name:'prefers-reduced-transparency', value:'no-preference'}],
});
const failures = [];
const missingAssets = [];
page.on('response', response => {
  const url = new URL(response.url());
  if (url.origin === base && url.pathname.startsWith('/assets/build-') && !response.ok()) {
    missingAssets.push(url.pathname);
  }
});
page.on('pageerror', error => failures.push(error.message));
try {
  for (const width of [320,390,926,1280]) {
    await page.setViewportSize({width,height:1084});
    await page.goto(`${base}/app.html?view=investments.html`);
    await page.locator('[data-page="investments.html"][aria-current]').waitFor();
    await page.evaluate(() => { window.originalNav = document.querySelector('.pams-primary-nav'); });
    const before = await page.locator('.pams-primary-nav').boundingBox();
    for (const key of ['ledger','more','investments']) {
      await page.locator(`[data-page="${key}.html"]`).click();
      await page.locator(`[data-page="${key}.html"][aria-current]`).waitFor();
      const frame = page.frameLocator('#page-content');
      await frame.locator(`body.page-${key}`).waitFor();
      assert.equal(await page.evaluate(() => window.originalNav === document.querySelector('.pams-primary-nav')),true);
      assert.equal(await frame.locator('.nav').count(),0);
      const after = await page.locator('.pams-primary-nav').boundingBox();
      assert.deepEqual(after,before);
      const navStyle = await page.locator(`[data-page="${key}.html"]`).evaluate(node => {
        const css = getComputedStyle(node);
        return {color:css.color, weight:css.fontWeight, background:getComputedStyle(document.body).backgroundColor};
      });
      assert.deepEqual(navStyle, {color:'rgb(71, 181, 255)',weight:'650',background:'rgb(0, 0, 0)'});
      const contentFrame = page.frames().find(f => f.parentFrame() === page.mainFrame());
      assert.equal(await contentFrame.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      assert.equal(after.width,Math.min(Math.min(width,430)-44,328));
      assert.ok(Math.abs(after.x-(width-after.width)/2)<1);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),false);
      if (key==='ledger') {
        const viewport = await page.locator('#page-content').boundingBox();
        assert.ok(viewport.y + viewport.height > after.y);
        assert.match(await page.locator('.pams-primary-nav').evaluate(node => getComputedStyle(node).backdropFilter), /blur/);
        assert.equal(await frame.locator('.pageHeader').count(),0);
        assert.equal(await frame.locator('[data-ledger-tab="account"], #account-cashflow-section').count(),0);
        for (const tab of ['recent','overview']) {
          const link = frame.locator(`[data-ledger-tab="${tab}"]`);
          const hash = await link.getAttribute('href');
          await link.click();
          await page.waitForFunction(expected => location.hash === expected, hash);
        }
      }
      if (key==='more') assert.equal(await frame.locator('h1').count(),0);
      if (key==='investments') {
        for (const tab of ['analysis','transactions','risk','allocation','holdings']) {
          const link = frame.locator(`[data-invest-tab="${tab}"]`);
          const hash = await link.getAttribute('href');
          await link.click();
          await page.waitForFunction(expected => location.hash === expected, hash);
        }
      }
      if (output) await page.screenshot({path:path.join(output,`shell-${width}-${key}.png`)});
    }
    await page.goBack();
    await page.locator('[data-page="more.html"][aria-current]').waitFor();
    await page.goForward();
    await page.locator('[data-page="investments.html"][aria-current]').waitFor();
    await page.reload();
    await page.locator('[data-page="investments.html"][aria-current]').waitFor();
    await page.frameLocator('#page-content').locator('[data-invest-tab="analysis"]').click();
    await page.waitForFunction(() => location.hash === '#return-trend');
    await page.reload();
    await page.frameLocator('#page-content').locator('[data-invest-tab="analysis"].is-active').waitFor();
    await page.goto(`${base}/app.html?view=ledger.html#account-cashflow-section`);
    await page.waitForFunction(() => location.hash === '#cashflow-workbench');
    await page.reload();
    await page.frameLocator('#page-content').locator('[data-ledger-tab="overview"].is-active').waitFor();
    await page.frameLocator('#page-content').locator('body').evaluate(() => location.assign('more.html'));
    await page.locator('[data-page="more.html"][aria-current]').waitFor();
    assert.equal(await page.frameLocator('#page-content').locator('iframe').count(),0);
    await page.evaluate(() => {
      window.accountRouteNotice = null;
      window.addEventListener('message', event => {
        if (event.data?.type === 'pams-content-route' && event.data.href.includes('/accounts.html')) {
          window.accountRouteNotice = event.data.href;
        }
      });
    });
    delayAccountLoad = true;
    await page.frameLocator('#page-content').locator('body').evaluate(node => {
      const link = document.createElement('a');
      link.href = 'accounts.html?task=balance';
      link.textContent = '兼容跳转测试';
      node.append(link);
    });
    await page.frameLocator('#page-content').getByText('兼容跳转测试', {exact:true}).click();
    await page.waitForFunction(() => window.accountRouteNotice?.endsWith('#balance'));
    assert.equal(new URL(page.url()).searchParams.get('view'), 'more.html');
    releaseAccountLoad(); releaseAccountLoad = null;
    await page.waitForFunction(() => new URL(location).searchParams.get('view') === 'accounts.html');
    await page.goBack();
    await page.frameLocator('#page-content').locator('body.page-more').waitFor();
    assert.equal(new URL(page.url()).searchParams.get('view'), 'more.html');
    await page.frameLocator('#page-content').locator('#moreIssues').click();
    await page.frameLocator('#page-content').locator('body.issues-only').waitFor();
    await page.waitForFunction(() => new URL(location).searchParams.get('content_view') === 'issues');
    await page.reload();
    await page.frameLocator('#page-content').locator('body.issues-only').waitFor();
    assert.equal(new URL(page.url()).searchParams.get('content_view'), 'issues');
    await page.goto(`${base}/data_sources.html?view=issues#dataQuality`);
    await page.frameLocator('#page-content').locator('body.issues-only').waitFor();
    await page.waitForFunction(() => new URL(location).searchParams.get('content_view') === 'issues');
    await page.reload();
    await page.frameLocator('#page-content').locator('body.issues-only').waitFor();
    for (const target of ['accounts.html#overview','data_sources.html','account_import.html']) {
      await page.frameLocator('#page-content').locator('body').evaluate((node,href) => location.assign(href),target);
      const expected = target.startsWith('accounts.html') ? 'more.html' : target.split('#')[0];
      await page.waitForFunction(name => document.querySelector('#page-content').contentWindow.location.pathname.endsWith(name),expected);
      await page.frameLocator('#page-content').locator('body').waitFor();
      if (target.startsWith('accounts.html')) {
        await page.waitForFunction(() => location.hash === '#account-center');
        await page.frameLocator('#page-content').locator('#account-center[open]').waitFor();
        const accountList = page.frameLocator('#page-content').locator('#account-center #accountSyncPanel');
        assert.equal(await accountList.evaluate(node => getComputedStyle(node).backgroundColor), 'rgba(0, 0, 0, 0)');
        assert.equal(await accountList.evaluate(node => getComputedStyle(node).borderRadius), '0px');
      }
      assert.equal(await page.locator('.pams-primary-nav').count(),1);
      assert.equal(await page.frameLocator('#page-content').locator('iframe').count(),0);
    }
    await page.frameLocator('#page-content').locator('body').evaluate(() => location.assign('http://unavailable.invalid/'));
    await page.waitForFunction(() => !document.querySelector('#page-content').contentDocument);
    await page.locator('[data-page="ledger.html"]').click();
    await page.locator('[data-page="ledger.html"][aria-current]').waitFor();
  }
  workflowFixtures = true;
  await page.goto(`${base}/accounts.html?account=fixture#overview`);
  const workflow = page.frameLocator('#page-content');
  await workflow.getByRole('button',{name:'示例储蓄卡',exact:true}).waitFor();
  await page.waitForFunction(() => new URL(location).searchParams.get('view') === 'more.html' && location.hash === '#account-center');
  await workflow.locator('#moreIssues').click();
  await workflow.getByRole('link',{name:'核对 示例储蓄卡 ›'}).click();
  const review = workflow.getByRole('dialog');
  await review.getByText('示例储蓄卡 · 待核查').waitFor();
  await review.getByRole('link',{name:'余额核准'}).click();
  await workflow.getByRole('heading',{name:'余额核准',exact:true}).waitFor();
  await workflow.locator('.accountTabs').waitFor({state:'hidden'});
  await workflow.locator('#accountTaskBack').click();
  await workflow.getByRole('button',{name:'示例储蓄卡',exact:true}).click();
  await review.getByRole('link',{name:'导入资料'}).click();
  await workflow.getByRole('button',{name:'导入配置',exact:true}).waitFor();
  assert.equal(await workflow.locator('#account').inputValue(),'fixture');
  const importFrame = await page.locator('#page-content').boundingBox();
  const importNav = await page.locator('.pams-primary-nav').boundingBox();
  assert.ok(importFrame.y + importFrame.height < importNav.y);
  await workflow.getByRole('button',{name:'导入配置',exact:true}).click();
  await workflow.getByRole('heading',{name:'导入配置',exact:true}).waitFor();
  await workflow.getByText('示例模板',{exact:true}).waitFor();
  await workflow.locator('#accountTaskBack').click();
  await workflow.locator('#account option').first().waitFor({state:'attached'});
  assert.equal(await workflow.locator('#account').inputValue(),'fixture');
  await workflow.locator('#flow-back').click();
  await workflow.getByRole('button',{name:'示例储蓄卡',exact:true}).waitFor();
  await page.waitForFunction(() => new URL(location).searchParams.get('account') === 'fixture');
  await page.locator('[data-page="ledger.html"]').click();
  await workflow.getByRole('link',{name:'查看内部划转'}).click();
  await workflow.getByRole('heading',{name:'划转汇总'}).waitFor();
  await workflow.getByRole('heading',{name:'已确认'}).waitFor();
  await workflow.getByRole('heading',{name:'待配对'}).waitFor();
  await page.waitForFunction(() => location.hash === '#transfers');
  await workflow.getByRole('link',{name:'流水',exact:true}).click();
  await workflow.locator('#recentSearchToggle').click();
  const searchPanel = await workflow.locator('.appBottomSheetPanel').boundingBox();
  const ledgerNav = await page.locator('.pams-primary-nav').boundingBox();
  assert.ok(searchPanel.y + searchPanel.height < ledgerNav.y);
  await workflow.getByRole('button',{name:'关闭',exact:true}).click();
  const lastRow = workflow.locator('#recentRows .ledgerListRow').last();
  await lastRow.waitFor();
  assert.match(await lastRow.innerText(), /示例流水 12/);
  await workflow.locator('.appBottomSheetPanel').waitFor({state:'hidden'});
  await lastRow.evaluate(row => {
    row.scrollIntoView({block:'end', behavior:'instant'});
    for (let node = row.parentElement; node; node = node.parentElement) {
      if (node.scrollHeight > node.clientHeight && /auto|scroll/.test(getComputedStyle(node).overflowY)) {
        node.scrollTop = node.scrollHeight;
      }
    }
    window.scrollTo({top:document.scrollingElement.scrollHeight, behavior:'instant'});
  });
  const lastRowBox = await lastRow.boundingBox();
  assert.ok(lastRowBox.y + lastRowBox.height < ledgerNav.y, JSON.stringify({lastRowBox, ledgerNav}));
  await session.send('Emulation.setEmulatedMedia', {
    features: [{name:'prefers-reduced-transparency', value:'reduce'}],
  });
  assert.equal(await page.locator('.pams-primary-nav').evaluate(node => getComputedStyle(node).backdropFilter), 'none');
  await session.send('Emulation.setEmulatedMedia', {features: [
    {name:'prefers-reduced-transparency', value:'no-preference'},
    {name:'prefers-reduced-motion', value:'no-preference'},
  ]});
  const primaryNav = page.locator('.pams-primary-nav');
  assert.equal(await primaryNav.evaluate(node => getComputedStyle(node).backdropFilter), 'blur(12px) saturate(1.45)');
  await page.evaluate(() => {
    window.navVibrations = [];
    navigator.vibrate = duration => { window.navVibrations.push(duration); return true; };
    window.navSelectionMoved = false;
    document.querySelector('.pams-primary-nav').addEventListener('transitionrun', event => {
      if (event.pseudoElement === '::before' && event.propertyName === 'transform') {
        window.navSelectionMoved = true;
      }
    });
  });
  const moreLink = primaryNav.locator('[data-page="more.html"]');
  await moreLink.click();
  await page.waitForFunction(() => window.navSelectionMoved);
  await moreLink.click();
  assert.deepEqual(await page.evaluate(() => window.navVibrations), [10]);
  await session.send('Emulation.setEmulatedMedia', {features:[{name:'prefers-reduced-motion', value:'reduce'}]});
  await primaryNav.locator('[data-page="ledger.html"]').press('Enter');
  await primaryNav.locator('[data-page="ledger.html"][aria-current="page"]').waitFor();
  assert.equal(await primaryNav.evaluate(node => getComputedStyle(node, '::before').transitionDuration), '0s');
  assert.deepEqual(await page.evaluate(() => window.navVibrations), [10]);
  workflowFixtures = false;
  // A desktop viewport alone reports zero insets. Emulate the phone's
  // obstruction so the test catches an iframe starting behind the status bar.
  await session.send('Emulation.setTouchEmulationEnabled', {enabled:true});
  const firstControls = {
    investments: '[data-invest-tab="analysis"]',
    ledger: '[data-ledger-tab="recent"]',
    more: '#moreAdvanced > summary',
    accounts: '#accountTaskBack',
    data_sources: '.issueDiagnostics',
    account_import: '#flow-back',
  };
  for (const {width,height,top,bottom,left,right} of [
    {width:320,height:568,top:0,bottom:0,left:0,right:0},
    {width:390,height:844,top:59,bottom:34,left:0,right:0},
    {width:844,height:390,top:0,bottom:21,left:59,right:0},
  ]) {
    await session.send('Emulation.setDeviceMetricsOverride', {width,height,deviceScaleFactor:1,mobile:true});
    await session.send('Emulation.setSafeAreaInsetsOverride', {insets:{top,bottom,left,right}});
    for (const [key,selector] of Object.entries(firstControls)) {
      const contentView = key === "data_sources" ? "&content_view=issues" : key === "accounts" ? "&task=balance" : "";
      await page.goto(`${base}/app.html?view=${key}.html${contentView}`);
      const frame = page.frameLocator('#page-content');
      await frame.locator('html.pams-content').waitFor({state:'attached'});
      const box = await page.locator('#page-content').boundingBox();
      const safeWidth = Math.min(430,width-left-right);
      assert.equal(box.width,safeWidth,key);
      assert.equal(box.x,left+(width-left-right-safeWidth)/2,key);
      assert.equal(box.y,top+8,key);
      const control = frame.locator(selector);
      await control.click({trial:true,timeout:5000});
      const controlBox = await control.boundingBox();
      assert.ok(controlBox && controlBox.y >= box.y,key);
      const primary = await page.locator('.pams-primary-nav a').first().boundingBox();
      assert.ok(primary.y+primary.height <= height-bottom,key);
      const contentFrame = page.frames().find(f => f.parentFrame() === page.mainFrame());
      assert.equal(await contentFrame.evaluate(() => document.documentElement.scrollWidth > innerWidth),false,key);
    }
    await page.goto(`${base}/investments.html?content=1`);
    assert.equal(await page.locator('meta[name="viewport"]').getAttribute('content'),'width=device-width, initial-scale=1');
    assert.equal(await page.locator('body').evaluate(node => getComputedStyle(node).paddingTop),'8px');
    await page.locator('[data-invest-tab="analysis"]').click();
    await page.waitForFunction(() => location.hash === '#return-trend');
  }
  assert.deepEqual(failures,[]);
  assert.deepEqual(missingAssets,[]);
  console.log('Shell: four desktop widths, six pages at three touch sizes, safe areas, tabs and history passed with fixed API fixtures.');
} catch (error) {
  console.error(JSON.stringify({url:page.url(),frames:page.frames().map(f=>f.url()),failures}));
  throw error;
} finally { releaseAccountLoad?.(); await browser.close(); await new Promise(resolve=>server.close(resolve)); }
