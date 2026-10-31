import { KIND_TEXT, DECIMALS, LABELS, TEXT, decimalString, displayAssetType, recordEditable } from './contract.js?v=20261031-modules-v1';

// The editor owns field rendering, exact decimal strings, search cancellation,
// and suspended local input. It does not infer financial facts or publish.
export function createRecordsEditor(context) {
  const { $, state, api, el, button, account, editable, images, issues, evidence,
    render, run, dirty, saveDraft, makePreview, Option } = context;
  let active = true;
  const isActive = () => active && context.isActive();
  const listeners = [];
  function listen(target, type, callback, options) {
    listeners.push(context.listen(target, type, event => { if (isActive()) callback(event); }, options));
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
    if (!isActive() || state.conflict) return;
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
    $('record-form').querySelector('[type="submit"]').hidden = !editable() || state.needsRefresh;
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
      if (!isActive()) return;
      capture(); clearSearch(); $('asset-results').replaceChildren();
      if (key === 'asset_type' && state.buffer.kind === 'holding') {
        for (const field of ['asset_id', 'symbol', 'name', 'quantity', 'price', 'market_value', 'cost', 'cost_basis_type', 'net_cash']) delete state.buffer[field];
        $('asset-search').value = '';
      }
      renderFields();
    });
    for (const key of ['symbol', 'name', 'asset_type']) $(`field-${key}`)?.addEventListener('input', () => { if (isActive()) delete state.buffer.asset_id; });
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
  listen($('asset-search'), 'input', () => {
    clearSearch(); const version = searchVersion, query = $('asset-search').value.trim(); $('asset-results').replaceChildren();
    if (!query) return;
    searchTimer = setTimeout(async () => {
      searchController = new AbortController();
      $('asset-results').textContent = '搜索中…';
      try {
        const data = await api(`/assets?q=${encodeURIComponent(query)}`, 'GET', undefined, false, searchController.signal);
        if (!isActive() || version !== searchVersion || !$('editor').open) return;
        $('asset-results').replaceChildren();
        for (const asset of data.rows || []) $('asset-results').append(button(`${asset.symbol} · ${asset.name} · ${asset.currency}`, () => {
          capture(); Object.assign(state.buffer, { asset_id: asset.asset_id, symbol: asset.symbol, name: asset.name, asset_type: asset.asset_type, currency: asset.currency });
          clearSearch(); $('asset-results').replaceChildren(); renderFields();
        }));
        if (!data.rows?.length) $('asset-results').textContent = '没有匹配标的';
      } catch (error) { if (isActive() && version === searchVersion) $('asset-results').textContent = error.message; }
    }, 280);
  });
  listen($('record-form'), 'submit', event => {
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
  listen($('close-editor'), 'click', closeEditor); listen($('keep-edit'), 'click', closeEditor);
  listen($('editor'), 'cancel', event => { event.preventDefault(); closeEditor(); });

  return { openEditor, rememberEditor, resumeEditor, closeEditor, clearSearch,
    snapshot() {
      const fields = { ...state.buffer };
      $('record-form').querySelectorAll('[name]').forEach(input => { fields[input.name] = input.value; });
      return { index: state.edit, fields, excluded: $('row-excluded').checked };
    },
    dispose() { active = false; clearSearch(); for (const remove of listeners.splice(0)) remove?.(); } };
}
