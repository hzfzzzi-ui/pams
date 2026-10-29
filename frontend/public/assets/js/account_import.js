const ROOT = '/api/account-import';
const KIND_TEXT = {trade:'交易',holding:'持仓快照',cash:'账单'};
const DECIMALS = ['quantity', 'price', 'gross_amount', 'fee_amount', 'tax_amount', 'net_cash', 'market_value', 'cost'];
const LABELS = { kind: '类型', business_date: '业务日期', trade_time: '成交时间', confirmed_date: '确认日期', asset_type: '标的类型', symbol: '代码', name: '名称', currency: '币种', action: '方向', status: '状态', quantity: '数量 / 份数', price: '价格', gross_amount: '发生金额', fee_scope: '费用范围', fee_amount: '费用金额', tax_amount: '税款', net_cash: '现金金额 / 余额', market_value: '市值', cost: '成本', cost_basis_type: '成本口径', counter_account_code: '对手账户', source_ref: '成交/流水号', note: '业务备注', duplicate_decision: '重复判定', asset_id: '标的 ID' };
const PREVIEW_LABELS = { account_code: '账户', as_of: '快照日期', ...LABELS, current_price: '现价', buy_price: '成本均价', major_category: '资产大类', minor_category: '资产子类', asset_liability: '资产/负债',
  event_date:'发生日期',posting_date:'入账日期',amount:'原币金额',amount_cny:'人民币金额',fx_rate_to_cny:'采用汇率',cashflow_type:'资金类型',category:'分类',subcategory:'子类',counterparty:'对方',summary:'摘要',description:'描述',affects_nav:'影响NAV',affects_spending:'计入收支',affects_investment_capital:'计入投资本金',is_internal_transfer:'内部划转',net_cash:'净收付 / 余额' };
const TEXT = { trade: '交易', holding: '持仓快照', cash: '现金', stock: '股票', fund: '基金', buy: '买入', sell: '卖出', subscribe: '申购', redeem: '赎回', dividend: '分红', fee: '费用', tax: '税款', interest: '利息', balance: '余额', deposit: '入金', withdraw: '出金', transfer: '划转', pending: '待确认', confirmed: '已确认', ready: '可发布', duplicate: '重复，跳过', blocked: '阻断', draft: '草稿', published: '已发布', voided: '已作废', partial: '部分记录', complete: '完整快照', holding_cost: '持仓成本', diluted_cost: '摊薄成本', unknown: '未知', total_charges: '税费合计', separate: '手续费与税分列', distinct: '核对为不同交易', skip: '跳过此条' };

export function decimalString(value) {
  const text = String(value ?? '').trim();
  if (text && !/^-?(?:\d+(?:\.\d*)?|\.\d+)$/.test(text)) throw new Error('金额与数量须为十进制数字，不接受千分位或科学计数法');
  return text;
}
export function fileFormat(name, kind = 'trade') {
  const format = String(name).match(/\.([^.]+)$/)?.[1]?.toLowerCase();
  if (![...['csv', 'tsv', 'xls', 'xlsx', 'png', 'jpg', 'jpeg'], ...(kind === 'cash' ? ['pdf'] : [])].includes(format)) throw new Error('仅支持 CSV、TSV、同花顺 XLS、XLSX、PNG、JPG、JPEG；账单另支持 PDF。');
  return format;
}
export function previewFields(object) {
  return Object.entries(PREVIEW_LABELS)
    .filter(([key]) => key !== 'asset_id' && Object.hasOwn(object || {}, key))
    .filter(([key]) => object[key] != null && (typeof object[key] !== 'string' || object[key].trim() !== ''))
    .map(([key, label]) => ({ key, label, value: object[key] }));
}
export function accessState(caps) {
  if (caps?.configured !== true) return 'unavailable';
  if (caps.authenticated !== true) return 'locked';
  return caps.schema_ready === true ? 'ready' : 'unavailable';
}
export function displayAssetType(type) {
  return ['fund', 'etf', 'mutual_fund'].includes(type) ? 'fund' : ['stock', 'equity', 'listed_equity'].includes(type) ? 'stock' : type;
}
export function recordEditable(row) {
  return !row || row.can_edit_fields === true || (row.can_edit_fields === undefined && (!row.source?.file_id || row.source?.origin === 'ocr'));
}
export function publishable(caps, batch, preview, dirty = false) {
  return caps?.publish_enabled === true && batch?.status === 'draft' && !dirty && preview?.can_publish === true &&
    preview.batch_id === batch.batch_id && preview.revision === batch.revision && !!preview.preview_token &&
    preview.counts?.blocked === 0 && preview.counts?.pending === 0 && !preview.rows?.some(row => ['blocked', 'pending'].includes(row.status));
}
export function createImportClient(fetcher) {
  return async (path, method = 'GET', body, raw = false, signal) => {
    if (!path.startsWith('/') || path.startsWith('//') || /[.#]/.test(path.split('?')[0])) throw new Error('无效导入路径');
    const response = await fetcher(ROOT + path, { method, signal, credentials: 'same-origin', cache: 'no-store', redirect: 'error',
      headers: body === undefined ? {} : { 'Content-Type': raw ? (body.type || 'application/octet-stream') : 'application/json' },
      body: body === undefined ? undefined : raw ? body : JSON.stringify(body) });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw Object.assign(new Error(typeof data.detail === 'string' ? data.detail : data.detail?.message || `请求失败（${response.status}）`), { status: response.status, code: data.detail?.code });
    return data;
  };
}

if (typeof document !== 'undefined') {
  const $ = id => document.getElementById(id);
  const api = createImportClient(window.fetch.bind(window));
  const state = { caps: null, accounts: [], counterAccounts: [], batch: null, preview: null, step: 1, mode: 'upload', imageIndex: null, dirty: false, busy: false, conflict: false, edit: null, buffer: null, suspended: null, mappings: new Map() };
  const el = (tag, text, cls) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = String(text); if (cls) node.className = cls; return node; };
  const message = text => { $('notice').textContent = text; };
  const path = () => `/batches/${encodeURIComponent(state.batch.batch_id)}`;
  const account = () => state.accounts.find(a => a.account_code === $('account').value);
  const editable = () => !state.batch || state.batch.status === 'draft';
  const images = () => (state.batch?.files || []).filter(file => file.content_type?.startsWith('image/'));
  const issues = list => (list || []).map(item => typeof item === 'string' ? item : item.message || item.code || JSON.stringify(item)).join('；');
  const button = (text, action) => { const node = el('button', text); node.type = 'button'; node.addEventListener('click', action); return node; };
  const dirty = () => { state.dirty = true; state.preview = null; };

  async function run(task) {
    if (state.busy) return;
    state.busy = true; renderControls();
    try { await task(); }
    catch (error) {
      if (error.status === 409) {
        state.conflict = true; state.preview = null; $('conflict').hidden = false;
        if ($('editor').open) { rememberEditor(); $('editor').close(); } render();
      }
      if (error.status === 403 && error.code === 'publish_disabled') {
        state.caps = { ...state.caps, publish_enabled: false }; renderAccess();
      }
      if (error.status === 401) {
        state.caps = { ...state.caps, authenticated: false }; renderAccess();
        if ($('editor').open) { rememberEditor(); $('editor').close(); }
      }
      message(error.message);
      if ($('editor').open) $('editor-error').textContent = error.message;
    } finally { state.busy = false; renderControls(); }
  }
  function renderAccess() {
    const access = accessState(state.caps);
    $('gate').hidden = access === 'ready'; $('workspace').hidden = access !== 'ready'; $('actions').hidden = access !== 'ready';
    const canUnlock = state.caps?.configured === true && state.caps.authenticated !== true;
    $('unlock-form').hidden = !canUnlock;
    message(access === 'unavailable' ? (canUnlock ? '请先解锁；暂存结构尚未核验，当前不可用。' : '账户导入暂不可用：配置或暂存表尚未就绪。') : access === 'locked' ? '请解锁投资账户导入。' : '');
    $('capability-note').hidden = access !== 'ready' || state.caps.publish_enabled === true;
  }
  async function start() {
    state.caps = await api('/capabilities'); renderAccess();
    if (accessState(state.caps) !== 'ready') return;
    const context = await api('/context');
    state.accounts = context.accounts || [];
    state.counterAccounts = context.counter_accounts || [];
    const query = new URLSearchParams(location.search);
    if (!state.batch && ['trade','holding','cash'].includes(query.get('kind'))) $('kind').value = query.get('kind');
    renderAccounts(state.batch?.account_code || query.get('account'));
    if (!state.batch && query.get('mode') === 'manual') state.mode = 'manual';
    if (!state.accounts.length) message('没有可用的导入账户。');
    const limits = state.caps.limits || {};
    $('file-limits').textContent = [state.caps.ocr_available ? '截图识别可用' : '截图识别不可用', ...Object.entries(limits).map(([key, value]) => `${({ files: '文件上限', file_bytes: '单文件字节上限', batch_bytes: '批次字节上限', rows: '记录上限', max_files: '文件上限', max_file_bytes: '单文件字节上限', max_rows: '记录上限' })[key] || key}：${value}`)].join(' · ');
    await history(); render();
  }
  function renderAccounts(preferred = $('account').value) {
    const available = state.accounts.filter(a => (a.kinds || ['trade','holding','cash']).includes($('kind').value));
    $('account').replaceChildren(...available.map(a => new Option(a.label || a.account_code, a.account_code)));
    if (available.some(a => a.account_code === preferred)) $('account').value = preferred;
    if (!available.length) message('此类别暂无已配置账户。');
  }
  function renderControls() {
    const locked = state.busy || state.conflict || !editable() || !state.accounts.length;
    $('workspace').inert = state.busy;
    $('record-form').inert = state.busy;
    $('unlock-form').querySelector('button').disabled = state.busy;
    $('retry').disabled = state.busy;
    $('account').disabled = !!state.batch || state.busy;
    $('kind').disabled = !!state.batch || state.busy;
    for (const id of ['completeness', 'files', 'images', 'add-row', 'save-draft', 'confirm-image', 'previous-image']) $(id).disabled = locked;
    $('file-list').querySelectorAll('.import-mapping button, .import-mapping select').forEach(control => { control.disabled = locked; });
    $('next').disabled = locked || !(state.batch?.rows?.length || images().length) || !!state.suspended;
    $('publish').disabled = state.busy || state.conflict || !publishable(state.caps, state.batch, state.preview, state.dirty);
    $('back').disabled = state.busy; $('reload').disabled = state.busy;
    $('new-batch').disabled = state.busy;
    $('save-draft').textContent = state.busy ? '处理中…' : '保存草稿';
  }
  function render() {
    const imageReview = state.step === 2 && state.imageIndex !== null;
    $('intake').hidden = state.step !== 1; $('review').hidden = state.step !== 2 || imageReview;
    $('image-review').hidden = !imageReview;
    for (const [id, step] of [['step-one', 1], ['step-two', 2]]) {
      if (step === state.step) $(id).setAttribute('aria-current', 'step'); else $(id).removeAttribute('aria-current');
    }
    for (const id of ['save-draft', 'next']) $(id).hidden = state.step !== 1;
    for (const id of ['back', 'publish']) $(id).hidden = state.step !== 2 || imageReview;
    for (const id of ['previous-image','confirm-image']) $(id).hidden = !imageReview;
    $('upload-area').hidden = state.mode !== 'upload';
    $('screenshot-area').hidden = state.mode !== 'ocr';
    $('ocr-status').textContent = state.caps?.ocr_available ? '' : '截图识别当前不可用';
    for (const mode of ['upload', 'ocr', 'manual']) $(`mode-${mode}`).setAttribute('aria-pressed', String(state.mode === mode));
    $('add-row').hidden = state.mode !== 'manual';
    $('manual-records-head').hidden = state.mode !== 'manual';
    $('rows').hidden = state.mode !== 'manual';
    $('completeness-label').hidden = $('kind').value !== 'holding';
    $('complete-warning').hidden = $('completeness').value !== 'complete';
    const batch = state.batch;
    $('batch-meta').textContent = batch ? `${TEXT[batch.status] || batch.status} · ${batch.batch_id} · 版本 ${batch.revision}${state.dirty ? ' · 未保存' : ''}${batch.updated_at ? ` · ${batch.updated_at}` : ''}` : '尚无暂存记录';
    $('rows').replaceChildren();
    for (const [index, row] of (batch?.rows || []).entries()) {
      const f = row.fields || {}, node = el('article', undefined, 'import-row');
      const cash = f.kind === 'cash' || f.asset_type === 'cash';
      node.append(el('strong', `${f.symbol || (cash ? '现金' : '未填代码')} · ${f.name || (cash ? '现金余额' : '未填名称')}`));
      node.append(el('p', [f.business_date || '日期未填', KIND_TEXT[f.kind] || f.kind, TEXT[f.action] || f.action, TEXT[f.status] || f.status, row.excluded ? '已排除' : row.acknowledged ? '已核对' : '待核对'].filter(Boolean).join(' · '), 'import-meta'));
      const amounts = DECIMALS.filter(key => f[key] != null && f[key] !== '').map(key => `${key === 'fee_amount' && f.fee_scope === 'total_charges' ? '税费合计' : LABELS[key]} ${f[key]}`).join(' · ');
      if (amounts) node.append(el('p', `${amounts} ${f.currency || ''}`, 'import-meta'));
      if (row.issues?.length) node.append(el('p', issues(row.issues), 'import-warning'));
      node.append(button(editable() ? '编辑 / 核对' : '查看记录', () => openEditor(index)));
      $('rows').append(node);
    }
    if (!batch?.rows?.length) $('rows').append(el('p', '暂无记录', 'import-meta'));
    if (state.suspended) $('rows').prepend(button('继续未保存的编辑', () => resumeEditor()));
    $('file-list').replaceChildren(...(batch?.files || []).map(file => {
      const node = evidence(file);
      if (!file.content_type?.startsWith('image/')) node.append(mappingEditor(file));
      return node;
    }));
    $('originals').hidden = !batch?.files?.length;
    if (imageReview) renderImageReview();
    renderControls();
  }
  function evidence(file, source) {
    const node = el('div', undefined, 'import-evidence');
    if (file) {
      const url = `${ROOT}${path()}/files/${encodeURIComponent(file.file_id)}`;
      const link = el('a', file.filename || '查看原始文件'); link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer'; node.append(link);
      if (file.content_type?.startsWith('image/')) {
        const img = el('img'); img.src = url; img.alt = file.filename || '原始截图'; img.loading = 'lazy'; img.referrerPolicy = 'no-referrer';
        img.addEventListener('error', () => { img.hidden = true; node.append(el('p', '原图加载失败，请重新解锁或打开原始文件。', 'import-warning')); }); node.append(img);
        img.tabIndex = 0;
        const open = () => { $('full-image').src = url; $('image-viewer').showModal(); };
        img.addEventListener('click', open); img.addEventListener('keydown', event => { if (event.key === 'Enter') open(); });
      }
    }
    if (source) {
      node.append(el('p', [source.sheet, source.row != null ? `原始行 ${source.row}` : '', source.bbox ? `位置 ${JSON.stringify(source.bbox)}` : ''].filter(Boolean).join(' · '), 'import-meta'));
      if (source.raw_text) node.append(el('pre', source.raw_text));
    }
    return node;
  }
  function mappingEditor(file) {
    const headers = (file.metadata?.headers || []).filter(header => typeof header === 'string' && header !== '');
    if (!state.mappings.has(file.file_id)) state.mappings.set(file.file_id, { open: false, dirty: false, confirm: false, error: '', values: Object.fromEntries(Object.entries(file.metadata?.suggested_mapping || {}).filter(([key, header]) => Object.hasOwn(LABELS, key) && headers.includes(header))) });
    const draft = state.mappings.get(file.file_id), node = el('div', undefined, 'import-mapping');
    node.dataset.mappingFile = file.file_id;
    const toggle = button('配置字段', () => { draft.open = !draft.open; draft.confirm = false; render(); });
    const panel = el('div'); panel.id = `mapping-${file.file_id}`; panel.hidden = !draft.open;
    toggle.setAttribute('aria-expanded', String(draft.open)); toggle.setAttribute('aria-controls', panel.id);
    toggle.disabled = state.busy || state.conflict || !editable(); node.append(toggle, panel);
    if (!draft.open) return node;
    panel.append(el('h3', '字段映射'));
    if (draft.applied) {
      panel.append(el('p', draft.error || '映射已保存，等待重新载入批次。', 'import-warning'), button('重新载入批次', () => run(async () => {
        try { useBatch(await api(path())); message('已载入重新识别的暂存记录；尚未发布。'); }
        catch (error) { draft.error = error.message; throw error; }
      }).finally(render))); return node;
    }
    if (!headers.length) { panel.append(el('p', '此文件未提供可配置表头。', 'import-meta')); return node; }
    const grid = el('div', undefined, 'import-grid');
    for (const [key, text] of Object.entries(LABELS)) {
      const label = el('label', text), select = el('select'); select.dataset.canonical = key;
      select.append(new Option('自动识别', ''), ...headers.map(header => new Option(header, header)));
      select.value = draft.values[key] || ''; select.disabled = state.busy || state.conflict || !editable();
      select.addEventListener('change', () => {
        if (select.value) draft.values[key] = select.value; else delete draft.values[key];
        draft.dirty = true; draft.confirm = false; draft.error = ''; render();
      }); label.append(select); grid.append(label);
    }
    panel.append(grid);
    if (draft.error) { const error = el('p', draft.error, 'import-warning'); error.setAttribute('role', 'alert'); panel.append(error); }
    const controls = el('div', undefined, 'import-mapping-actions');
    const apply = button(draft.confirm ? '确认替换并重新识别' : '保存映射并重新识别', () => {
      if (!draft.confirm) { draft.confirm = true; render(); return; }
      run(async () => {
        try {
          if (state.suspended) throw new Error('请先继续并保存未完成的记录编辑，字段映射尚未应用。');
          draft.error = '';
          await saveDraft(); message('正在保存字段映射并重新识别…');
          const batchPath = path();
          const batch = await api(`${batchPath}/files/${encodeURIComponent(file.file_id)}/parse`, 'POST', { kind: state.batch.kind, mapping: draft.values });
          state.batch = batch; state.preview = null; state.dirty = false; draft.applied = true; draft.dirty = false;
          try { useBatch(await api(batchPath)); message('字段映射已保存至账户模板，暂存记录已重新载入；尚未发布。'); }
          catch (error) { error.message = `映射已保存，但重新载入失败；请重新载入批次，不要重复解析。${error.message}`; throw error; }
        } catch (error) { draft.error = error.message; throw error; }
      }).finally(render);
    });
    apply.disabled = state.busy || state.conflict || !editable();
    if (draft.confirm) {
      const warning = el('p', '重新识别会替换此文件的暂存记录，包括已编辑内容和核对状态。其他记录保留，不会发布账目。', 'import-warning'); warning.setAttribute('role', 'alert');
      panel.append(warning); controls.append(button('取消替换', () => { draft.confirm = false; render(); }));
    }
    controls.append(apply); panel.append(controls); return node;
  }
  async function history() {
    const data = await api('/batches?limit=20'); $('batch-list').replaceChildren();
    for (const batch of data.rows || []) {
      const node = el('div', undefined, 'import-row');
      node.append(el('strong', state.accounts.find(a => a.account_code === batch.account_code)?.label || batch.account_code));
      node.append(el('p', `${TEXT[batch.status] || batch.status} · ${batch.batch_id}${batch.updated_at ? ` · ${batch.updated_at}` : ''}`, 'import-meta'));
      node.append(button('打开批次', () => run(async () => {
        if (!abandon()) return;
        useBatch(await api(`/batches/${encodeURIComponent(batch.batch_id)}`)); message('');
      }))); $('batch-list').append(node);
    }
    if (!data.rows?.length) $('batch-list').append(el('p', '暂无历史批次', 'import-meta'));
  }
  const mappingDirty = () => [...state.mappings.values()].some(mapping => mapping.dirty);
  function abandon() { return !(state.dirty || state.suspended || mappingDirty()) || window.confirm('有未保存输入，放弃本地修改？'); }
  function useBatch(batch) {
    state.batch = batch; state.preview = null; state.dirty = false; state.suspended = null; state.step = 1; state.imageIndex = null; state.conflict = false;
    state.mappings.clear();
    $('conflict').hidden = true; $('kind').value = batch.kind; renderAccounts(batch.account_code);
    $('completeness').value = batch.completeness || 'partial'; state.mode = batch.mode === 'manual' ? 'manual' : images().length ? 'ocr' : 'upload'; render();
  }
  async function ensureBatch() {
    if (state.batch) return;
    if (!account()) throw new Error('请选择服务端配置的投资账户');
    state.batch = await api('/batches', 'POST', { account_code: account().account_code, mode: state.mode === 'manual' ? 'manual' : 'upload', kind: $('kind').value, completeness: $('completeness').value });
    render();
  }
  async function saveDraft() {
    await ensureBatch();
    if (state.conflict || !editable()) throw new Error('当前批次不可修改，请重新载入或新建批次');
    if (state.dirty) {
      const payload = { revision: state.batch.revision, completeness: $('completeness').value, rows: state.batch.rows.map(row => ({ ...(row.row_id ? { row_id: row.row_id } : row.image_file_id ? {image_file_id:row.image_file_id} : {}), fields: row.fields, acknowledged: !!row.acknowledged, excluded: !!row.excluded })) };
      state.batch = await api(path(), 'PUT', payload); state.dirty = false; state.preview = null;
    }
    message('草稿已保存，尚未发布。'); render();
  }
  async function upload(files) {
    if (!files.length) return;
    if (state.suspended) throw new Error('请先继续并保存未完成的编辑，文件尚未上传。');
    const formats = files.map(file => fileFormat(file.name, $('kind').value));
    if (state.mode === 'ocr' && formats.some(format => !['png','jpg','jpeg'].includes(format))) throw new Error('截图识别仅接收 PNG、JPG、JPEG');
    if (state.mode === 'upload' && formats.some(format => ['png','jpg','jpeg'].includes(format))) throw new Error('请在截图识别方式中选择图片');
    const limits = state.caps.limits || {};
    const maxFiles = limits.files ?? limits.max_files, maxBytes = limits.file_bytes ?? limits.max_file_bytes;
    if (maxFiles && files.length + (state.batch?.files?.length || 0) > maxFiles) throw new Error('文件数量超过服务端上限');
    if (maxBytes && files.some(file => file.size > maxBytes)) throw new Error('文件大小超过服务端上限');
    await saveDraft();
    for (const file of files) {
      message(`正在识别 ${file.name}…`);
      state.batch = await api(`${path()}/files?filename=${encodeURIComponent(file.name)}`, 'POST', file, true);
      state.preview = null; render();
    }
    message('文件已暂存，请核对原始凭证和识别结果。');
  }
  function field(group, key, options, type = 'text', full = false) {
    const label = el('label', LABELS[key]); if (full) label.className = 'full';
    const input = el(options ? 'select' : 'input'); input.name = key; input.id = `field-${key}`;
    if (options) {
      input.append(new Option('请选择', ''));
      for (const value of options) input.append(new Option((key === 'kind' ? KIND_TEXT[value] : null) || ({income:'收入',expense:'支出', distinct: '核对为不同交易', skip: '跳过此条', total_charges: '税费合计', separate: '手续费与税分列' })[value] || TEXT[value] || state.counterAccounts.find(a => a.account_code === value)?.display_name || value, value));
      if (state.buffer[key] && !options.includes(state.buffer[key])) {
        const option = new Option(key === 'asset_type' ? TEXT[displayAssetType(state.buffer[key])] || state.buffer[key] : key === 'action' ? `${TEXT[state.buffer[key]] || state.buffer[key]}（当前账户不支持）` : String(state.buffer[key]), String(state.buffer[key]));
        option.disabled = key === 'action'; input.append(option);
      }
    } else { input.type = type; if (DECIMALS.includes(key)) { input.inputMode = 'decimal'; input.autocomplete = 'off'; } }
    input.value = String(state.buffer[key] ?? ''); input.disabled = !editable();
    if (['kind', 'business_date', 'currency', 'action'].includes(key)) input.required = true;
    if (key === 'business_date' && displayAssetType(state.buffer.asset_type) === 'fund' && state.buffer.kind === 'trade') label.firstChild.textContent = '申请日期';
    if (key === 'fee_amount') label.firstChild.textContent = state.buffer.fee_scope === 'total_charges' ? '税费合计' : state.buffer.fee_scope === 'separate' ? '手续费' : '费用金额';
    if (key === 'net_cash' && state.buffer.kind === 'holding') label.firstChild.textContent = '现金余额';
    label.append(input); $(group).append(label); return input;
  }
  function capture() {
    $('record-form').querySelectorAll('[name]').forEach(input => { state.buffer[input.name] = input.value; });
  }
  function rememberEditor() {
    capture(); state.suspended = { index: state.edit, fields: { ...state.buffer }, excluded: $('row-excluded').checked };
  }
  function resumeEditor() {
    const saved = state.suspended; if (!saved) return;
    openEditor(saved.index, saved);
  }
  function openEditor(index, saved) {
    if (state.conflict) return;
    if (state.suspended && !saved) { resumeEditor(); return; }
    state.edit = index;
    const row = state.batch.rows[index];
    state.buffer = { ...(saved?.fields || row?.fields || { kind: $('kind').value, asset_type: account()?.default_asset_type || '', currency: account()?.currency || '', status: 'confirmed' }) };
    const checked = state.preview?.batch_id === state.batch.batch_id && state.preview?.revision === state.batch.revision ? state.preview.rows?.find(item => item.row_id === row?.row_id) : null;
    if (!saved && row && !state.buffer.asset_type && checked?.fields?.asset_type) state.buffer.asset_type = checked.fields.asset_type;
    $('editor-account').textContent = account()?.label || state.batch.account_code;
    $('row-excluded').checked = !!(saved || row)?.excluded;
    $('row-excluded').disabled = !editable(); $('editor-error').textContent = '';
    $('asset-search').value = ''; $('asset-results').replaceChildren(); renderFields();
    const source = row?.source, file = state.batch.files?.find(item => item.file_id === source?.file_id);
    $('row-evidence').replaceChildren(evidence(file, source));
    if (row?.issues?.length) $('row-evidence').append(el('p', issues(row.issues), 'import-warning'));
    $('record-form').querySelector('[type="submit"]').hidden = !editable();
    state.suspended = null; render(); $('editor').showModal();
  }
  function renderFields() {
    for (const id of ['identity-fields', 'asset-fields', 'value-fields', 'source-fields']) $(id).replaceChildren();
    state.buffer.kind = state.batch.kind;
    field('identity-fields', 'kind', [state.batch.kind]).disabled = true; field('identity-fields', 'business_date', null, 'date');
    const f = state.buffer, holding = f.kind === 'holding', cash = f.kind === 'cash' || (holding && f.asset_type === 'cash');
    const row = state.batch.rows[state.edit], tradeActions = row?.source?.trade_actions || account()?.trade_actions;
    if (!holding && !cash) { field('identity-fields', 'trade_time'); field('identity-fields', 'status', ['pending', 'confirmed']); }
    if (displayAssetType(f.asset_type) === 'fund' && !holding && !cash) field('identity-fields', 'confirmed_date', null, 'date');
    if (f.kind !== 'cash') field('asset-fields', 'asset_type', holding ? ['stock', 'fund', 'cash'] : Object.keys(tradeActions || { stock: [], fund: [] }).filter(type => type !== 'cash' || f.asset_type === 'cash'));
    if (!cash) {
      field('asset-fields', 'symbol'); field('asset-fields', 'name', null, 'text', true);
    }
    field('asset-fields', 'currency');
    if (!holding) {
      const serverActions = tradeActions?.[displayAssetType(f.asset_type)];
      const actions = serverActions ?? (cash ? ['balance', 'deposit', 'withdraw', 'transfer'] : displayAssetType(f.asset_type) === 'fund' ? ['subscribe', 'redeem', 'dividend', 'fee'] : ['buy', 'sell', 'dividend', 'fee']);
      field('asset-fields', 'action', actions);
    }
    const keys = cash ? ['net_cash'] : holding ? ['quantity', 'price', 'market_value', 'cost'] : f.action === 'fee' ? ['fee_amount', 'tax_amount', 'net_cash'] : ['dividend', 'tax', 'interest', 'deposit', 'withdraw'].includes(f.action) ? ['gross_amount', 'fee_amount', 'tax_amount', 'net_cash'] : ['quantity', 'price', 'gross_amount', 'fee_amount', 'tax_amount', 'net_cash'];
    if (!holding && !cash) field('value-fields', 'fee_scope', ['total_charges', 'separate']);
    keys.filter(key => key !== 'tax_amount' || f.fee_scope === 'separate').forEach(key => field('value-fields', key));
    if (holding && !cash) field('value-fields', 'cost_basis_type', ['holding_cost', 'diluted_cost', 'unknown']);
    if (['deposit', 'withdraw', 'transfer'].includes(f.action)) field('value-fields', 'counter_account_code', state.counterAccounts.filter(a => a.account_code !== state.batch.account_code).map(a => a.account_code));
    field('source-fields', 'source_ref', null, 'text', true);
    field('source-fields', 'note', null, 'text', true);
    const rowIssues = [...(row?.issues || []), ...(state.preview?.rows?.find(item => item.row_id === row?.row_id)?.issues || [])];
    if (f.duplicate_decision || rowIssues.some(issue => issue.code === 'possible_duplicate' || issue === 'possible_duplicate')) field('source-fields', 'duplicate_decision', ['distinct', 'skip']);
    if (!recordEditable(row)) {
      $('record-form').querySelectorAll('[name]').forEach(input => { if (!['duplicate_decision','counter_account_code'].includes(input.name)) input.disabled = true; });
    }
    $('asset-search').disabled = cash || !editable() || !recordEditable(row);
    $('asset-lookup').hidden = cash;
    for (const key of ['asset_type', 'action', 'fee_scope']) $(`field-${key}`)?.addEventListener('change', () => {
      capture(); clearSearch(); $('asset-results').replaceChildren();
      if (key === 'asset_type' && state.buffer.kind === 'holding') {
        for (const field of ['asset_id', 'symbol', 'name', 'quantity', 'price', 'market_value', 'cost', 'cost_basis_type', 'net_cash']) delete state.buffer[field];
        $('asset-search').value = '';
      }
      renderFields();
    });
    for (const key of ['symbol', 'name', 'asset_type']) $(`field-${key}`)?.addEventListener('input', () => { delete state.buffer.asset_id; });
  }
  function editedFields() {
    capture();
    const row = state.batch.rows[state.edit];
    if (!recordEditable(row)) {
      const fields = { ...row.fields };
      for (const key of ['duplicate_decision','counter_account_code']) if ($(`field-${key}`)) fields[key] = state.buffer[key] || null;
      return fields;
    }
    const visible = [...$('record-form').querySelectorAll('[name]')].map(input => input.name);
    const fields = Object.fromEntries(Object.entries(state.buffer).filter(([key]) => key === 'asset_id' || !Object.hasOwn(LABELS, key) || visible.includes(key)));
    fields.kind = state.batch.kind;
    for (const key of DECIMALS) if (key in fields) fields[key] = $('row-excluded').checked ? String(fields[key] ?? '') || null : decimalString(fields[key]) || null;
    if (fields.fee_scope === 'total_charges') fields.tax_amount = null;
    if (fields.duplicate_decision === '') fields.duplicate_decision = null;
    if (fields.kind === 'cash') { fields.asset_type = 'cash'; delete fields.asset_id; }
    if (fields.kind === 'holding' && fields.asset_type === 'cash') { fields.action = 'balance'; delete fields.asset_id; }
    return fields;
  }
  let searchTimer, searchController, searchVersion = 0;
  function clearSearch() { clearTimeout(searchTimer); searchController?.abort(); searchVersion++; }
  $('asset-search').addEventListener('input', () => {
    clearSearch(); const version = searchVersion, query = $('asset-search').value.trim(); $('asset-results').replaceChildren();
    if (!query) return;
    searchTimer = setTimeout(async () => {
      searchController = new AbortController();
      $('asset-results').textContent = '搜索中…';
      try {
        const data = await api(`/assets?q=${encodeURIComponent(query)}`, 'GET', undefined, false, searchController.signal);
        if (version !== searchVersion || !$('editor').open) return;
        $('asset-results').replaceChildren();
        for (const asset of data.rows || []) $('asset-results').append(button(`${asset.symbol} · ${asset.name} · ${asset.currency}`, () => {
          capture(); Object.assign(state.buffer, { asset_id: asset.asset_id, symbol: asset.symbol, name: asset.name, asset_type: asset.asset_type, currency: asset.currency });
          clearSearch(); $('asset-results').replaceChildren(); renderFields();
        }));
        if (!data.rows?.length) $('asset-results').textContent = '没有匹配标的';
      } catch (error) { if (version === searchVersion) $('asset-results').textContent = error.message; }
    }, 280);
  });
  function values(title, object) {
    const section = el('div'); section.append(el('h3', title)); const list = el('dl', undefined, 'import-values');
    if (!object || !Object.keys(object).length) section.append(el('p', '无记录', 'import-meta'));
    for (const { key, label, value } of previewFields(object)) {
      const translated = key === 'kind' ? KIND_TEXT[value] : ({income:'收入',expense:'支出',operating_income:'经营收入',operating_expense:'经营支出',internal_transfer:'内部划转',investment_funding:'投资入金',investment_withdrawal:'投资出金',investment_return_cash:'投资现金收益',debt_repayment:'偿还负债',balance_adjustment:'余额调整'})[value];
      list.append(el('dt', label), el('dd', value == null || value === '' ? '未知' : typeof value === 'boolean' ? value ? '是' : '否' : typeof value === 'object' ? JSON.stringify(value) : translated || TEXT[key === 'asset_type' ? displayAssetType(value) : value] || String(value)));
    }
    section.append(list);
    if (object && Object.keys(object).length) {
      const raw = el('details', undefined, 'import-preview-raw'); raw.append(el('summary', '完整服务端记录'), el('pre', JSON.stringify(object, null, 2))); section.append(raw);
    }
    return section;
  }
  function renderPreview() {
    const p = state.preview;
    $('review-meta').textContent = `${account()?.label || state.batch.account_code} · ${TEXT[state.batch.completeness]} · 版本 ${p.revision}${p.write_count != null ? ` · 待写入 ${p.write_count} 条` : ''}`;
    $('counts').replaceChildren(...['new', 'duplicates', 'blocked', 'pending'].map(key => {
      const node = el('div'); node.append(el('dt', { new: '新增', duplicates: '重复', blocked: '阻断', pending: '待确认' }[key]), el('dd', p.counts?.[key] ?? '未知')); return node;
    }));
    const impact = p.impact || {};
    $('impact').textContent = impact.nav_estimate_status === 'known' && impact.nav_change != null ? `NAV 影响 ${impact.nav_change} · ${impact.message || ''}` : `NAV 影响暂不可用${impact.message ? ` · ${impact.message}` : ''}`;
    $('warnings').replaceChildren(...(p.warnings || []).map(warning => el('p', issues([warning]))));
    if (!state.caps.publish_enabled) $('warnings').append(el('p', '发布尚未启用；草稿可保留。'));
    $('issue-groups').replaceChildren(...(p.issue_groups || []).map(group => el('p',`${group.message} · ${group.count} 条`,'import-warning')));
    const filter = $('preview-filter').value;
    $('preview-rows').replaceChildren(...(p.rows || []).filter(row => filter === 'all' || (filter === 'excluded' ? row.status === 'excluded' : ['blocked','pending'].includes(row.status))).map(row => {
      const node = el('article', undefined, 'import-row'); node.dataset.status = row.status;
      const record = row.fields || state.batch.rows.find(item => item.row_id === row.row_id)?.fields || {};
      node.dataset.recordRole = record.action === 'balance' ? 'balance' : 'transaction';
      node.append(el('strong', `${record.symbol || '现金'} · ${record.name || ''}`), el('p', row.status === 'excluded' ? '已排除' : TEXT[row.status] || row.status, 'import-meta'));
      if (row.issues?.length) node.append(el('p', issues(row.issues), 'import-warning'));
      node.append(el('p',[record.business_date, TEXT[record.action] || ({income:'收入',expense:'支出'})[record.action],record.market_value ?? record.gross_amount ?? record.net_cash,record.currency].filter(value=>value!=null&&value!=='').join(' · '),'import-meta'));
      const allValues=el('details'); allValues.append(el('summary','前后值与底表条目'));
      const planned=(p.write_plan || []).filter(entry=>entry.row_id===row.row_id);
      if (!planned.length) allValues.append(values('待核对字段',row.fields || row.after));
      for (const entry of planned) {
        const details = el('details'); details.append(el('summary', `${entry.operation === 'update' ? '更新' : '新增'} · ${({pams_holdings_snapshot:'持仓快照',pams_investment_trade_fact:'交易事实',pams_cashflow_fact:'资金流水'})[entry.table] || entry.table}`));
        details.append(values('原值',entry.before),values('待写入',entry.after));
        const raw = el('pre',JSON.stringify(entry,null,2)); details.append(raw); allValues.append(details);
      }
      node.append(allValues);
      const index = state.batch.rows.findIndex(item => item.row_id === row.row_id);
      if (index >= 0) {
        node.append(button(recordEditable(state.batch.rows[index]) ? '编辑 / 重复判定' : '查看 / 处理', () => openEditor(index)));
        node.append(button(state.batch.rows[index].excluded ? '纳入本次清单' : '排除此条', () => run(async () => {
          state.batch.rows[index].excluded = !state.batch.rows[index].excluded; dirty(); await saveDraft(); await makePreview();
        })));
      }
      return node;
    }));
    if (state.batch.kind === 'cash') {
      const nodes = [...$('preview-rows').children];
      $('preview-rows').replaceChildren(...[['transaction','账单流水'],['balance','期末余额 · 独立确认']].flatMap(([role,title]) => {
        const rows = nodes.filter(node => node.dataset.recordRole === role);
        if (!rows.length) return [];
        const group = el('section'); group.append(el('h3',title),...rows); return [group];
      }));
    }
    render();
  }
  $('unlock-form').addEventListener('submit', event => {
    event.preventDefault(); const token = $('token').value; $('token').value = '';
    run(async () => { await api('/unlock', 'POST', { token }); await start(); });
  });
  $('retry').addEventListener('click', () => run(start));
  for (const mode of ['upload', 'ocr', 'manual']) $(`mode-${mode}`).addEventListener('click', () => {
    if (state.batch?.rows?.length && mode !== state.mode) { message('请先保留当前草稿并新建批次，再选择另一种输入方式。'); return; }
    state.mode = mode; render();
  });
  $('kind').addEventListener('change', () => { renderAccounts(); render(); });
  $('completeness').addEventListener('change', () => { if (state.batch) dirty(); render(); });
  $('files').addEventListener('change', event => { const files = [...event.target.files]; event.target.value = ''; run(() => upload(files)); });
  $('images').addEventListener('change', event => { const files = [...event.target.files]; event.target.value = ''; run(() => upload(files)); });
  $('add-row').addEventListener('click', () => run(async () => { await ensureBatch(); openEditor(-1); }));
  $('save-draft').addEventListener('click', () => run(async () => { if (state.suspended) { resumeEditor(); return; } await saveDraft(); }));
  $('record-form').addEventListener('submit', event => {
    event.preventDefault();
    if (!$('row-excluded').checked && !$('record-form').reportValidity()) return;
    run(async () => {
      const fields = editedFields(), old = state.batch.rows[state.edit];
      const row = { ...old, fields, acknowledged: true, excluded: $('row-excluded').checked || fields.duplicate_decision === 'skip',
        ...(!old && state.step===2 && state.imageIndex!==null ? {image_file_id:images()[state.imageIndex].file_id} : {}) };
      if (state.edit < 0) { state.batch.rows.push(row); state.edit = state.batch.rows.length - 1; } else state.batch.rows[state.edit] = row;
      dirty(); await saveDraft(); state.suspended = null; $('editor').close(); clearSearch();
      if (state.step === 2 && state.imageIndex === null) await makePreview();
    });
  });
  function closeEditor() { if (state.busy) return; if (editable()) rememberEditor(); clearSearch(); $('editor').close(); render(); }
  $('close-editor').addEventListener('click', closeEditor); $('keep-edit').addEventListener('click', closeEditor);
  $('editor').addEventListener('cancel', event => { event.preventDefault(); closeEditor(); });
  async function makePreview() {
    message('正在核验服务端清单…');
    state.preview = await api(`${path()}/preview`, 'POST', { revision: state.batch.revision });
    state.step = 2; state.imageIndex = null; message(''); renderPreview();
  }
  $('next').addEventListener('click', () => run(async () => {
    await saveDraft();
    if (images().length) { state.step = 2; state.imageIndex = 0; render(); } else await makePreview();
    pushFlow(); window.scrollTo(0, 0);
  }));
  $('back').addEventListener('click', () => run(goBack));
  $('flow-back').addEventListener('click', () => run(goBack));
  $('previous-image').addEventListener('click', () => run(goBack));
  $('confirm-image').addEventListener('click', () => run(async () => {
    if (state.suspended) { resumeEditor(); return; }
    const file = images()[state.imageIndex];
    state.batch = await api(`${path()}/files/${encodeURIComponent(file.file_id)}/review`, 'POST', { revision: state.batch.revision });
    if (state.imageIndex + 1 < images().length) { state.imageIndex++; render(); } else await makePreview();
    pushFlow(); window.scrollTo(0,0);
  }));
  $('preview-filter').addEventListener('change', renderPreview);
  $('close-image-viewer').addEventListener('click', () => $('image-viewer').close());
  $('publish').addEventListener('click', () => run(async () => {
    if (!publishable(state.caps, state.batch, state.preview, state.dirty) || state.conflict) return;
    const result = await api(`${path()}/publish`, 'POST', { revision: state.batch.revision, preview_token: state.preview.preview_token });
    useBatch(result.batch); message('批次已发布。'); await history();
  }));
  $('refresh-history').addEventListener('click', () => run(history));
  $('new-batch').addEventListener('click', () => {
    if (!abandon()) return;
    state.batch = null; state.preview = null; state.dirty = false; state.suspended = null; state.conflict = false; state.step = 1; state.imageIndex = null;
    state.mappings.clear();
    $('conflict').hidden = true; $('completeness').value = 'partial'; message(''); render();
  });
  $('reload').addEventListener('click', () => run(async () => {
    if (!window.confirm('重新载入会放弃本地未保存修改，继续？')) return;
    if ($('editor').open) $('editor').close();
    useBatch(await api(path())); message('已载入服务器版本。');
  }));
  window.addEventListener('beforeunload', event => { if (state.dirty || state.suspended || mappingDirty() || $('editor').open) { event.preventDefault(); event.returnValue = ''; } });
  function pushFlow() { window.history.pushState({pamsImport:true,step:state.step,imageIndex:state.imageIndex},'',state.step===1?'#input':'#review'); }
  async function goBack() {
    if ($('image-viewer').open) { $('image-viewer').close(); return; }
    if ($('editor').open) { rememberEditor(); $('editor').close(); clearSearch(); render(); return; }
    if (state.step === 2) {
      if (state.imageIndex !== null && state.imageIndex > 0) state.imageIndex--;
      else if (state.imageIndex === null && images().length) state.imageIndex = images().length-1;
      else { state.step=1; state.imageIndex=null; state.preview=null; }
      render(); window.history.replaceState({pamsImport:true,step:state.step,imageIndex:state.imageIndex},'',state.step===1?'#input':'#review'); return;
    }
    if (state.suspended || mappingDirty()) { message('请先保存或取消未完成的编辑，再返回。'); return; }
    if (state.dirty) await saveDraft();
    window.location.assign('more.html');
  }
  function renderImageReview() {
    const file=images()[state.imageIndex]; if (!file) return;
    $('image-review-title').textContent=`截图 ${state.imageIndex+1} / ${images().length}`;
    $('image-original').replaceChildren(evidence(file));
    $('image-records').replaceChildren();
    for (const [index,row] of state.batch.rows.entries()) if (row.source?.file_id===file.file_id) {
      const node=el('article',undefined,'import-row'); node.append(values(row.excluded?'已排除':'识别记录',row.fields));
      if (row.issues?.length) node.append(el('p',issues(row.issues),'import-warning'));
      node.append(button('修改本条记录',()=>openEditor(index))); $('image-records').append(node);
    }
    if (!state.batch.rows.some(row=>row.source?.file_id===file.file_id)) $('image-records').append(el('p','本图未识别出记录，请对照原图补充；空图不能确认。','import-warning'));
    $('image-records').append(button('补充本图记录',()=>openEditor(-1)));
    $('confirm-image').textContent=state.imageIndex+1===images().length?'完成核对并预检':'确认本图并继续';
  }
  window.history.replaceState({pamsImport:true,step:1,imageIndex:null},'');
  window.addEventListener('popstate', event => {
    if (!event.state?.pamsImport) return;
    if ($('editor').open) closeEditor();
    if ($('image-viewer').open) $('image-viewer').close();
    state.step=event.state.step; state.imageIndex=event.state.imageIndex;
    if (state.step===2 && state.imageIndex===null && !state.preview) { state.step=1; state.imageIndex=null; }
    render();
  });
  let edge=null;
  document.addEventListener('touchstart', event => {
    const touch=event.touches[0];
    edge=(navigator.standalone || window.matchMedia('(display-mode: standalone)').matches) && event.touches.length===1 && touch.clientX<24 &&
      !event.target.closest('input,select,textarea,button,dialog,img') ? {x:touch.clientX,y:touch.clientY} : null;
  },{passive:true});
  document.addEventListener('touchmove', event => { if (edge && event.touches[0].clientX-edge.x>40 && Math.abs(event.touches[0].clientY-edge.y)<35) event.preventDefault(); },{passive:false});
  document.addEventListener('touchend', event => { const touch=event.changedTouches[0]; if (edge && touch.clientX-edge.x>80 && Math.abs(touch.clientY-edge.y)<35) run(goBack); edge=null; },{passive:true});
  function viewport() {
    const view = window.visualViewport;
    document.body.style.setProperty('--import-view-height', `${view?.height || window.innerHeight}px`);
    document.body.style.setProperty('--import-keyboard', `${view ? Math.max(0, window.innerHeight - view.height - view.offsetTop) : 0}px`);
  }
  window.visualViewport?.addEventListener('resize', viewport); window.visualViewport?.addEventListener('scroll', viewport);
  viewport(); run(start);
}
