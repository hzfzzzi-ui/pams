/* Unified import inbox widget mounted at the top of Account Center. */

import { subaccountImportHref } from '../../app/routes.js';

function esc(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

export function mountImportInbox(container, { get, win }) {
  if (!container) return { refresh: () => {}, dispose: () => {} };
  let alive = true;
  let busy = false;

  container.innerHTML = `
    <section class="importInboxCard" aria-label="统一导入收件箱">
      <div class="importInboxHead">
        <div>
          <h3>导入收件箱</h3>
          <p class="importInboxSub" data-inbox-status>正在检查待核对草稿…</p>
        </div>
      </div>
      <div class="importInboxActions">
        <label class="importInboxBtn is-primary">
          <span>批量甩文件 / 截图</span>
          <input type="file" multiple accept=".pdf,.xls,.xlsx,.csv,.tsv,.zip,.png,.jpg,.jpeg" data-inbox-files hidden />
        </label>
        <button type="button" class="importInboxBtn" data-inbox-scan>扫描本地收件箱</button>
      </div>
      <p class="importInboxFeedback" data-inbox-feedback hidden role="status"></p>
      <details class="importInboxFold" data-inbox-drafts-fold hidden>
        <summary><span>待核对草稿</span><small data-inbox-draft-count>0</small></summary>
        <div class="importInboxList" data-inbox-draft-list></div>
      </details>
      <details class="importInboxFold" data-inbox-yield-fold>
        <summary><span>零钱通 / 活期+ 月末收益轧差</span><small>免截明细图</small></summary>
        <form class="importYieldForm" data-yield-form>
          <div class="importYieldGrid">
            <label>上月末余额<input type="number" step="0.01" name="prev" placeholder="10000.00" required /></label>
            <label>本月末余额<input type="number" step="0.01" name="curr" placeholder="12028.45" required /></label>
            <label>本月净转入<input type="number" step="0.01" name="net" value="0" placeholder="0.00" /></label>
          </div>
          <div class="importYieldFoot">
            <button type="submit" class="importInboxBtn is-primary">计算本月收益</button>
            <output class="importYieldResult" data-yield-result></output>
          </div>
        </form>
      </details>
    </section>
  `;

  const statusEl = container.querySelector('[data-inbox-status]');
  const feedbackEl = container.querySelector('[data-inbox-feedback]');
  const fileInput = container.querySelector('[data-inbox-files]');
  const scanBtn = container.querySelector('[data-inbox-scan]');
  const draftsFold = container.querySelector('[data-inbox-drafts-fold]');
  const draftCountEl = container.querySelector('[data-inbox-draft-count]');
  const draftListEl = container.querySelector('[data-inbox-draft-list]');
  const yieldForm = container.querySelector('[data-yield-form]');
  const yieldOut = container.querySelector('[data-yield-result]');

  function showFeedback(text, isError = false) {
    if (!feedbackEl) return;
    feedbackEl.hidden = !text;
    feedbackEl.textContent = text || '';
    feedbackEl.classList.toggle('is-error', Boolean(isError));
  }

  async function refresh() {
    if (!alive) return;
    try {
      const data = await get('/api/import-inbox/summary', { cache: false });
      if (!alive) return;
      const pending = Number(data?.pending_count || 0);
      const localNew = Number(data?.local_inbox?.new_count || 0);
      const parts = [];
      parts.push(pending > 0 ? `${pending} 份草稿待核对` : '暂无待核对草稿');
      if (localNew > 0) parts.push(`本地收件箱发现 ${localNew} 份新文件`);
      statusEl.textContent = parts.join(' · ');

      const drafts = data?.pending_drafts || [];
      draftsFold.hidden = drafts.length === 0;
      draftCountEl.textContent = `${drafts.length} 份`;
      draftListEl.innerHTML = drafts.map(d => `
        <a class="importInboxRow" href="${esc(subaccountImportHref(d.account_code))}">
          <div>
            <strong>${esc(d.account_label || d.account_code)}</strong>
            <small>${esc(d.filename || d.source_name || '')}</small>
          </div>
          <span>去核对 ›</span>
        </a>
      `).join('');
    } catch {
      if (alive) statusEl.textContent = '支持直接丢账单 PDF / XLS / CSV / 截图自动识别账户';
    }
  }

  async function onFilesSelected() {
    const files = [...(fileInput.files || [])];
    fileInput.value = '';
    if (!files.length || busy) return;
    busy = true;
    showFeedback(`正在自动识别并导入 ${files.length} 份文件…`);
    let ok = 0;
    const failed = [];
    const fetchFn = win?.fetch?.bind(win) || fetch;
    for (const file of files) {
      try {
        const qs = new URLSearchParams({ filename: file.name, rel_path: file.webkitRelativePath || '' });
        const resp = await fetchFn(`/api/import-inbox/upload?${qs}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/octet-stream' },
          body: await file.arrayBuffer(),
        });
        const payload = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(payload?.detail?.message || `HTTP ${resp.status}`);
        ok += (payload.staged || []).length || 1;
      } catch (err) {
        failed.push(`${file.name}（${err?.message || '识别失败'}）`);
      }
    }
    busy = false;
    if (failed.length) {
      showFeedback(`已生成 ${ok} 份草稿；${failed.length} 份需手动选择账户：${failed.join('、')}`, true);
    } else {
      showFeedback(`已自动识别并生成 ${ok} 份草稿，请点下方「待核对草稿」确认入库。`);
    }
    await refresh();
  }

  async function onScanLocal() {
    if (busy) return;
    busy = true;
    scanBtn.disabled = true;
    showFeedback('正在扫描本地 _Inbox 目录…');
    try {
      const fetchFn = win?.fetch?.bind(win) || fetch;
      const resp = await fetchFn('/api/import-inbox/scan-local', { method: 'POST' });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data?.detail?.message || `HTTP ${resp.status}`);
      showFeedback(`扫描完成：新增 ${data.staged_count || 0} 份草稿，跳过重复 ${data.skipped_duplicates || 0} 份。`);
      await refresh();
    } catch (err) {
      showFeedback(`扫描失败：${err?.message || String(err)}`, true);
    } finally {
      busy = false;
      scanBtn.disabled = false;
    }
  }

  async function onYieldSubmit(event) {
    event.preventDefault();
    const fd = new FormData(yieldForm);
    try {
      const fetchFn = win?.fetch?.bind(win) || fetch;
      const resp = await fetchFn('/api/import-inbox/cash-yield', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          previous_balance: String(fd.get('prev') || '0'),
          current_balance: String(fd.get('curr') || '0'),
          net_transfers: String(fd.get('net') || '0'),
        }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data?.detail?.message || '计算失败');
      yieldOut.textContent = `本月货基收益：¥${data.implied_yield}${data.needs_review ? '（为负请核对净转入）' : ''}`;
      yieldOut.classList.toggle('is-warn', Boolean(data.needs_review));
    } catch (err) {
      yieldOut.textContent = err?.message || '输入格式有误';
    }
  }

  fileInput?.addEventListener('change', onFilesSelected);
  scanBtn?.addEventListener('click', onScanLocal);
  yieldForm?.addEventListener('submit', onYieldSubmit);
  void refresh();

  return {
    refresh,
    dispose() {
      alive = false;
      fileInput?.removeEventListener('change', onFilesSelected);
      scanBtn?.removeEventListener('click', onScanLocal);
      yieldForm?.removeEventListener('submit', onYieldSubmit);
    },
  };
}
