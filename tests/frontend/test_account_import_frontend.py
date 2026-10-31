"""Offline contracts. Optional real-browser coverage uses only intercepted requests."""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PUBLIC = ROOT / "frontend" / "public"
HTML = (PUBLIC / "account_import.html").read_text(encoding="utf-8")
ENTRY = PUBLIC / "assets/js/account_import.js"
JS = ENTRY.read_text(encoding="utf-8") + "\n" + "\n".join(
    (PUBLIC / "assets/js/features/account_import" / name).read_text(encoding="utf-8")
    for name in ["contract.js", "client.js", "mapping_editor.js", "records_editor.js", "controller.js", "request_scope.js"]
)
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
         f"const m = await import({json.dumps(ENTRY.as_uri())});\n"
         + code)


def test_isolated_two_step_shell_and_existing_bank_link() -> None:
    assert 'id="step-one"' in HTML and 'id="step-two"' in HTML
    assert "填写 / 识别" in HTML and "核对发布" in HTML
    assert 'id="review" aria-label="核对发布" hidden' in HTML
    assert 'id="ack-all"' not in HTML
    assert 'id="image-review"' in HTML and 'id="confirm-image"' in HTML
    assert HTML.index('id="kind"') < HTML.index('id="account"')
    assert 'type="module" src="assets/js/account_import.js?v=' in HTML
    assert 'aria-label="关闭记录编辑器"' in HTML
    assert "app-shell" in HTML and '<dialog id="editor"' in HTML
    more = (PUBLIC / "more.html").read_text(encoding="utf-8")
    assert more.count('href="account_import.html"') == 1
    assert 'href="accounts.html#overview"' in more
    assert 'href="accounts.html#sources"' not in more
    assert '<nav class="nav"' not in HTML
    assert 'id="flow-back"' in HTML
    assert "430px" in CSS and "safe-area-inset-bottom" in CSS
    assert "--import-keyboard" in CSS and "visualViewport" in JS
    assert "font-size: 16px" in CSS and "min-height: 44px" in CSS
    assert "overflow-wrap: anywhere" in CSS
    assert "hero" not in HTML.lower() and 'class="card' not in HTML


@pytest.mark.parametrize("caps, expected", [
    (None, "unavailable"), ({}, "unavailable"),
    ({"schema_ready": False}, "unavailable"),
    ({"schema_ready": True, "authentication_required": False}, "ready"),
    ({"configured": False, "authenticated": False, "schema_ready": True}, "ready"),
    ({"configured": True, "authenticated": False, "schema_ready": False}, "unavailable"),
    ({"configured": True, "authenticated": True, "schema_ready": False}, "unavailable"),
    ({"configured": True, "authenticated": False, "schema_ready": True}, "ready"),
    ({"configured": True, "authenticated": True, "schema_ready": True}, "ready"),
])
def test_schema_alone_controls_availability_without_unlock(caps: dict | None, expected: str) -> None:
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
    assert re.search(r'id="files"[^>]*accept="([^"]+)"', HTML).group(1) == ".csv,.tsv,.xls,.xlsx,.pdf"
    assert 'id="images"' in HTML and 'accept="image/png,image/jpeg"' in HTML
    assert "CSV / TSV / 同花顺 XLS / XLSX" in HTML
    module_test("""
      for (const ext of ['csv','tsv','xls','xlsx','png','jpg','jpeg']) {
        assert.equal(m.fileFormat('报表.'+ext),ext);
        assert.equal(m.fileFormat('报表.'+ext.toUpperCase()),ext);
      }
      for (const name of ['报表.pdf','报表.webp','无扩展名','报表.csv.exe','报表.tsv '])
        assert.throws(()=>m.fileFormat(name), /仅支持 CSV、TSV、同花顺 XLS、XLSX、PNG、JPG、JPEG/);
      assert.equal(m.fileFormat('账单.pdf','cash'),'pdf');
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


def test_unlock_ui_is_removed_and_record_kind_is_fixed_to_batch() -> None:
    for removed in ['unlock-form', 'id="token"', 'type="password"', '访问口令', '解锁']:
        assert removed not in HTML
    assert "/unlock" not in JS and "authenticated" not in JS
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
      await client('/batches','POST',{account_code:'ths',kind:'trade',mode:'manual'});
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
      assert.equal(calls[0][1].body, JSON.stringify({account_code:'ths',kind:'trade',mode:'manual'}));
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
    assert "listen($('publish'), 'click'" in JS
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
    assert "仅预览" in HTML and "暂存表尚未就绪" in JS


def test_review_hides_requested_summaries_but_keeps_actionable_states():
    assert 'id="impact" hidden' in HTML
    assert "$('impact').hidden = !knownImpact" in JS
    assert "NAV 影响暂不可用" not in JS
    assert "发布尚未启用" not in HTML and "发布尚未启用；草稿可保留" not in JS
    for prefix in ['持仓、交易、现金独立核对', '资产底表待补充/核准', '已识别的券商入出金缺少银行对手账户证据']:
        assert prefix in JS
    assert "groupLabels[group.code] || group.message" in JS
    assert "row.issues?.length" in JS and "publishable(state.caps" in JS
    assert "--import-yellow: var(--pams-action)" in CSS


def test_holding_cash_uses_balance_without_security_fields_or_local_totals() -> None:
    assert 'id="asset-lookup"' in HTML
    assert "holding ? ['stock', 'fund', 'cash']" in JS
    assert "$('asset-lookup').hidden = cash" in JS
    assert "if (holding && !cash) field('value-fields', 'cost_basis_type'" in JS
    assert "if (fields.kind === 'holding' && fields.asset_type === 'cash') { fields.action = 'balance'; delete fields.asset_id; }" in JS
    assert "'asset_id', 'symbol', 'name', 'quantity', 'price', 'market_value', 'cost', 'cost_basis_type', 'net_cash'" in JS
    assert "new Date" not in JS and "parseFloat" not in JS and "toFixed" not in JS


BROWSER = (ROOT / "tests/frontend/frontend_import_flow.mjs").read_text(encoding="utf-8")


def test_optional_intercepted_browser_workflow() -> None:
    """Set an installed Playwright index.mjs path; no dependency install or live API."""
    if not os.environ.get("PAMS_FRONTEND_PLAYWRIGHT_MODULE"):
        pytest.skip("Optional offline browser runtime not configured")
    node(BROWSER, timeout=120)
