"""Offline contracts. Optional real-browser coverage uses only intercepted requests."""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "frontend" / "public"
HTML = (PUBLIC / "account_import.html").read_text(encoding="utf-8")
JS = (PUBLIC / "assets/js/account_import.js").read_text(encoding="utf-8")
CSS = (PUBLIC / "assets/css/account-import.css").read_text(encoding="utf-8")


def node(script: str, timeout: int = 30) -> str:
    executable = shutil.which("node")
    if not executable:
        pytest.skip("Node.js is required for offline JavaScript contracts")
    result = subprocess.run(
        [executable, "--input-type=module"], check=False, input=script, text=True,
        encoding="utf-8", capture_output=True, cwd=ROOT, timeout=timeout,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def module_test(code: str) -> None:
    node("import assert from 'node:assert/strict';\n"
         f"const m = await import('data:text/javascript;base64,' + Buffer.from({json.dumps(JS)}).toString('base64'));\n"
         + code)


def test_isolated_two_step_shell_and_existing_bank_link() -> None:
    assert 'id="step-one"' in HTML and 'id="step-two"' in HTML
    assert "填写 / 识别" in HTML and "核对发布" in HTML
    assert 'id="review" aria-label="核对发布" hidden' in HTML
    assert HTML.index('id="file-list"') < HTML.index('id="ack-all"')
    assert 'type="module" src="assets/js/account_import.js?v=' in HTML
    assert 'aria-label="关闭记录编辑器"' in HTML
    assert "app-shell" in HTML and '<dialog id="editor"' in HTML
    more = (PUBLIC / "more.html").read_text(encoding="utf-8")
    assert more.count('href="account_import.html"') == 1
    assert 'href="accounts.html#sources"' in more
    assert re.findall(r'<a data-nav[^>]*href="([^"]+)"', HTML) == [
        "investments.html", "ledger.html", "more.html",
    ]
    assert "430px" in CSS and "safe-area-inset-bottom" in CSS
    assert "--import-keyboard" in CSS and "visualViewport" in JS
    assert "font-size: 16px" in CSS and "min-height: 44px" in CSS
    assert "overflow-wrap: anywhere" in CSS
    assert "hero" not in HTML.lower() and 'class="card' not in HTML


@pytest.mark.parametrize("caps, expected", [
    (None, "unavailable"), ({}, "unavailable"),
    ({"configured": False, "authenticated": True, "schema_ready": True}, "unavailable"),
    ({"configured": True, "authenticated": False, "schema_ready": False}, "locked"),
    ({"configured": True, "authenticated": True, "schema_ready": False}, "unavailable"),
    ({"configured": True, "authenticated": False, "schema_ready": True}, "locked"),
    ({"configured": True, "authenticated": True, "schema_ready": True}, "ready"),
])
def test_missing_configuration_schema_and_authentication(caps: dict | None, expected: str) -> None:
    module_test(f"assert.equal(m.accessState({json.dumps(caps)}), {json.dumps(expected)});")


def test_decimal_values_remain_exact_strings_and_unknowns_stay_empty() -> None:
    module_test("""
      for (const text of ['12345678901234567890.0100', '-0.0010', '0', '.50', ''])
        assert.equal(m.decimalString(text), text);
      assert.equal(m.decimalString(null), '');
      for (const invalid of ['1e8', 'NaN', 'Infinity', '1,234.56', '<img>', '1.2.3'])
        assert.throws(() => m.decimalString(invalid));
    """)
    assert "parseFloat" not in JS and "parseInt" not in JS
    assert "toFixed" not in JS and "new Date" not in JS
    assert "fields.tax_amount = null" in JS
    assert "total_charges" in JS and "手续费与税分列" in JS
    assert "field('value-fields', 'counter_account_code', state.counterAccounts" in JS


def test_upload_formats_match_parser_and_include_tsv() -> None:
    assert re.search(r'id="files"[^>]*accept="([^"]+)"', HTML).group(1) == ".csv,.tsv,.xls,.xlsx,.png,.jpg,.jpeg"
    assert "CSV / TSV / 同花顺 XLS / XLSX / PNG / JPG / JPEG" in HTML
    module_test("""
      for (const ext of ['csv','tsv','xls','xlsx','png','jpg','jpeg']) {
        assert.equal(m.fileFormat('报表.'+ext),ext);
        assert.equal(m.fileFormat('报表.'+ext.toUpperCase()),ext);
      }
      for (const name of ['报表.pdf','报表.webp','无扩展名','报表.csv.exe','报表.tsv '])
        assert.throws(()=>m.fileFormat(name), /仅支持 CSV、TSV、同花顺 XLS、XLSX、PNG、JPG、JPEG/);
    """)


def test_missing_import_type_uses_matching_server_preview_not_symbol_prefixes() -> None:
    assert "state.preview?.batch_id === state.batch.batch_id" in JS
    assert "state.preview?.revision === state.batch.revision" in JS
    assert "!state.buffer.asset_type && checked?.fields?.asset_type" in JS
    assert "state.buffer.asset_type = checked.fields.asset_type" in JS


def test_preview_business_fields_are_translated_without_financial_conversion() -> None:
    module_test("""
      const source={id:7,asset_id:'internal',account_code:'ths',as_of:'2026-10-20',
        current_price:'1.0000',buy_price:'12.000001',asset_liability:'负债',major_category:'现金',
        minor_category:'现金/活期',market_value:'-123456789.0100',cost:null,
        pnl:'1.2345',created_at:'private timestamp',updated_at:'private timestamp'};
      const before=JSON.stringify(source), rows=m.previewFields(source);
      const labels=Object.fromEntries(rows.map(row=>[row.key,row.label]));
      assert.deepEqual(labels,{account_code:'账户',as_of:'快照日期',market_value:'市值',
        current_price:'现价',buy_price:'成本均价',major_category:'资产大类',minor_category:'资产子类',asset_liability:'资产/负债'});
      assert.equal(rows.find(row=>row.key==='market_value').value,'-123456789.0100');
      assert.ok(!rows.some(row=>row.key==='cost'));
      assert.equal(JSON.stringify(source),before);
      assert.deepEqual(m.previewFields(null),[]);
    """)
    assert "JSON.stringify(object, null, 2)" in JS
    assert "el('summary', '完整服务端记录')" in JS


def test_preview_omits_empty_values_but_preserves_explicit_zero() -> None:
    module_test("""
      const source=Object.freeze({quantity:0,price:'0.0000',net_cash:'-0.00',cost:null,
        market_value:'',counter_account_code:'  ',note:undefined,business_date:''});
      const rows=m.previewFields(source);
      assert.deepEqual(rows.map(({key,value})=>[key,value]),[
        ['quantity',0],['price','0.0000'],['net_cash','-0.00']]);
      assert.equal(source.cost,null); assert.equal(source.market_value,'');
      assert.equal(source.counter_account_code,'  '); assert.ok(Object.hasOwn(source,'note'));
    """)


def test_master_asset_types_are_only_grouped_for_display() -> None:
    module_test("""
      for (const type of ['fund','etf','mutual_fund']) assert.equal(m.displayAssetType(type),'fund');
      for (const type of ['stock','equity','listed_equity']) assert.equal(m.displayAssetType(type),'stock');
      assert.equal(m.displayAssetType('other'),'other');
      assert.equal(m.displayAssetType('基金'),'基金');
    """)
    assert "asset_type: asset.asset_type" in JS


def test_trade_actions_are_account_context_driven_not_symbol_rules() -> None:
    assert "row?.source?.trade_actions || account()?.trade_actions" in JS
    assert "tradeActions?.[displayAssetType(f.asset_type)]" in JS
    assert "serverActions ??" in JS
    assert "option.disabled = key === 'action'" in JS
    assert "source_asset_type" not in JS


def test_only_401_resets_auth_and_record_kind_is_fixed_to_batch() -> None:
    assert "if (error.status === 401)" in JS
    assert "error.status === 401 || error.status === 403" not in JS
    assert "error.status === 403 && error.code === 'publish_disabled'" in JS
    assert "state.caps = { ...state.caps, publish_enabled: false }" in JS
    assert "field('identity-fields', 'kind', [state.batch.kind]).disabled = true" in JS
    assert "fields.kind = state.batch.kind" in JS


def test_publish_requires_current_clean_server_preview_and_explicit_capability() -> None:
    module_test("""
      const caps = {publish_enabled:true};
      const batch = {batch_id:'b',revision:3,status:'draft'};
      const preview = {batch_id:'b',revision:3,preview_token:'private',can_publish:true,
        counts:{blocked:0,pending:0},rows:[]};
      assert.equal(m.publishable(caps,batch,preview), true);
      assert.equal(m.publishable({},batch,preview), false);
      assert.equal(m.publishable({publish_enabled:false},batch,preview), false);
      assert.equal(m.publishable(caps,batch,preview,true), false);
      for (const override of [{revision:2},{batch_id:'old'},{preview_token:''},{can_publish:false},
        {counts:{blocked:1,pending:0}},{counts:{blocked:0,pending:1}},
        {rows:[{status:'blocked'}]},{rows:[{status:'pending'}]}])
        assert.equal(m.publishable(caps,batch,{...preview,...override}), false);
      assert.equal(m.publishable(caps,{...batch,status:'published'},preview), false);
    """)


def test_only_private_same_origin_import_requests_and_raw_file_uploads() -> None:
    module_test("""
      const calls=[];
      const client=m.createImportClient(async (...args) => {
        calls.push(args); return {ok:true,json:async()=>({revision:1})};
      });
      await client('/unlock','POST',{token:'secret'});
      const file = new Blob(['raw,csv'], {type:'text/csv'});
      await client('/batches/b/files?filename=a%20b.csv','POST',file,true);
      assert.equal(calls[1][1].body,file);
      for (const [url, options] of calls) {
        assert.ok(url.startsWith('/api/account-import/'));
        assert.equal(options.credentials,'same-origin');
        assert.equal(options.cache,'no-store');
        assert.equal(options.redirect,'error');
        assert.equal(options.headers.Authorization,undefined);
      }
      assert.equal(calls[0][1].body, JSON.stringify({token:'secret'}));
      for (const path of ['https://other.invalid', '//other.invalid', '/../facts'])
        await assert.rejects(client(path));
      const stale=m.createImportClient(async()=>({ok:false,status:409,json:async()=>({detail:'conflict'})}));
      await assert.rejects(stale('/batches/b','PUT',{}), e=>e.status===409);
    """)
    assert "FormData" not in JS
    assert "localStorage" not in JS and "sessionStorage" not in JS
    assert not re.search(r"\b(innerHTML|outerHTML|eval|insertAdjacentHTML)\b", JS)
    assert "source.raw_text" in JS and "encodeURIComponent(file.file_id)" in JS
    assert "referrerPolicy = 'no-referrer'" in JS
    assert JS.count("/publish`, 'POST'") == 1
    assert "$('publish').addEventListener('click'" in JS
    save = JS.split("async function saveDraft()", 1)[1].split("async function upload", 1)[0]
    assert "'PUT'" in save and "/publish" not in save
    assert "possible_duplicate" in JS and "核对为不同交易" in JS
    assert "state.suspended" in JS and "error.status === 409" in JS


def test_mapping_is_explicit_private_and_uses_batch_kind_and_exact_headers() -> None:
    module_test("""
      const calls=[];
      const client=m.createImportClient(async (...args)=>{
        calls.push(args); return {ok:true,json:async()=>({revision:2})};
      });
      const mapping={business_date:'  日期  ',source_ref:'流水号',gross_amount:'金额<img src=x>'};
      await client('/batches/b/files/f/parse','POST',{kind:'trade',mapping});
      assert.equal(calls[0][0],'/api/account-import/batches/b/files/f/parse');
      assert.deepEqual(JSON.parse(calls[0][1].body),{kind:'trade',mapping});
    """)
    assert "source_ref: '成交/流水号'" in JS
    assert "file.metadata?.headers" in JS and "file.metadata?.suggested_mapping" in JS
    assert "new Option(header, header)" in JS
    mapping = JS.split("function mappingEditor(file)", 1)[1].split("async function history", 1)[0]
    assert "window.confirm" not in mapping
    assert "if (!draft.open) return node" in mapping and "if (!draft.confirm)" in mapping
    assert "{ kind: state.batch.kind, mapping: draft.values }" in mapping
    assert "useBatch(await api(batchPath))" in mapping
    assert "/publish" not in mapping and "/templates" not in mapping
    assert "发布尚未启用" in HTML and "暂存表尚未就绪" in JS


def test_holding_cash_uses_balance_without_security_fields_or_local_totals() -> None:
    assert 'id="asset-lookup"' in HTML
    assert "holding ? ['stock', 'fund', 'cash']" in JS
    assert "$('asset-lookup').hidden = cash" in JS
    assert "if (holding && !cash) field('value-fields', 'cost_basis_type'" in JS
    assert "if (fields.kind === 'holding' && fields.asset_type === 'cash') { fields.action = 'balance'; delete fields.asset_id; }" in JS
    assert "'asset_id', 'symbol', 'name', 'quantity', 'price', 'market_value', 'cost', 'cost_basis_type', 'net_cash'" in JS
    assert "new Date" not in JS and "parseFloat" not in JS and "toFixed" not in JS


BROWSER = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
const {chromium} = await import(pathToFileURL(process.env.PAMS_FRONTEND_PLAYWRIGHT_MODULE).href);
const browser = await chromium.launch({headless:true,channel:process.env.PAMS_FRONTEND_PLAYWRIGHT_CHANNEL||undefined});
const publicRoot = path.resolve('frontend/public');
const allCalls=[], errors=[];
let caps, batch, previewMode, conflict, publishes, uploads, parseBodies, parseFailure, reloadFailure, saveFailure, publishFailure;
const reset=()=>{
  caps={configured:true,authenticated:true,schema_ready:true,publish_enabled:true,ocr_available:true,limits:{max_files:3,max_file_bytes:2048}};
  batch=null; previewMode='ready'; conflict=false; publishes=0; uploads=[]; parseBodies=[]; parseFailure=0; reloadFailure=false; saveFailure=null; publishFailure=null;
};
const context=await browser.newContext();
await context.route('**/*',async route=>{
  const request=route.request(), url=new URL(request.url()), p=url.pathname;
  const send=(data,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
  assert.equal(url.origin,'http://account-import.invalid');
  if(p.startsWith('/api/')) {
    assert.ok(p.startsWith('/api/account-import/')); allCalls.push([p,request.method()]);
    const method=request.method(), body=method==='GET'?null:request.postData();
    if(p.endsWith('/capabilities')) return send(caps);
    if(p.endsWith('/unlock')) { assert.equal(JSON.parse(body).token,'secret'); caps.authenticated=true; caps.schema_ready=true; return send({}); }
    assert.equal(caps.authenticated,true);
    if(p.endsWith('/context')) return send({accounts:[
      {account_code:'ths',label:'同花顺投资账户',profile:'tonghuashun',currency:'CNY',default_asset_type:'stock',trade_actions:{stock:['buy','sell','dividend','fee'],fund:['buy','sell','dividend','fee']}},
      {account_code:'licai',label:'腾讯理财通账户',profile:'tencent_licaitong',currency:'CNY',default_asset_type:'fund',trade_actions:{stock:['buy','sell','dividend','fee'],fund:['subscribe','redeem','dividend','fee']}}],
      profiles:['tonghuashun','tencent_licaitong'],counter_accounts:[{account_code:'bank',display_name:'银行卡账户',base_currency:'CNY'}]});
    if(p.endsWith('/assets')) return send({rows:[{asset_id:'a',symbol:'600001',name:'长名称基金<img src=x onerror=alert(1)>',asset_type:'fund',source_asset_type:'基金',currency:'CNY'}]});
    if(p==='/api/account-import/batches' && method==='GET') return send({rows:batch?[batch]:[]});
    if(p==='/api/account-import/batches') {batch={...JSON.parse(body),batch_id:'b',revision:1,status:'draft',files:[],rows:[]}; return send(batch);}
    if(p.endsWith('/files') && method==='POST') {
      assert.ok(!request.headers()['content-type'].includes('multipart'));
      uploads.push(url.searchParams.get('filename'));
      const file_id='f'+uploads.length;
      batch.files.push({file_id,filename:uploads.at(-1),content_type:request.headers()['content-type'],metadata:{headers:['  日期  ','代码','流水号','金额<img src=x onerror=alert(1)>'],suggested_mapping:{business_date:'  日期  ',symbol:'代码',source_ref:'流水号',note:'不在表头中'}}});
      batch.rows.push({row_id:'ocr'+uploads.length,fields:{kind:'trade',business_date:'2026-10-22',symbol:'600001',name:'截图交易',currency:'CNY',asset_type:uploads.at(-1).endsWith('.xls')?null:'stock',action:'buy',gross_amount:'10.00'},source:{file_id,raw_text:'<script>unsafe</script>',bbox:[1,2,3,4]},issues:[{code:'low_confidence',message:'请核对原图'}],acknowledged:false});
      batch.revision++; return send(batch);
    }
    if(p.endsWith('/parse')) {
      assert.equal(method,'POST'); parseBodies.push(JSON.parse(body));
      if(parseFailure) {const status=parseFailure; parseFailure=0; return send({detail:{message:'字段映射请求失败'}},status);}
      assert.equal(parseBodies.at(-1).kind,batch.kind);
      batch.rows=batch.rows.map(row=>row.source?.file_id==='f1'?{...row,fields:{...row.fields,gross_amount:'11.00',note:''},acknowledged:false}:row);
      batch.files[0].metadata.suggested_mapping=JSON.parse(body).mapping;
      batch.revision++; return send(batch);
    }
    if(p.includes('/files/') && method==='GET') return route.fulfill({contentType:'image/png',body:Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jhxcAAAAASUVORK5CYII=','base64')});
    if(p.endsWith('/preview')) {
      const blocked=previewMode==='blocked'||previewMode==='duplicate', pending=previewMode==='pending';
      assert.equal(JSON.parse(body).revision,batch.revision);
      return send({batch_id:'b',revision:batch.revision,preview_token:'pt',can_publish:!blocked&&!pending,
        counts:{new:1,duplicates:0,blocked:blocked?1:0,pending:pending?1:0},
        rows:batch.rows.map(row=>({row_id:row.row_id,fields:{...row.fields,asset_type:row.fields.asset_type||'stock'},status:blocked?'blocked':pending?'pending':'ready',issues:previewMode==='duplicate'?[{code:'possible_duplicate',message:'疑似重复'}]:pending?[{code:'pending_confirmation',message:'原始资料尚待确认，缺失金额需核对'}]:[],
          before:batch.kind==='holding'?{id:7,account_code:'ths',as_of:'2026-10-19',symbol:'CASH_CNY',name:'Cash CNY',currency:'CNY',quantity:'-50.0100',market_value:'-50.0100',cost:'-50.0100',current_price:'1.0000',buy_price:'1.000000',asset_liability:'负债',major_category:'现金',minor_category:'现金/活期',created_at:'private timestamp',updated_at:'private timestamp',pnl:'12.3000',raw_note:'<img src=x onerror=alert(1)>'}:null,
          after:batch.kind==='holding'?{account_code:'ths',as_of:'2026-10-20',symbol:'CASH_CNY',name:'Cash CNY',currency:'CNY',quantity:'0',market_value:'0',cost:'0',current_price:'1',buy_price:'0',asset_liability:'资产',major_category:'现金',minor_category:'现金/活期'}:{market_value:null,cost:null,counter_account_code:'',...row.fields}})),
        impact:{nav_change:null,nav_estimate_status:'unavailable',message:'交易入账不代替持仓或现金快照'},warnings:[]});
    }
    if(p.endsWith('/publish')) {
      if(publishFailure) {const failure=publishFailure; publishFailure=null; return send({detail:{code:failure.code,message:failure.message}},failure.status);}
      publishes++; assert.deepEqual(JSON.parse(body),{revision:batch.revision,preview_token:'pt'}); batch.status='published'; return send({batch});
    }
    if(p.endsWith('/b') && method==='PUT') {
      if(saveFailure) {const failure=saveFailure; saveFailure=null; return send({detail:{code:failure.code,message:failure.message}},failure.status);}
      if(conflict) {conflict=false; return send({detail:'版本冲突'},409);}
      const update=JSON.parse(body); assert.equal(update.revision,batch.revision);
      batch={...batch,...update,revision:batch.revision+1,rows:update.rows.map((row,i)=>({...batch.rows.find(old=>old.row_id===row.row_id),...row,row_id:row.row_id||'r'+i}))}; return send(batch);
    }
    if(p.endsWith('/b') && method==='GET') {if(reloadFailure) {reloadFailure=false;return send({detail:'批次重新载入失败'},503);} return send(batch);}
    throw new Error('Unexpected API '+p);
  }
  const relative=p.slice(1)||'account_import.html';
  assert.ok(['account_import.html','assets/js/account_import.js','assets/css/account-import.css','assets/css/style.css','assets/css/design-system.css'].includes(relative));
  return route.fulfill({contentType:relative.endsWith('.js')?'text/javascript':relative.endsWith('.css')?'text/css':'text/html',body:await fs.readFile(path.join(publicRoot,relative))});
});
const page=await context.newPage(); page.on('pageerror',e=>errors.push(e.message));
const ready=()=>page.waitForFunction(()=>!document.getElementById('workspace').hidden&&!document.getElementById('workspace').inert);
const capture=async name=>{
  if(process.env.PAMS_FRONTEND_VISUAL_QA===name)
    console.log('VISUAL:'+JSON.stringify({name,image:(await page.screenshot({type:'jpeg',quality:60})).toString('base64')}));
};
const open=async()=>{await page.goto('http://account-import.invalid/account_import.html'); await ready();};
const save=async()=>{await page.locator('#record-form [type=submit]').click(); await page.waitForFunction(()=>!document.getElementById('editor').open&&!document.getElementById('workspace').inert);};
try {
  reset(); caps.configured=false;
  await page.goto('http://account-import.invalid/account_import.html');
  await page.waitForFunction(()=>document.getElementById('notice').textContent.includes('暂不可用'));
  assert.equal(await page.locator('#unlock-form').isVisible(),false);
  assert.equal(await page.locator('#workspace').isVisible(),false);
  assert.equal(allCalls.filter(([p])=>p.endsWith('/context')).length,0);
  reset(); caps.authenticated=false; caps.schema_ready=false;
  await page.reload(); await page.locator('#token').fill('secret');
  await page.locator('#unlock-form button').click(); await ready();
  assert.equal(await page.locator('#token').inputValue(),'');
  for(const width of [320,390,1280]) {
    reset(); await page.setViewportSize({width,height:844}); await open();
    await page.locator('#mode-manual').click(); await page.locator('#add-row').click();
    await page.locator('#field-business_date').fill('2026-10-22');
    await page.locator('#asset-search').fill('600001'); await page.locator('#asset-results button').click();
    assert.equal(await page.locator('#field-name').inputValue(),'长名称基金<img src=x onerror=alert(1)>');
    assert.equal(await page.locator('#asset-results img').count(),0);
    assert.deepEqual(await page.locator('#field-action option').evaluateAll(options=>options.filter(option=>option.value&&!option.disabled).map(option=>option.value)),['buy','sell','dividend','fee']);
    await page.locator('#field-action').selectOption('buy');
    await page.locator('#field-status').selectOption('pending');
    await page.locator('#field-fee_scope').selectOption('separate');
    await page.locator('#field-gross_amount').fill('12345678901234567890.0100');
    await page.locator('#field-fee_amount').fill('1.00'); await page.locator('#field-tax_amount').fill('0.20');
    await page.locator('#keep-edit').click();
    await page.locator('#mode-upload').click();
    if(width===320) {
      await page.locator('#files').setInputFiles({name:'暂不上传.csv',mimeType:'text/csv',buffer:Buffer.from('code,amount')});
      await page.waitForFunction(()=>document.getElementById('notice').textContent.includes('请先继续'));
      assert.equal(uploads.length,0);
    }
    await page.locator('#mode-manual').click();
    await page.getByRole('button',{name:'继续未保存的编辑'}).click();
    assert.equal(await page.locator('#field-gross_amount').inputValue(),'12345678901234567890.0100');
    await page.locator('#field-fee_scope').selectOption('total_charges');
    assert.equal(await page.locator('#field-tax_amount').count(),0);
    const fit=await page.evaluate(()=>({body:document.documentElement.scrollWidth,width:innerWidth,dialog:document.getElementById('editor').getBoundingClientRect().width,font:getComputedStyle(document.getElementById('field-name')).fontSize}));
    assert.ok(fit.body<=width); assert.ok(fit.dialog<=430); assert.equal(fit.font,'16px');
    const footer=await page.locator('.import-sheet-actions').boundingBox(); assert.ok(footer.y+footer.height<=844);
    await capture(width+'-editor');
    await save();
    await capture(width+'-intake');
    assert.equal(batch.rows[0].fields.gross_amount,'12345678901234567890.0100');
    assert.equal(batch.rows[0].fields.tax_amount,null); assert.equal(batch.rows[0].acknowledged,true);
    assert.equal(batch.rows[0].fields.asset_id,'a'); assert.equal(publishes,0);
    assert.equal(batch.rows[0].fields.asset_type,'fund'); assert.equal(batch.rows[0].fields.action,'buy');
    assert.ok(!(await page.locator('#rows').innerText()).includes('null'));
    assert.equal(await page.locator('#rows img,#rows script').count(),0);
    previewMode='pending'; await page.locator('#next').click(); await page.locator('#review').waitFor({state:'visible'});
    const compactLabels=await page.locator('#preview-rows .import-values dt').allTextContents();
    for(const label of ['市值','成本','对手账户']) assert.ok(!compactLabels.includes(label));
    assert.ok((await page.locator('#preview-rows').innerText()).includes('原始资料尚待确认，缺失金额需核对'));
    const rawPreview=page.locator('.import-preview-raw').first();
    await rawPreview.locator('summary').click();
    assert.ok((await rawPreview.locator('pre').innerText()).includes('"market_value": null'));
    assert.ok((await rawPreview.locator('pre').innerText()).includes('"cost": null'));
    assert.ok((await rawPreview.locator('pre').innerText()).includes('"counter_account_code": ""'));
    await rawPreview.locator('summary').click();
    await capture(width+'-preview');
    assert.equal(await page.locator('#publish').isDisabled(),true); assert.equal(publishes,0);
    await page.locator('#back').click();
    previewMode='duplicate'; await page.locator('#next').click(); await page.locator('#review').waitFor({state:'visible'});
    await page.getByRole('button',{name:'编辑 / 重复判定'}).click();
    await page.locator('#field-duplicate_decision').selectOption('distinct');
    await page.locator('#field-status').selectOption('confirmed'); await save();
    assert.equal(batch.rows[0].fields.duplicate_decision,'distinct');
    conflict=true; await page.locator('#rows button').click();
    await page.locator('#field-note').fill('保留冲突输入'); await page.locator('#record-form [type=submit]').click();
    await page.locator('#conflict').waitFor({state:'visible'});
    assert.equal(batch.rows[0].fields.note,''); assert.equal(await page.locator('#next').isDisabled(),true);
    page.once('dialog',dialog=>dialog.accept()); await page.locator('#reload').click(); await ready();
    assert.equal(await page.locator('#conflict').isVisible(),false);
    previewMode='ready'; await page.locator('#next').click(); await page.locator('#review').waitFor({state:'visible'});
    await page.waitForFunction(()=>!document.getElementById('publish').disabled);
    assert.equal(publishes,0); await page.locator('#publish').click();
    await page.waitForFunction(()=>document.getElementById('batch-meta').textContent.includes('已发布'));
    assert.equal(publishes,1); assert.equal(await page.locator('#add-row').isDisabled(),true);
  }
  reset(); caps.publish_enabled=false; await open();
  await page.locator('#files').setInputFiles([{name:'一.csv',mimeType:'text/csv',buffer:Buffer.from('code,amount\n600001,10.00')},{name:'截图.png',mimeType:'image/png',buffer:Buffer.from('png')}]);
  await page.waitForFunction(()=>document.getElementById('file-list').querySelectorAll('a').length===2&&!document.getElementById('workspace').inert);
  assert.deepEqual(uploads,['一.csv','截图.png']);
  assert.equal(await page.locator('#ack-label').isVisible(),true);
  await page.locator('#ack-all').check(); await page.locator('#save-draft').click(); await ready();
  assert.equal(batch.rows[1].acknowledged,true); assert.equal(publishes,0);
  await page.locator('#add-row').click();
  assert.equal(await page.locator('#field-kind').isDisabled(),true); assert.equal(await page.locator('#field-kind').inputValue(),'trade');
  await page.locator('#asset-search').fill('600001'); await page.locator('#asset-results button').click();
  await page.locator('#field-action').selectOption('buy'); await page.locator('#field-fee_scope').selectOption('total_charges');
  await page.locator('#field-business_date').fill('2026-10-20');
  for(const [key,value] of Object.entries({quantity:'1',price:'50.0100',gross_amount:'50.0100',fee_amount:'0',net_cash:'-50.0100'})) await page.locator('#field-'+key).fill(value);
  await save(); assert.equal(batch.rows.at(-1).fields.kind,'trade');
  assert.equal(batch.rows.at(-1).fields.net_cash,'-50.0100');
  assert.equal(batch.rows.at(-1).fields.asset_type,'fund');
  assert.equal(await page.locator('#capability-note').isVisible(),true);
  assert.equal(await page.getByRole('button',{name:'配置字段',exact:true}).count(),1);
  let map=page.locator('[data-mapping-file="f1"]');
  assert.equal(await map.locator('select').count(),0);
  await page.locator('#rows button').first().click();
  assert.equal(await page.locator('#field-source_ref').locator('..').innerText(),'成交/流水号');
  await page.locator('#field-note').fill('已编辑文件行'); await save();
  const manualBefore=JSON.stringify(batch.rows.at(-1));
  let mappingDialogs=0;
  const unexpectedDialog=async dialog=>{mappingDialogs++;await dialog.dismiss();};
  page.on('dialog',unexpectedDialog);
  for(const width of [320,390,1280]) {
    await page.setViewportSize({width,height:844});
    map=page.locator('[data-mapping-file="f1"]');
    await map.getByRole('button',{name:'配置字段',exact:true}).click();
    assert.equal(await map.locator('[data-canonical=business_date]').inputValue(),'  日期  ');
    assert.equal(await map.locator('[data-canonical=note]').inputValue(),'');
    const exactHeader='金额<img src=x onerror=alert(1)>';
    await map.locator('[data-canonical=gross_amount]').selectOption(exactHeader);
    assert.equal(await map.locator('img,script').count(),0);
    await page.locator('#mode-manual').click(); await page.locator('#mode-upload').click();
    assert.equal(await map.locator('[data-canonical=gross_amount]').inputValue(),exactHeader);
    const before=parseBodies.length;
    await map.getByRole('button',{name:'保存映射并重新识别',exact:true}).click();
    assert.equal(parseBodies.length,before);
    assert.ok((await map.innerText()).includes('包括已编辑内容和核对状态'));
    await map.getByRole('button',{name:'取消替换',exact:true}).click();
    assert.equal(parseBodies.length,before);
    await map.getByRole('button',{name:'保存映射并重新识别',exact:true}).click();
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await capture(width+'-mapping');
    if(width===320) parseFailure=500;
    if(width===390) parseFailure=409;
    if(width===1280) reloadFailure=true;
    await map.getByRole('button',{name:'确认替换并重新识别',exact:true}).click();
    await ready();
    if(width===320) {
      assert.equal(parseBodies.length,before+1);
      assert.equal(batch.rows[0].fields.note,'已编辑文件行');
      assert.ok((await map.innerText()).includes('字段映射请求失败'));
      assert.equal(await map.locator('[data-canonical=gross_amount]').inputValue(),exactHeader);
      await map.getByRole('button',{name:'确认替换并重新识别',exact:true}).click(); await ready();
    }
    if(width===390) {
      assert.equal(await page.locator('#conflict').isVisible(),true);
      assert.equal(await map.locator('[data-canonical=gross_amount]').isDisabled(),true);
      assert.equal(mappingDialogs,0); page.off('dialog',unexpectedDialog);
      page.once('dialog',dialog=>dialog.accept()); await page.locator('#reload').click(); await ready();
      page.on('dialog',unexpectedDialog);
      await map.getByRole('button',{name:'配置字段',exact:true}).click();
      await map.getByRole('button',{name:'保存映射并重新识别',exact:true}).click();
      await map.getByRole('button',{name:'确认替换并重新识别',exact:true}).click(); await ready();
    }
    if(width===1280) {
      assert.equal(parseBodies.length,before+1);
      assert.ok((await map.innerText()).includes('映射已保存，但重新载入失败'));
      await map.getByRole('button',{name:'重新载入批次',exact:true}).click(); await ready();
      assert.equal(parseBodies.length,before+1);
    }
    assert.deepEqual(parseBodies.at(-1),{kind:'trade',mapping:{business_date:'  日期  ',symbol:'代码',source_ref:'流水号',gross_amount:exactHeader}});
    assert.equal(batch.rows[0].fields.note,''); assert.equal(batch.rows[0].fields.gross_amount,'11.00');
    assert.equal(batch.rows[0].acknowledged,false);
    assert.equal(JSON.stringify(batch.rows.at(-1)),manualBefore);
    assert.equal(await map.locator('select').count(),0);
    const afterParse=allCalls.slice(allCalls.findLastIndex(([p])=>p.endsWith('/parse'))+1);
    assert.ok(afterParse.some(([p,method])=>p==='/api/account-import/batches/b'&&method==='GET'));
    assert.equal(publishes,0);
  }
  assert.equal(mappingDialogs,0); page.off('dialog',unexpectedDialog);
  await page.locator('#next').click(); await page.locator('#review').waitFor({state:'visible'});
  assert.equal(await page.locator('#publish').isDisabled(),true); assert.equal(publishes,0);
  for(const width of [320,390,1280]) {
    reset(); caps.publish_enabled=false;
    await page.setViewportSize({width,height:844}); await open();
    await page.locator('#mode-manual').click(); await page.locator('#kind').selectOption('holding');
    await page.locator('#add-row').click();
    assert.equal(await page.locator('#field-kind').isDisabled(),true); assert.equal(await page.locator('#field-kind').inputValue(),'holding');
    await page.locator('#field-business_date').fill('2026-10-20');
    await page.locator('#asset-search').fill('600001'); await page.locator('#asset-results button').click();
    await page.locator('#field-quantity').fill('2.0000'); await page.locator('#field-price').fill('3.00');
    await page.locator('#field-market_value').fill('6.00'); await page.locator('#field-cost').fill('5.00');
    await page.locator('#field-cost_basis_type').selectOption('holding_cost');
    await page.locator('#field-asset_type').selectOption('cash');
    assert.equal(await page.locator('#asset-lookup').isVisible(),false);
    assert.equal(await page.locator('#field-symbol,#field-name,#field-quantity,#field-price,#field-cost,#field-market_value,#field-cost_basis_type').count(),0);
    assert.equal(await page.locator('#field-business_date').inputValue(),'2026-10-20');
    assert.equal(await page.locator('#field-net_cash').inputValue(),'');
    assert.equal(await page.locator('#field-net_cash').locator('..').innerText(),'现金余额');
    await page.locator('#field-net_cash').fill('12345678901234567890.0100');
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await capture(width+'-holding-cash'); await save();
    assert.equal(batch.kind,'holding'); assert.equal(batch.rows.length,1);
    let fields=batch.rows[0].fields;
    assert.equal(fields.kind,'holding'); assert.equal(fields.asset_type,'cash');
    assert.equal(fields.action,'balance'); assert.equal(fields.currency,'CNY');
    assert.equal(fields.business_date,'2026-10-20'); assert.equal(fields.net_cash,'12345678901234567890.0100');
    for(const key of ['asset_id','symbol','name','quantity','price','cost','market_value','cost_basis_type']) assert.ok(!(key in fields));
    assert.ok((await page.locator('#rows').innerText()).includes('现金余额'));
    await page.locator('#rows button').click();
    assert.equal(await page.locator('#field-net_cash').inputValue(),'12345678901234567890.0100');
    assert.equal(await page.locator('#asset-lookup').isVisible(),false);
    await page.locator('#field-asset_type').selectOption('stock');
    assert.equal(await page.locator('#asset-lookup').isVisible(),true);
    for(const key of ['symbol','name','quantity','price','cost','market_value']) assert.equal(await page.locator('#field-'+key).inputValue(),'');
    assert.equal(await page.locator('#field-net_cash').count(),0);
    await page.locator('#field-asset_type').selectOption('cash');
    assert.equal(await page.locator('#field-net_cash').inputValue(),'');
    await page.locator('#field-net_cash').fill('0'); await save();
    assert.equal(batch.rows.length,1); assert.equal(batch.rows[0].fields.net_cash,'0');
    assert.equal(publishes,0);
    await page.locator('#next').click(); await page.locator('#review').waitFor({state:'visible'});
    const labels=await page.locator('#preview-rows .import-values dt').allTextContents();
    for(const label of ['账户','快照日期','现价','成本均价','资产/负债','资产大类','资产子类']) assert.ok(labels.includes(label));
    for(const hidden of ['id','created_at','updated_at','pnl']) assert.ok(!labels.includes(hidden));
    assert.ok((await page.locator('#preview-rows .import-values dd').allTextContents()).includes('-50.0100'));
    const raw=page.locator('.import-preview-raw').first();
    assert.equal(await raw.getAttribute('open'),null);
    await raw.locator('summary').click();
    assert.ok((await raw.locator('pre').innerText()).includes('"pnl": "12.3000"'));
    assert.ok((await raw.locator('pre').innerText()).includes('<img src=x onerror=alert(1)>'));
    assert.equal(await page.locator('#preview-rows img,#preview-rows script').count(),0);
    await raw.locator('summary').click(); await capture(width+'-snapshot-preview');
  }
  reset(); caps.ocr_available=false; await open();
  for(const ext of ['pdf','webp']) {
    await page.locator('#files').setInputFiles({name:'不支持.'+ext,mimeType:'application/octet-stream',buffer:Buffer.from('unsupported')});
    await page.waitForFunction(()=>document.getElementById('notice').textContent.includes('仅支持 CSV'));
    await ready(); assert.equal(batch,null); assert.equal(uploads.length,0);
  }
  await page.locator('#files').setInputFiles({name:'空类型截图.PNG',mimeType:'',buffer:Buffer.from('png')});
  await page.waitForFunction(()=>document.getElementById('notice').textContent.includes('截图识别当前不可用'));
  await ready(); assert.equal(batch,null); assert.equal(uploads.length,0);
  await page.locator('#files').setInputFiles({name:'报表.TSV',mimeType:'text/tab-separated-values',buffer:Buffer.from('code\tamount\n600001\t10.00')});
  await page.waitForFunction(()=>document.getElementById('file-list').querySelectorAll('a').length===1&&!document.getElementById('workspace').inert);
  assert.deepEqual(uploads,['报表.TSV']); assert.equal(publishes,0);
  await page.locator('#files').setInputFiles({name:'原始导出.xls',mimeType:'application/vnd.ms-excel',buffer:Buffer.from('native broker table')});
  await page.waitForFunction(()=>document.getElementById('file-list').querySelectorAll('a').length===2&&!document.getElementById('workspace').inert);
  assert.deepEqual(uploads,['报表.TSV','原始导出.xls']); assert.equal(publishes,0);
  await page.locator('#next').click(); await page.locator('#review').waitFor({state:'visible'});
  await page.locator('#preview-rows button').filter({hasText:'编辑 / 重复判定'}).nth(1).click();
  assert.equal(await page.locator('#field-asset_type').inputValue(),'stock');
  assert.equal(batch.rows[1].fields.asset_type,null);
  await save(); assert.equal(batch.rows[1].fields.asset_type,'stock'); assert.equal(publishes,0);
  reset(); await open();
  await page.locator('#account').selectOption('licai'); await page.locator('#mode-manual').click(); await page.locator('#add-row').click();
  await page.locator('#field-business_date').fill('2026-10-20');
  await page.locator('#asset-search').fill('600001'); await page.locator('#asset-results button').click();
  assert.deepEqual(await page.locator('#field-action option').evaluateAll(options=>options.filter(option=>option.value&&!option.disabled).map(option=>option.value)),['subscribe','redeem','dividend','fee']);
  await page.locator('#field-asset_type').selectOption('stock');
  assert.deepEqual(await page.locator('#field-action option').evaluateAll(options=>options.filter(option=>option.value&&!option.disabled).map(option=>option.value)),['buy','sell','dividend','fee']);
  await page.locator('#field-action').selectOption('buy'); await page.locator('#field-asset_type').selectOption('fund');
  assert.equal(await page.locator('#field-action option[value=buy]').evaluate(option=>option.disabled),true);
  assert.deepEqual(await page.locator('#field-action option').evaluateAll(options=>options.filter(option=>option.value&&!option.disabled).map(option=>option.value)),['subscribe','redeem','dividend','fee']);
  await page.locator('#field-action').selectOption('subscribe'); await page.locator('#field-confirmed_date').fill('2026-10-21');
  await page.locator('#field-fee_scope').selectOption('total_charges');
  for(const [key,value] of Object.entries({quantity:'1',price:'1',gross_amount:'1',fee_amount:'0',net_cash:'-1'})) await page.locator('#field-'+key).fill(value);
  await save(); assert.equal(batch.account_code,'licai'); assert.equal(batch.rows[0].fields.action,'subscribe');
  assert.equal(batch.rows[0].fields.asset_type,'fund'); assert.ok(!('source_asset_type' in batch.rows[0].fields)); assert.equal(publishes,0);
  await page.locator('#new-batch').click(); await page.locator('#kind').selectOption('cash'); await page.locator('#add-row').click();
  await page.locator('#editor').waitFor({state:'visible'});
  assert.equal(await page.locator('#field-kind').isDisabled(),true); assert.equal(await page.locator('#field-kind').inputValue(),'cash');
  await page.locator('#field-action').selectOption('withdraw'); await page.locator('#field-counter_account_code').selectOption('bank');
  await page.locator('#field-business_date').fill('2026-10-20'); await page.locator('#field-net_cash').fill('-50.0100'); await save();
  assert.equal(batch.kind,'cash'); assert.equal(batch.rows[0].fields.kind,'cash');
  assert.equal(batch.rows[0].fields.counter_account_code,'bank'); assert.equal(batch.rows[0].fields.net_cash,'-50.0100');
  await page.locator('#rows button').click(); await page.locator('#field-note').fill('403后保留输入');
  saveFailure={status:403,code:'origin_required',message:'同源检查失败'};
  await page.locator('#record-form [type=submit]').click();
  await page.waitForFunction(()=>document.getElementById('editor-error').textContent.includes('同源检查失败'));
  assert.equal(await page.locator('#editor').isVisible(),true); assert.equal(await page.locator('#unlock-form').isVisible(),false);
  assert.equal(await page.locator('#workspace').isVisible(),true); assert.equal(await page.locator('#field-note').inputValue(),'403后保留输入');
  saveFailure={status:401,code:'session_expired',message:'会话已过期'};
  await page.locator('#record-form [type=submit]').click(); await page.locator('#unlock-form').waitFor({state:'visible'});
  assert.equal(await page.locator('#editor').isVisible(),false); assert.equal(await page.locator('#workspace').isVisible(),false);
  await page.locator('#token').fill('secret'); await page.locator('#unlock-form button').click(); await ready();
  await page.getByRole('button',{name:'继续未保存的编辑'}).click();
  assert.equal(await page.locator('#field-note').inputValue(),'403后保留输入'); await save();
  assert.equal(batch.rows[0].fields.note,'403后保留输入');
  await page.locator('#next').click(); await page.locator('#review').waitFor({state:'visible'});
  await page.waitForFunction(()=>!document.getElementById('publish').disabled);
  publishFailure={status:403,code:'origin_required',message:'发布同源检查失败'};
  await page.locator('#publish').click();
  await page.waitForFunction(()=>document.getElementById('notice').textContent.includes('发布同源检查失败')&&!document.getElementById('publish').disabled);
  assert.equal(await page.locator('#unlock-form').isVisible(),false); assert.equal(await page.locator('#review').isVisible(),true);
  assert.equal(await page.locator('#capability-note').isVisible(),false); assert.equal(publishes,0);
  publishFailure={status:403,code:'publish_disabled',message:'发布未启用；可保存和核对草稿'};
  await page.locator('#publish').click(); await page.locator('#capability-note').waitFor({state:'visible'});
  assert.equal(await page.locator('#publish').isDisabled(),true); assert.equal(await page.locator('#unlock-form').isVisible(),false);
  assert.equal(await page.locator('#workspace').isVisible(),true); assert.equal(batch.status,'draft'); assert.equal(publishes,0);
  await page.locator('#back').click(); await page.locator('#rows button').click();
  assert.equal(await page.locator('#field-note').inputValue(),'403后保留输入'); await save();
  assert.equal(errors.length,0,errors.join('\n'));
  console.log(JSON.stringify({viewports:[320,390,1280],requests:allCalls.length,errors,productionRequests:0}));
} finally {await browser.close();}
"""


def test_optional_intercepted_browser_workflow() -> None:
    """Set an installed Playwright index.mjs path; no dependency install or live API."""
    if not os.environ.get("PAMS_FRONTEND_PLAYWRIGHT_MODULE"):
        pytest.skip("Optional offline browser runtime not configured")
    node(BROWSER, timeout=120)
