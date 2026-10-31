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
const server = createServer(async (req,res) => {
  const url = new URL(req.url,'http://localhost');
  if (/\/(investments|ledger|more|accounts|data_sources|account_import)\.html$/.test(url.pathname) && url.searchParams.get('content') !== '1') {
    res.writeHead(302,{location:`/app.html?view=${url.pathname.slice(1)}&${url.searchParams}`});res.end();return;
  }
  const file=path.resolve(root,'.'+url.pathname);
  try {
    assert.ok(file.startsWith(root+path.sep));
    const body=await readFile(file);
    res.writeHead(200,{'content-type':({'.html':'text/html; charset=utf-8','.js':'application/javascript; charset=utf-8','.css':'text/css; charset=utf-8'})[path.extname(file)] || 'application/octet-stream'});res.end(body);
  } catch { res.writeHead(503,{'content-type':'application/json'});res.end('{"detail":"Fixed unavailable fixture"}'); }
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const base=`http://127.0.0.1:${server.address().port}`;
await context.route('**/*', async route => {
  const url = new URL(route.request().url());
  if(url.origin==='http://unavailable.invalid')return route.fulfill({contentType:'text/html',body:'<!doctype html><title>Isolated external document</title><p>Fixed test document</p>'});
  if(url.origin===base)return route.continue();
  if(url.hostname==='127.0.0.1')return route.fulfill({status:503,contentType:'application/json',body:'{"detail":"Fixed unavailable fixture"}'});
  return route.abort();
});
const page = await context.newPage();
const failures = [];
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
      assert.equal(after.width,Math.min(width,430));
      assert.ok(Math.abs(after.x-(width-after.width)/2)<1);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),false);
      if (key==='ledger') {
        assert.equal(await frame.locator('.pageHeader').count(),0);
        for (const tab of ['recent','account','overview']) await frame.locator(`[data-ledger-tab="${tab}"]`).click();
      }
      if (key==='more') assert.equal(await frame.locator('h1').count(),0);
      if (key==='investments') {
        for (const tab of ['analysis','transactions','risk','allocation','holdings']) await frame.locator(`[data-invest-tab="${tab}"]`).click();
      }
      if (output) await page.screenshot({path:path.join(output,`shell-${width}-${key}.png`)});
    }
    await page.goBack();
    await page.locator('[data-page="more.html"][aria-current]').waitFor();
    await page.goForward();
    await page.locator('[data-page="investments.html"][aria-current]').waitFor();
    await page.reload();
    await page.locator('[data-page="investments.html"][aria-current]').waitFor();
    await page.frameLocator('#page-content').locator('body').evaluate(() => location.assign('more.html'));
    await page.locator('[data-page="more.html"][aria-current]').waitFor();
    assert.equal(await page.frameLocator('#page-content').locator('iframe').count(),0);
    for (const target of ['accounts.html#overview','data_sources.html','account_import.html']) {
      await page.frameLocator('#page-content').locator('body').evaluate((node,href) => location.assign(href),target);
      await page.waitForFunction(expected => document.querySelector('#page-content').contentWindow.location.pathname.endsWith(expected),target.split('#')[0]);
      await page.frameLocator('#page-content').locator('body').waitFor();
      assert.equal(await page.locator('.pams-primary-nav').count(),1);
      assert.equal(await page.frameLocator('#page-content').locator('iframe').count(),0);
    }
    await page.frameLocator('#page-content').locator('body').evaluate(() => location.assign('http://unavailable.invalid/'));
    await page.waitForFunction(() => !document.querySelector('#page-content').contentDocument);
    await page.locator('[data-page="ledger.html"]').click();
    await page.locator('[data-page="ledger.html"][aria-current]').waitFor();
  }
  assert.deepEqual(failures,[]);
  console.log('Shell: four widths, persistent navigation, tabs, history and reload passed; APIs are explicit 503 fixtures.');
} catch (error) {
  console.error(JSON.stringify({url:page.url(),frames:page.frames().map(f=>f.url()),failures}));
  throw error;
} finally { await browser.close(); await new Promise(resolve=>server.close(resolve)); }
