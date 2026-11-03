import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
const {chromium}=await import(pathToFileURL(process.env.PAMS_FRONTEND_PLAYWRIGHT_MODULE).href);
const browser=await chromium.launch({headless:true});
const page=await browser.newPage({viewport:{width:390,height:844}}), requests=[], errors=[];
page.on('request',request=>{if(request.url().includes('/api/')) requests.push(request.url());});
page.on('pageerror',error=>errors.push(error.message));
try {
 for(const [account,name,content] of [['ccb_test_1234','statement.csv',process.env.PAMS_IMPORT_TEST_DOCUMENT],['tonghuashun','table.xls',process.env.PAMS_IMPORT_TEST_TRADE]]) {
  await page.goto(process.env.PAMS_IMPORT_TEST_URL+'/account_import.html');
  await page.waitForFunction(()=>!document.querySelector('#workspace').hidden&&!document.querySelector('#workspace').inert);
  await page.locator('#account').selectOption(account);
  await page.locator('#files').setInputFiles({name,mimeType:'application/octet-stream',buffer:Buffer.from(content,'base64')});
  await page.waitForFunction(()=>document.querySelector('#batch-meta').textContent.includes('草稿'));
  await page.locator('#next').click();await page.locator('#review').waitFor({state:'visible'});
  assert.equal(await page.locator('#publish').isDisabled(),true);
  await page.locator('#preview-rows button').first().click();await page.locator('#editor').waitFor({state:'visible'});
  await page.locator('#record-form [type=submit]').click();await page.locator('#editor').waitFor({state:'hidden'});
  await page.waitForFunction(()=>!document.querySelector('#publish').disabled);
  await page.locator('#publish').click();await page.waitForFunction(()=>document.querySelector('#notice').textContent.includes('已发布'));
 }
 assert.ok(requests.every(url=>!url.includes('/batches')));assert.deepEqual(errors,[]);
} finally {await browser.close();}
