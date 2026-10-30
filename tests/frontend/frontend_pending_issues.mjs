import assert from 'node:assert/strict';
import { readFile, mkdir } from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const { chromium } = await import(pathToFileURL(process.env.PAMS_FRONTEND_PLAYWRIGHT_MODULE).href);
const browser = await chromium.launch({ headless: true, channel: process.env.PAMS_FRONTEND_PLAYWRIGHT_CHANNEL || undefined });
const page = await browser.newPage();
const root = path.resolve('frontend/public');
const output = process.env.PAMS_FRONTEND_ARTIFACT_DIR;
if (output) await mkdir(output, { recursive: true });
const errors = [];
page.on('pageerror', error => errors.push(error.message));
await page.route('**/*', async route => {
  const url = new URL(route.request().url());
  if (url.origin !== 'http://issues.invalid') return route.abort();
  if (url.pathname === '/api/portfolio/data-quality') return route.fulfill({json:{data_quality:{summary:{status:'warning',warning_count:2,error_count:0},checks:[
    {id:'account_balance_coverage',status:'attention',details:{missing_accounts:['example-a','example-b']}},
    {id:'valuation_series_freshness',status:'attention',details:{valuation_series_latest_as_of:'2026-06-18',investment_positions_latest_as_of:'2026-10-29'}}
  ]}}});
  if (url.pathname === '/api/accounts/overview') return route.fulfill({json:{accounts:[{account_code:'example-a',display_name:'示例账户甲'},{account_code:'example-b',display_name:'示例账户乙'}]}});
  const file = path.resolve(root, '.' + url.pathname);
  assert.ok(file.startsWith(root + path.sep));
  try { return await route.fulfill({contentType:file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'text/html',body:await readFile(file)}); }
  catch { return route.abort(); }
});
try {
  for (const width of [320, 390, 1280]) {
    await page.setViewportSize({width,height:844});
    await page.goto('http://issues.invalid/data_sources.html?view=issues#dataQuality');
    await page.getByText('部分账户缺少余额确认',{exact:true}).waitFor();
    assert.equal(await page.locator('#issuesContent .issueRow').count(),2);
    assert.equal(await page.locator('#sourceList').isVisible(),false);
    assert.equal(await page.locator('#syncMatrix').isVisible(),false);
    assert.match(await page.locator('#issuesContent').innerText(),/示例账户甲、示例账户乙/);
    assert.match(await page.locator('#issuesContent').innerText(),/2026-06-18.*2026-10-29/);
    assert.equal(await page.getByRole('link',{name:'核对账户 ›'}).getAttribute('href'),'accounts.html#overview');
    assert.equal(await page.getByRole('link',{name:'数据诊断 ›'}).getAttribute('href'),'data_sources.html');
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth > innerWidth),false);
    if(output) await page.screenshot({path:path.join(output,`${width}-pending.png`),fullPage:true});
  }
  assert.deepEqual(errors,[]);
  console.log('Pending issues UI OK: 320/390/1280, two fixture warnings, no production requests');
} finally { await browser.close(); }
