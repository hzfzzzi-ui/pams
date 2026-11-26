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
        <button type="button" class="importInboxBtn" data-inbox-email>拉取 Gmail 账单</button>
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
  const emailBtn = container.querySelector('[data-inbox-email]');
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
      if (data?.email_inbox?.configured) {
        parts.push(`Gmail 每日 ${data.email_inbox.poll_time || '08:30'} 自动拉取`);
      }
      if (localNew > 0) parts.push(`本地收件箱发现 ${localNew} 份新文件`);
      statusEl.textContent = parts.join(' · ');

      const drafts = data?.pending_drafts || [];
      const readyCount = drafts.filter(d => d.auto_publishable).length;
      draftsFold.hidden = drafts.length === 0;
      draftCountEl.textContent = `${drafts.length} 份`;
      const batchBar = readyCount > 0
        ? `<div class="importInboxBatchBar"><button type="button" class="importInboxBtn is-primary" data-inbox-confirm-ready>一键入账无异常草稿（${readyCount} 份）</button></div>`
        : '';
      draftListEl.innerHTML = batchBar + drafts.map(d => {
        const metaParts = [d.filename || d.source_name || ''];
        if (Number(d.item_count || 0) > 0) metaParts.push(`${d.item_count} 笔`);
        const dupTag = d.duplicate_period_hint
          ? '<span class="importInboxWarnTag">本期已有已入账账单</span>' : '';
        const confirmBtn = d.auto_publishable
          ? `<button type="button" class="importInboxMiniBtn is-confirm" data-confirm-draft="${esc(d.source_id)}">入账</button>` : '';
        return `
        <div class="importInboxRow">
          <div class="importInboxRowMain">
            <strong>${esc(d.account_label || d.account_code)} ${dupTag}</strong>
            <small>${esc(metaParts.filter(Boolean).join(' · '))}</small>
          </div>
          <div class="importInboxRowOps">
            ${confirmBtn}
            <a class="importInboxMiniLink" href="${esc(subaccountImportHref(d.account_code))}">核对 ›</a>
            <button type="button" class="importInboxMiniBtn is-discard" data-discard-draft="${esc(d.source_id)}" title="忽略此重复或过期草稿">忽略</button>
          </div>
        </div>`;
      }).join('');
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

  async function onPullEmail() {
    if (busy) return;
    busy = true;
    if (emailBtn) emailBtn.disabled = true;
    showFeedback('正在连接 Gmail 拉取近期账单邮件…');
    try {
      const fetchFn = win?.fetch?.bind(win) || fetch;
      const resp = await fetchFn('/api/import-inbox/pull-email', { method: 'POST' });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data?.detail?.message || `HTTP ${resp.status}`);
      if (data.skipped) {
        showFeedback(data.message || '尚未配置 Gmail IMAP 环境变量。', true);
      } else {
        showFeedback(`Gmail 拉取完成：扫描 ${data.fetched_messages || 0} 封邮件，新增 ${data.staged_count || 0} 份草稿，跳过重复 ${data.skipped_duplicates || 0} 份。`);
      }
      await refresh();
    } catch (err) {
      showFeedback(`Gmail 拉取失败：${err?.message || String(err)}`, true);
    } finally {
      busy = false;
      if (emailBtn) emailBtn.disabled = false;
    }
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

  async function onDraftListClick(event) {
    if (busy) return;
    const fetchFn = win?.fetch?.bind(win) || fetch;
    const batchBtn = event.target.closest('[data-inbox-confirm-ready]');
    if (batchBtn) {
      busy = true;
      batchBtn.disabled = true;
      showFeedback('正在批量确认入账无异常草稿…');
      try {
        const resp = await fetchFn('/api/import-inbox/confirm-ready', { method: 'POST' });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data?.detail?.message || `HTTP ${resp.status}`);
        showFeedback(`批量入账完成：已入账 ${data.confirmed_count || 0} 份${data.skipped?.length ? `，跳过需人工核对 ${data.skipped.length} 份` : ''}。`);
        await refresh();
      } catch (err) {
        showFeedback(`批量入账失败：${err?.message || String(err)}`, true);
      } finally {
        busy = false;
      }
      return;
    }
    const confirmBtn = event.target.closest('[data-confirm-draft]');
    if (confirmBtn) {
      const id = confirmBtn.dataset.confirmDraft;
      busy = true;
      confirmBtn.disabled = true;
      showFeedback('正在核验并确认入账…');
      try {
        const resp = await fetchFn(`/api/import-inbox/drafts/${encodeURIComponent(id)}/confirm`, { method: 'POST' });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data?.detail?.message || `HTTP ${resp.status}`);
        showFeedback(`已确认入账：${data.filename || data.account_code}`);
        await refresh();
      } catch (err) {
        showFeedback(`入账失败：${err?.message || String(err)}`, true);
      } finally {
        busy = false;
      }
      return;
    }
    const discardBtn = event.target.closest('[data-discard-draft]');
    if (discardBtn) {
      const id = discardBtn.dataset.discardDraft;
      busy = true;
      discardBtn.disabled = true;
      try {
        const resp = await fetchFn(`/api/import-inbox/drafts/${encodeURIComponent(id)}/discard`, { method: 'POST' });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data?.detail?.message || `HTTP ${resp.status}`);
        showFeedback(`已忽略草稿：${data.filename || id}（后续自动拉取将跳过此文件）`);
        await refresh();
      } catch (err) {
        showFeedback(`忽略失败：${err?.message || String(err)}`, true);
      } finally {
        busy = false;
      }
    }
  }

  fileInput?.addEventListener('change', onFilesSelected);
  emailBtn?.addEventListener('click', onPullEmail);
  scanBtn?.addEventListener('click', onScanLocal);
  draftListEl?.addEventListener('click', onDraftListClick);
  yieldForm?.addEventListener('submit', onYieldSubmit);
  void refresh();

  return {
    refresh,
    dispose() {
      alive = false;
      fileInput?.removeEventListener('change', onFilesSelected);
      emailBtn?.removeEventListener('click', onPullEmail);
      scanBtn?.removeEventListener('click', onScanLocal);
      draftListEl?.removeEventListener('click', onDraftListClick);
      yieldForm?.removeEventListener('submit', onYieldSubmit);
    },
  };
}
