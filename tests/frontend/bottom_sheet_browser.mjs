import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {pathToFileURL} from 'node:url';
import path from 'node:path';

const {chromium} = await import(pathToFileURL(process.env.PAMS_FRONTEND_PLAYWRIGHT_MODULE).href);
const root = path.resolve(process.argv[2]);
const browser = await chromium.launch({headless: true, channel: process.env.PAMS_FRONTEND_PLAYWRIGHT_CHANNEL || undefined});
const page = await browser.newPage({viewport: {width: 390, height: 844}});
const errors = [];
page.on('pageerror', error => errors.push(error.message));
await page.route('**/*', async route => {
  const url = new URL(route.request().url());
  assert.equal(url.origin, 'http://ui-fixture.invalid');
  assert.equal(route.request().method(), 'GET');
  if (url.pathname === '/') return route.fulfill({contentType: 'text/html', body: `<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><title>固定组件测试数据</title>
<style>[hidden]{display:none!important}.appBottomSheet{position:fixed;inset:0}.appBottomSheetScrim{position:absolute;inset:0;background:#0008}.appBottomSheetPanel{position:absolute;inset:30% 0 0;background:#222;color:#fff;padding:16px}.appBottomSheetBody{display:grid;gap:12px}button,input{min-height:44px}body.sheet-open{overflow:hidden}</style>
<main id="background"><button id="launch">打开详情</button><button id="other">其他入口</button></main>
<div id="preserved" inert aria-hidden="false">原有禁用区域</div>
<script type="module">
import {createBottomSheet} from '/assets/js/components/bottom_sheet.js';
window.sheet = createBottomSheet();
window.modalHtml = '<label>筛选<input autofocus id="first"></label><button hidden>隐藏</button><button disabled>禁用</button><button id="last">最后操作</button>';
document.getElementById('launch').onclick = () => window.sheet.open('固定测试详情', window.modalHtml);
</script></html>`});
  assert.equal(url.pathname, '/assets/js/components/bottom_sheet.js');
  return route.fulfill({contentType: 'text/javascript', body: await readFile(path.join(root, url.pathname), 'utf8')});
});
try {
  await page.goto('http://ui-fixture.invalid/');
  await page.waitForFunction(() => Boolean(window.sheet));
  await page.locator('#launch').click();
  assert.equal(await page.locator('#first').evaluate(el => el === document.activeElement), true);
  assert.equal(await page.locator('[role="dialog"]').getAttribute('aria-labelledby'), 'appBottomSheetTitle');
  assert.equal(await page.locator('#background').evaluate(el => el.inert), true);
  await page.locator('#last').focus(); await page.keyboard.press('Tab');
  assert.equal(await page.locator('.appBottomSheetClose').evaluate(el => el === document.activeElement), true);
  await page.keyboard.press('Shift+Tab');
  assert.equal(await page.locator('#last').evaluate(el => el === document.activeElement), true);
  await page.evaluate(() => { const button = document.createElement('button'); button.id = 'late'; button.textContent = '后到入口'; document.body.append(button); });
  await page.waitForFunction(() => document.getElementById('late').inert);
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#appBottomSheet').count(), 0);
  assert.equal(await page.locator('#launch').evaluate(el => el === document.activeElement), true);
  assert.equal(await page.locator('#background').evaluate(el => el.inert), false);
  assert.equal(await page.locator('#late').evaluate(el => el.inert), false);
  assert.equal(await page.locator('#preserved').evaluate(el => el.inert), true);
  assert.equal(await page.locator('#preserved').getAttribute('aria-hidden'), 'false');
  assert.equal(await page.locator('body').evaluate(el => el.classList.contains('sheet-open')), false);
  for (let i = 0; i < 10; i++) {
    await page.locator('#launch').click();
    await page.evaluate(() => sheet.open('替换详情', modalHtml));
    assert.equal(await page.locator('#appBottomSheet').count(), 1);
    await page.locator('.appBottomSheetClose').click();
    assert.equal(await page.locator('#appBottomSheet').count(), 0);
    assert.equal(await page.locator('#launch').evaluate(el => el === document.activeElement), true);
  }
  await page.evaluate(() => document.body.classList.add('sheet-open'));
  await page.locator('#launch').click(); await page.keyboard.press('Escape');
  assert.equal(await page.locator('body').evaluate(el => el.classList.contains('sheet-open')), true);
  await page.evaluate(() => document.body.classList.remove('sheet-open'));
  await page.locator('#launch').click();
  await page.evaluate(() => document.getElementById('launch').remove());
  await page.keyboard.press('Escape');
  assert.equal(await page.evaluate(() => document.activeElement === document.body), true);
  await page.evaluate(() => { sheet.open('释放测试', modalHtml); sheet.dispose(); });
  assert.equal(await page.locator('#appBottomSheet').count(), 0);
  assert.equal(await page.locator('#background').evaluate(el => el.inert), false);
  assert.deepEqual(errors, []);
  console.log('Bottom sheet: focus entry/trap/return, background state, dynamic content, 10 repeated cycles and disposal passed.');
} finally { await browser.close(); }
