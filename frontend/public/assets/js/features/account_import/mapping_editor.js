import { LABELS } from './contract.js?v=20261031-import-v1';

// Mapping drafts and explicit two-click replacement retain the server protocol.
export function createMappingEditor(context) {
  const { state, api, path, el, button, editable, render, run, useBatch, message,
    saveDraft, isActive, Option } = context;
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
      }).finally(() => { if (isActive()) render(); }))); return node;
    }
    if (!headers.length) { panel.append(el('p', '此文件未提供可配置表头。', 'import-meta')); return node; }
    const grid = el('div', undefined, 'import-grid');
    for (const [key, text] of Object.entries(LABELS)) {
      const label = el('label', text), select = el('select'); select.dataset.canonical = key;
      select.append(new Option('自动识别', ''), ...headers.map(header => new Option(header, header)));
      select.value = draft.values[key] || ''; select.disabled = state.busy || state.conflict || !editable();
      select.addEventListener('change', () => {
        if (!isActive()) return;
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
      }).finally(() => { if (isActive()) render(); });
    });
    apply.disabled = state.busy || state.conflict || !editable();
    if (draft.confirm) {
      const warning = el('p', '重新识别会替换此文件的暂存记录，包括已编辑内容和核对状态。其他记录保留，不会发布账目。', 'import-warning'); warning.setAttribute('role', 'alert');
      panel.append(warning); controls.append(button('取消替换', () => { draft.confirm = false; render(); }));
    }
    controls.append(apply); panel.append(controls); return node;
  }
  return {
    render: mappingEditor,
    isDirty: () => [...state.mappings.values()].some(mapping => mapping.dirty),
  };
}
