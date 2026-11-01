import { ROOT, KIND_TEXT, DECIMALS, LABELS, TEXT, accessState, fileFormat, previewFields, displayAssetType, recordEditable, publishable } from './contract.js?v=20261031-modules-v1';
import { createImportClient } from './client.js?v=20261031-modules-v1';
import { createRequestScope } from './request_scope.js?v=20261031-modules-v1';
import { createRecordsEditor } from './records_editor.js?v=20261031-modules-v1';
import { createMappingEditor } from './mapping_editor.js?v=20261031-modules-v1';

const mounted = new WeakMap();

export function mount(root, context = {}) {
  if (!root?.querySelector) throw new TypeError('Import page root is required');
  if (mounted.has(root)) return mounted.get(root);
  const document = root.ownerDocument;
  const window = context.win || document.defaultView;
  const { location, navigator, Option } = window;
  const $ = id => root.querySelector(`#${id}`);
  const scope = createRequestScope(context.client || createImportClient(window.fetch.bind(window)), {
    recovery: context.snapshot?.recovery, getBatch: () => state.batch,
  });
  const api = scope.request;
  const cleanup = [];
  function listen(target, type, callback, options) {
    if (!target) return;
    const guarded = event => { if (scope.isActive()) callback(event); };
    target.addEventListener(type, guarded, options);
    const remove = () => target.removeEventListener(type, guarded, options);
    cleanup.push(remove);
    return remove;
  }
  const state = context.snapshot ? structuredClone(context.snapshot.state) : { caps: null, accounts: [], counterAccounts: [], batch: null, preview: null, step: 1, mode: 'upload', imageIndex: null, dirty: false, busy: false, conflict: false, edit: null, buffer: null, suspended: null, mappings: new Map(), historyRows: [], initialized: false, needsRefresh: false };
  const el = (tag, text, cls) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = String(text); if (cls) node.className = cls; return node; };
  const message = text => { $('notice').textContent = text; };
  const path = () => `/batches/${encodeURIComponent(state.batch.batch_id)}`;
  const account = () => state.accounts.find(a => a.account_code === $('account').value);
  const editable = () => !state.batch || state.batch.status === 'draft';
  const emptyDraft = () => state.batch?.status === 'draft' && Array.isArray(state.batch.rows) && !state.batch.rows.length && Array.isArray(state.batch.files) && !state.batch.files.length && !state.dirty && !state.suspended && !mappingDirty() && !$('editor').open;
  function detachEmptyDraft() {
    if (!emptyDraft()) return;
    state.batch = null; state.preview = null; state.mappings.clear();
    message('已切换账户或资料类别；原空草稿仍保留在最近批次。');
  }
  const images = () => (state.batch?.files || []).filter(file => file.content_type?.startsWith('image/'));
  const issues = list => (list || []).map(item => typeof item === 'string' ? item : item.message || item.code || JSON.stringify(item)).join('；');
  const button = (text, action) => { const node = el('button', text); node.type = 'button'; node.addEventListener('click', event => { if (scope.isActive()) action(event); }); return node; };
  const dirty = () => { state.dirty = true; state.preview = null; };
  const records = createRecordsEditor({ $, state, api, el, button, account, editable, images, issues, evidence,
    render, run, dirty, saveDraft, makePreview, listen, isActive: scope.isActive, Option });
  const { openEditor, rememberEditor, resumeEditor, closeEditor, clearSearch } = records;
  const mappings = createMappingEditor({ state, api, path, el, button, editable, render, run, useBatch,
    message, saveDraft, isActive: scope.isActive, Option });
  const mappingEditor = mappings.render, mappingDirty = mappings.isDirty;


  async function run(task) {
    if (!scope.isActive() || state.busy) return;
    state.busy = true; renderControls();
    try { await task(); }
    catch (error) {
      if (!scope.isActive()) return;
      if (error.status === 409) {
        state.conflict = true; state.preview = null; $('conflict').hidden = false;
        if ($('editor').open) { rememberEditor(); $('editor').close(); } render();
      }
      if (error.status === 403 && error.code === 'publish_disabled') {
        state.caps = { ...state.caps, publish_enabled: false }; renderAccess();
      }
      if (scope.recovery.requiresReview) {
        state.needsRefresh = true; state.preview = null; state.step = 1; state.imageIndex = null;
        if ($('editor').open) { rememberEditor(); $('editor').close(); }
        render();
      }
      message(scope.recovery.requiresReview ? recoveryNotice() : error.message);
      if ($('editor').open) $('editor-error').textContent = error.message;
    } finally { if (scope.isActive()) { state.busy = false; renderControls(); } }
  }
  function renderAccess() {
    const access = accessState(state.caps);
    $('gate').hidden = access === 'ready'; $('workspace').hidden = access !== 'ready'; $('actions').hidden = access !== 'ready';
    message(access === 'unavailable' ? '账户导入暂不可用：暂存表尚未就绪或服务暂时无法连接。' : '');
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
    await history(); state.initialized = true; render();
  }
  function renderAccounts(preferred = $('account').value) {
    const available = state.accounts.filter(a => (a.kinds || ['trade','holding','cash']).includes($('kind').value));
    $('account').replaceChildren(...available.map(a => new Option(a.label || a.account_code, a.account_code)));
    if (available.some(a => a.account_code === preferred)) $('account').value = preferred;
    if (!available.length) message('此类别暂无已配置账户。');
  }
  function renderControls() {
    const locked = state.busy || state.conflict || state.needsRefresh || !editable() || !state.accounts.length;
    $('workspace').inert = state.busy;
    $('record-form').inert = state.busy;
    $('retry').disabled = state.busy;
    const identityLocked = (!!state.batch && !emptyDraft()) || state.busy || state.conflict || state.needsRefresh;
    $('account').disabled = identityLocked;
    $('kind').disabled = identityLocked;
    for (const id of ['completeness', 'files', 'images', 'add-row', 'save-draft', 'confirm-image', 'previous-image']) $(id).disabled = locked;
    $('file-list').querySelectorAll('.import-mapping button, .import-mapping select').forEach(control => { control.disabled = locked; });
    $('next').disabled = locked || !(state.batch?.rows?.length || images().length) || !!state.suspended;
    $('publish').disabled = state.busy || state.conflict || state.needsRefresh || !publishable(state.caps, state.batch, state.preview, state.dirty);
    $('back').disabled = state.busy; $('reload').disabled = state.busy;
    $('refresh-result').disabled = state.busy;
    $('pending-result').hidden = !state.needsRefresh;
    $('new-batch').disabled = state.busy || state.needsRefresh;
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
        img.addEventListener('error', () => { if (!scope.isActive()) return; img.hidden = true; node.append(el('p', '原图加载失败，请重新解锁或打开原始文件。', 'import-warning')); }); node.append(img);
        img.tabIndex = 0;
        const open = () => { if (!scope.isActive()) return; $('full-image').src = url; $('image-viewer').showModal(); };
        img.addEventListener('click', open); img.addEventListener('keydown', event => { if (event.key === 'Enter') open(); });
      }
    }
    if (source) {
      node.append(el('p', [source.sheet, source.row != null ? `原始行 ${source.row}` : '', source.bbox ? `位置 ${JSON.stringify(source.bbox)}` : ''].filter(Boolean).join(' · '), 'import-meta'));
      if (source.raw_text) node.append(el('pre', source.raw_text));
    }
    return node;
  }
  async function history() {
    const data = await api('/batches?limit=20');
    state.historyRows = data.rows || []; renderHistory();
  }
  function renderHistory() {
    const data = { rows: state.historyRows }; $('batch-list').replaceChildren();
    for (const batch of data.rows || []) {
      const node = el('div', undefined, 'import-row');
      node.append(el('strong', state.accounts.find(a => a.account_code === batch.account_code)?.label || batch.account_code));
      node.append(el('p', `${TEXT[batch.status] || batch.status} · ${batch.batch_id}${batch.updated_at ? ` · ${batch.updated_at}` : ''}`, 'import-meta'));
      node.append(button('打开批次', () => run(async () => {
        if (!abandon()) return;
        if (useBatch(await api(`/batches/${encodeURIComponent(batch.batch_id)}`))) message('');
      }))); $('batch-list').append(node);
    }
    if (!data.rows?.length) $('batch-list').append(el('p', '暂无历史批次', 'import-meta'));
  }

  function abandon() { return !(state.dirty || state.suspended || mappingDirty()) || window.confirm('有未保存输入，放弃本地修改？'); }
  function writeUsesRevision(operation) {
    return operation?.method === 'PUT' || /\/(?:publish|review|preview)$/.test(operation?.path || '');
  }
  function recoveryNotice() {
    if (scope.hasPendingWrite()) return '上次操作仍在处理中；读取草稿不能确认结果。请等待后再载入服务器版本，期间不会继续或重复写入。';
    if (scope.recovery.last?.outcome === 'unknown') {
      if (!writeUsesRevision(scope.recovery.last)) return '上次操作结果不明，尚未取得完成回执；仅刷新草稿不足以确认。请核对服务器结果，确认前保持只读，不会重试写入。';
      return '上次操作结果不明；仍为原版本草稿不能证明未执行。请稍后重新载入核对，期间保持只读，不会重试写入。';
    }
    return '操作结果待刷新确认；没有继续或重复上次写入。请手动载入服务器版本。';
  }
  function confirmRecovery(batch) {
    if (!scope.recovery.requiresReview) return true;
    if (scope.hasPendingWrite()) return false;
    const operation = scope.recovery.last;
    if (!operation) return false;
    if (operation.outcome === 'rejected') return scope.acknowledgeRecovery();
    const expectedId = operation.result?.batchId || operation.batchId;
    if (!expectedId || batch?.batch_id !== expectedId) return false;
    if (operation.outcome === 'succeeded') {
      const expected = operation.result;
      if (['published', 'voided'].includes(expected.status) && batch.status !== expected.status) return false;
      if (expected.revision != null && (!Number.isInteger(batch.revision) || !Number.isInteger(expected.revision) || batch.revision < expected.revision)) return false;
      return scope.acknowledgeRecovery();
    }
    // A versioned request cannot commit against a newer revision. Unversioned
    // create/upload/parse outcomes need a terminal batch or explicit server evidence.
    const terminal = ['published', 'voided'].includes(batch.status);
    const superseded = writeUsesRevision(operation) && Number.isInteger(operation.revision) && Number.isInteger(batch.revision) && batch.revision > operation.revision;
    return (terminal || superseded) && scope.acknowledgeRecovery();
  }
  function useBatch(batch) {
    if (!confirmRecovery(batch)) { state.needsRefresh = true; message(recoveryNotice()); renderControls(); return false; }
    state.batch = batch; state.preview = null; state.dirty = false; state.suspended = null; state.step = 1; state.imageIndex = null; state.conflict = false; state.needsRefresh = false;
    state.mappings.clear();
    $('conflict').hidden = true; $('kind').value = batch.kind; renderAccounts(batch.account_code);
    $('completeness').value = batch.completeness || 'partial'; state.mode = batch.mode === 'manual' ? 'manual' : images().length ? 'ocr' : 'upload'; render();
    return true;
  }
  async function ensureBatch() {
    if (state.batch) return;
    if (!account()) throw new Error('请选择服务端配置的投资账户');
    state.batch = await api('/batches', 'POST', { account_code: account().account_code, mode: state.mode === 'manual' ? 'manual' : 'upload', kind: $('kind').value, completeness: $('completeness').value });
    render();
  }
  async function saveDraft() {
    if (state.needsRefresh) throw new Error('上次操作结果待刷新确认，请先载入服务器版本。');
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
    const knownImpact = impact.nav_estimate_status === 'known' && impact.nav_change != null;
    $('impact').hidden = !knownImpact;
    $('impact').textContent = knownImpact ? `NAV 影响 ${impact.nav_change} · ${impact.message || ''}` : '';
    const hiddenSummaries = ['持仓、交易、现金独立核对', '资产底表待补充/核准', '已识别的券商入出金缺少银行对手账户证据'];
    $('warnings').replaceChildren(...(p.warnings || []).filter(warning => !hiddenSummaries.some(prefix => typeof warning === 'string' && warning.startsWith(prefix))).map(warning => el('p', issues([warning]), 'import-warning')));
    const groupLabels = {counter_required:'资金待关联', possible_duplicate:'疑似重复', asset_unresolved:'标的未匹配', gross_mismatch:'金额校验异常'};
    $('issue-groups').replaceChildren(...(p.issue_groups || []).map(group => el('p',`${groupLabels[group.code] || group.message} · ${group.count} 条`,'import-warning')));
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
  listen($('retry'), 'click', () => run(start));
  for (const mode of ['upload', 'ocr', 'manual']) listen($(`mode-${mode}`), 'click', () => {
    if (state.batch?.rows?.length && mode !== state.mode) { message('请先保留当前草稿并新建批次，再选择另一种输入方式。'); return; }
    state.mode = mode; render();
  });
  listen($('account'), 'change', () => { detachEmptyDraft(); render(); });
  listen($('kind'), 'change', () => { detachEmptyDraft(); renderAccounts(); render(); });
  listen($('completeness'), 'change', () => { if (state.batch) dirty(); render(); });
  listen($('files'), 'change', event => { const files = [...event.target.files]; event.target.value = ''; run(() => upload(files)); });
  listen($('images'), 'change', event => { const files = [...event.target.files]; event.target.value = ''; run(() => upload(files)); });
  listen($('add-row'), 'click', () => run(async () => { await ensureBatch(); openEditor(-1); }));
  listen($('save-draft'), 'click', () => run(async () => { if (state.suspended) { resumeEditor(); return; } await saveDraft(); }));
  async function makePreview() {
    message('正在核验服务端清单…');
    state.preview = await api(`${path()}/preview`, 'POST', { revision: state.batch.revision });
    state.step = 2; state.imageIndex = null; message(''); renderPreview();
  }
  listen($('next'), 'click', () => run(async () => {
    await saveDraft();
    if (images().length) { state.step = 2; state.imageIndex = 0; render(); } else await makePreview();
    pushFlow(); window.scrollTo(0, 0);
  }));
  listen($('back'), 'click', () => run(goBack));
  listen($('flow-back'), 'click', () => run(goBack));
  listen($('previous-image'), 'click', () => run(goBack));
  listen($('confirm-image'), 'click', () => run(async () => {
    if (state.suspended) { resumeEditor(); return; }
    const file = images()[state.imageIndex];
    state.batch = await api(`${path()}/files/${encodeURIComponent(file.file_id)}/review`, 'POST', { revision: state.batch.revision });
    if (state.imageIndex + 1 < images().length) { state.imageIndex++; render(); } else await makePreview();
    pushFlow(); window.scrollTo(0,0);
  }));
  listen($('preview-filter'), 'change', renderPreview);
  listen($('close-image-viewer'), 'click', () => $('image-viewer').close());
  listen($('publish'), 'click', () => run(async () => {
    if (state.needsRefresh || !publishable(state.caps, state.batch, state.preview, state.dirty) || state.conflict) return;
    const result = await api(`${path()}/publish`, 'POST', { revision: state.batch.revision, preview_token: state.preview.preview_token });
    useBatch(result.batch); message('批次已发布。'); await history();
  }));
  listen($('refresh-history'), 'click', () => run(history));
  listen($('new-batch'), 'click', () => {
    if (state.needsRefresh) { message(recoveryNotice()); return; }
    if (!abandon()) return;
    state.batch = null; state.preview = null; state.dirty = false; state.suspended = null; state.conflict = false; state.needsRefresh = false; state.step = 1; state.imageIndex = null;
    state.mappings.clear();
    $('conflict').hidden = true; $('completeness').value = 'partial'; message(''); render();
  });
  async function reloadLatest() {
    if (!window.confirm('重新载入会放弃本地未保存修改，继续？')) return;
    if (!state.batch) {
      await history(); $('history').open = true;
      if (scope.recovery.last?.outcome === 'rejected' && scope.acknowledgeRecovery()) {
        state.needsRefresh = false; renderControls();
        message('上次新建请求已被服务器拒绝；已刷新历史批次，可重新新建。');
      } else {
        message(`${state.needsRefresh ? recoveryNotice() + ' ' : ''}已刷新历史批次，请打开对应批次确认上次操作结果。`);
      }
      return;
    }
    if ($('editor').open) $('editor').close();
    if (useBatch(await api(path()))) message('已载入服务器版本。');
  }
  listen($('reload'), 'click', () => run(reloadLatest));
  listen($('refresh-result'), 'click', () => run(reloadLatest));
  listen(window, 'beforeunload', event => { if (state.dirty || state.suspended || mappingDirty() || $('editor').open) { event.preventDefault(); event.returnValue = ''; } });
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
  if (!context.snapshot) window.history.replaceState({pamsImport:true,step:1,imageIndex:null},'');
  listen(window, 'popstate', event => {
    if (!event.state?.pamsImport) return;
    if ($('editor').open) closeEditor();
    if ($('image-viewer').open) $('image-viewer').close();
    state.step=event.state.step; state.imageIndex=event.state.imageIndex;
    if (state.step===2 && state.imageIndex===null && !state.preview) { state.step=1; state.imageIndex=null; }
    render();
  });
  let edge=null;
  listen(root, 'touchstart', event => {
    const touch=event.touches[0];
    edge=(navigator.standalone || window.matchMedia('(display-mode: standalone)').matches) && event.touches.length===1 && touch.clientX<24 &&
      !event.target.closest('input,select,textarea,button,dialog,img') ? {x:touch.clientX,y:touch.clientY} : null;
  },{passive:true});
  listen(root, 'touchmove', event => { if (edge && event.touches[0].clientX-edge.x>40 && Math.abs(event.touches[0].clientY-edge.y)<35) event.preventDefault(); },{passive:false});
  listen(root, 'touchend', event => { const touch=event.changedTouches[0]; if (edge && touch.clientX-edge.x>80 && Math.abs(touch.clientY-edge.y)<35) run(goBack); edge=null; },{passive:true});
  function viewport() {
    const view = window.visualViewport;
    root.style.setProperty('--import-view-height', `${view?.height || window.innerHeight}px`);
    root.style.setProperty('--import-keyboard', `${view ? Math.max(0, window.innerHeight - view.height - view.offsetTop) : 0}px`);
  }
  listen(window.visualViewport, 'resize', viewport); listen(window.visualViewport, 'scroll', viewport);

  // Snapshots stay in this page's memory only. Original files, tokens and inputs
  // never enter web storage or a shared request cache.
  function snapshot() {
    const editorOpen = $('editor').open;
    const editorState = editorOpen ? records.snapshot() : null;
    const saved = structuredClone(state);
    if (editorOpen && editable()) saved.suspended = editorState;
    saved.busy = false;
    if (scope.hasPendingWrite()) {
      saved.needsRefresh = true;
      saved.preview = null;
      saved.step = 1;
      saved.imageIndex = null;
    }
    return { state: saved, recovery: scope.recovery, editorOpen: editorOpen && !saved.needsRefresh, editorState,
      selectedAccount: $('account').value, kind: $('kind').value,
      completeness: $('completeness').value, filter: $('preview-filter').value,
      notice: $('notice').textContent, imageOpen: $('image-viewer').open,
      imageSource: $('full-image').getAttribute('src') };
  }
  let savedOnExit;
  function dispose() {
    if (!scope.isActive()) return savedOnExit;
    savedOnExit = snapshot();
    scope.dispose();
    records.dispose();
    for (const remove of cleanup.splice(0)) remove();
    if ($('editor').open) $('editor').close();
    if ($('image-viewer').open) $('image-viewer').close();
    if (mounted.get(root) === instance) mounted.delete(root);
    return savedOnExit;
  }
  function restore() {
    if (!scope.isActive()) return;
    const saved = context.snapshot;
    $('kind').value = saved.kind;
    renderAccounts(saved.selectedAccount);
    $('completeness').value = saved.completeness;
    $('preview-filter').value = saved.filter;
    renderAccess(); renderHistory();
    $('conflict').hidden = !state.conflict;
    if (state.step === 2 && state.imageIndex === null && state.preview) renderPreview(); else render();
    message(state.needsRefresh ? recoveryNotice() : saved.notice);
    if (saved.editorOpen && saved.editorState) openEditor(saved.editorState.index, saved.editorState);
    if (saved.imageOpen && saved.imageSource) { $('full-image').src = saved.imageSource; $('image-viewer').showModal(); }
  }
  const instance = { dispose, snapshot, ready: null };
  mounted.set(root, instance);
  viewport();
  instance.ready = context.snapshot?.state.initialized
    ? Promise.resolve().then(restore)
    : run(start);
  return instance;
}

export function dispose(root) { return mounted.get(root)?.dispose(); }

