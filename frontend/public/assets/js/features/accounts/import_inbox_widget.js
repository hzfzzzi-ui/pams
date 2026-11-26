/* Unified import inbox widget mounted at the top of Account Center. */

import { subaccountImportHref } from '../../app/routes.js';

const DEFAULT_VISIBLE = 5;

function esc(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function renderDraftRow(d) {
  const metaParts = [d.filename || d.source_name || ''];
  if (Number(d.item_count || 0) > 0) metaParts.push(`${d.item_count} 笔`);
  const dupTag = d.duplicate_period_hint
    ? '<span class="importInboxWarnTag">本期已入账</span>' : '';
  const confirmBtn = d.auto_publishable
    ? `<button type="button" class="importInboxMiniBtn is-confirm" data-confirm-draft="${esc(d.source_id)}">入账</button>` : '';
  const href = subaccountImportHref(d.account_code, d.source_id, Boolean(d.duplicate_period_hint));
  return `
    <div class="importInboxRow">
      <div class="importInboxRowMain">
        <strong>${esc(d.account_label || d.account_code)} ${dupTag}</strong>
        <small>${esc(metaParts.filter(Boolean).join(' · '))}</small>
      </div>
      <div class="importInboxRowOps">
        ${confirmBtn}
        <a class="importInboxMiniLink" href="${esc(href)}">核对 ›</a>
        <button type="button" class="importInboxMiniBtn is-discard" data-discard-draft="${esc(d.source_id)}" title="忽略此草稿">忽略</button>
      </div>
    </div>`;
}

export function mountImportInbox(container, { get, win }) {
  if (!container) return { refresh: () => {}, dispose: () => {} };
  let alive = true;
  let busy = false;
  let expandFresh = false;
  let expandStale = false;
  let lastData = null;

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
          <span>上传账单 / 截图</span>
          <input type="file" multiple accept=".pdf,.xls,.xlsx,.csv,.tsv,.zip,.png,.jpg,.jpeg" data-inbox-files hidden />
        </label>
        <button type="button" class="importInboxBtn" data-inbox-email>同步 Gmail</button>
      </div>
      <p class="importInboxFeedback" data-inbox-feedback hidden role="status"></p>
      <details class="importInboxFold" data-inbox-fresh-fold hidden open>
        <summary><span>待入账账单</span><small data-inbox-fresh-count>0</small></summary>
        <div class="importInboxList" data-inbox-fresh-list></div>
      </details>
      <details class="importInboxFold" data-inbox-stale-fold hidden>
        <summary><span>重复 / 历史草稿</span><small data-inbox-stale-count>0</small></summary>
        <div class="importInboxList" data-inbox-stale-list></div>
      </details>
      <details class="importInboxFold" data-inbox-tools-fold>
        <summary><span>更多工具</span><small>本地目录 · 货基月末轧差</small></summary>
        <div class="importInboxToolsBody">
          <div class="importInboxToolRow">
            <span class="importInboxToolMeta" data-inbox-local-meta>扫描本地 _Inbox 目录自动识别</span>
            <button type="button" class="importInboxMiniBtn" data-inbox-scan>扫描本地</button>
          </div>
          <form class="importYieldForm" data-yield-form>
            <div class="importInboxToolTitle">零钱通 / 活期+ 月末收益轧差</div>
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
        </div>
      </details>
    </section>
  `;

  const statusEl = container.querySelector('[data-inbox-status]');
  const feedbackEl = container.querySelector('[data-inbox-feedback]');
  const fileInput = container.querySelector('[data-inbox-files]');
  const emailBtn = container.querySelector('[data-inbox-email]');
  const scanBtn = container.querySelector('[data-inbox-scan]');
  const localMetaEl = container.querySelector('[data-inbox-local-meta]');
  const freshFold = container.querySelector('[data-inbox-fresh-fold]');
  const freshCountEl = container.querySelector('[data-inbox-fresh-count]');
  const freshListEl = container.querySelector('[data-inbox-fresh-list]');
  const staleFold = container.querySelector('[data-inbox-stale-fold]');
  const staleCountEl = container.querySelector('[data-inbox-stale-count]');
  const staleListEl = container.querySelector('[data-inbox-stale-list]');
  const yieldForm = container.querySelector('[data-yield-form]');
  const yieldOut = container.querySelector('[data-yield-result]');

  function showFeedback(text, isError = false) {
    if (!feedbackEl) return;
    feedbackEl.hidden = !text;
    feedbackEl.textContent = text || '';
    feedbackEl.classList.toggle('is-error', Boolean(isError));
  }

  function renderDraftSections(data) {
    const drafts = data?.pending_drafts || [];
    const freshDrafts = drafts.filter(d => !d.duplicate_period_hint && d.acquisition_method === 'file');
    const staleDrafts = drafts.filter(d => d.duplicate_period_hint || d.acquisition_method !== 'file');
    const dupCount = staleDrafts.filter(d => d.duplicate_period_hint).length;
    const readyCount = freshDrafts.filter(d => d.auto_publishable).length;
    const localNew = Number(data?.local_inbox?.new_count || 0);

    const parts = [];
    if (freshDrafts.length > 0) parts.push(`${freshDrafts.length} 份待入账`);
    else parts.push('暂无待入账新账单');
    if (staleDrafts.length > 0) parts.push(`${staleDrafts.length} 份重复/历史草稿`);
    if (data?.email_inbox?.configured) {
      parts.push(`Gmail 每日 ${data.email_inbox.poll_time || '08:30'} 自动同步`);
    }
    statusEl.textContent = parts.join(' · ');
    if (localMetaEl) {
      localMetaEl.textContent = localNew > 0
        ? `本地 _Inbox 发现 ${localNew} 份新文件`
        : '扫描本地 _Inbox 目录自动识别';
    }

    freshFold.hidden = freshDrafts.length === 0;
    freshCountEl.textContent = `${freshDrafts.length} 份`;
    const visibleFresh = expandFresh ? freshDrafts : freshDrafts.slice(0, DEFAULT_VISIBLE);
    const batchBar = readyCount > 0
      ? `<div class="importInboxBatchBar"><button type="button" class="importInboxBtn is-primary" data-inbox-confirm-ready>一键入账无异常草稿（${readyCount} 份）</button></div>`
      : '';
    const freshMore = freshDrafts.length > DEFAULT_VISIBLE
      ? `<button type="button" class="importInboxMoreBtn" data-toggle-more="fresh">${expandFresh ? '收起列表' : `展开全部（共 ${freshDrafts.length} 份）`}</button>`
      : '';
    freshListEl.innerHTML = batchBar + visibleFresh.map(renderDraftRow).join('') + freshMore;

    staleFold.hidden = staleDrafts.length === 0;
    staleCountEl.textContent = `${staleDrafts.length} 份`;
    const visibleStale = expandStale ? staleDrafts : staleDrafts.slice(0, DEFAULT_VISIBLE);
    const staleActions = [];
    if (dupCount > 0) {
      staleActions.push(`<button type="button" class="importInboxMiniBtn" data-discard-bulk="duplicates">一键忽略重复账单（${dupCount}）</button>`);
    }
    if (staleDrafts.length > dupCount) {
      staleActions.push(`<button type="button" class="importInboxMiniBtn is-discard" data-discard-bulk="stale">清空全部重复/历史草稿（${staleDrafts.length}）</button>`);
    }
    const staleBar = staleActions.length
      ? `<div class="importInboxBatchBar">${staleActions.join('')}</div>` : '';
    const staleMore = staleDrafts.length > DEFAULT_VISIBLE
      ? `<button type="button" class="importInboxMoreBtn" data-toggle-more="stale">${expandStale ? '收起列表' : `展开全部（共 ${staleDrafts.length} 份）`}</button>`
      : '';
    staleListEl.innerHTML = staleBar + visibleStale.map(renderDraftRow).join('') + staleMore;
  }

  async function refresh() {
    if (!alive) return;
    try {
      const data = await get('/api/import-inbox/summary', { cache: false });
      if (!alive) return;
      lastData = data;
      renderDraftSections(data);
    } catch {
      if (alive) statusEl.textContent = '支持上传账单 PDF / XLS / CSV / 截图自动识别账户';
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
      showFeedback(`已自动识别并生成 ${ok} 份草稿，请确认入账。`);
    }
    await refresh();
  }

  async function onPullEmail() {
    if (busy) return;
    busy = true;
    if (emailBtn) emailBtn.disabled = true;
    showFeedback('正在同步 Gmail 近期账单邮件…');
    try {
      const fetchFn = win?.fetch?.bind(win) || fetch;
      const resp = await fetchFn('/api/import-inbox/pull-email', { method: 'POST' });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data?.detail?.message || `HTTP ${resp.status}`);
      if (data.skipped) {
        showFeedback(data.message || '尚未配置 Gmail IMAP 环境变量。', true);
      } else {
        showFeedback(`Gmail 同步完成：扫描 ${data.fetched_messages || 0} 封邮件，新增 ${data.staged_count || 0} 份草稿，跳过重复 ${data.skipped_duplicates || 0} 份。`);
      }
      await refresh();
    } catch (err) {
      showFeedback(`Gmail 同步失败：${err?.message || String(err)}`, true);
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

  async function onCardClick(event) {
    const toggleBtn = event.target.closest('[data-toggle-more]');
    if (toggleBtn) {
      if (toggleBtn.dataset.toggleMore === 'fresh') expandFresh = !expandFresh;
      else expandStale = !expandStale;
      if (lastData) renderDraftSections(lastData);
      return;
    }
    if (busy) return;
    const fetchFn = win?.fetch?.bind(win) || fetch;

    const bulkDiscard = event.target.closest('[data-discard-bulk]');
    if (bulkDiscard) {
      const scope = bulkDiscard.dataset.discardBulk;
      if (scope === 'stale') {
        const confirmFn = win?.confirm?.bind(win) || window.confirm;
        if (!confirmFn('将作废所有重复及历史遗留草稿（不影响任何已入账账本），继续？')) return;
      }
      busy = true;
      bulkDiscard.disabled = true;
      showFeedback('正在清理草稿…');
      try {
        const resp = await fetchFn(`/api/import-inbox/discard-duplicates?scope=${encodeURIComponent(scope)}`, { method: 'POST' });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data?.detail?.message || `HTTP ${resp.status}`);
        showFeedback(`已清理 ${data.discarded_count || 0} 份草稿（不影响已入账账本）。`);
        await refresh();
      } catch (err) {
        showFeedback(`清理失败：${err?.message || String(err)}`, true);
      } finally {
        busy = false;
      }
      return;
    }

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
        showFeedback(`已忽略草稿：${data.filename || id}`);
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
  container.addEventListener('click', onCardClick);
  yieldForm?.addEventListener('submit', onYieldSubmit);
  void refresh();

  return {
    refresh,
    dispose() {
      alive = false;
      fileInput?.removeEventListener('change', onFilesSelected);
      emailBtn?.removeEventListener('click', onPullEmail);
      scanBtn?.removeEventListener('click', onScanLocal);
      container.removeEventListener('click', onCardClick);
      yieldForm?.removeEventListener('submit', onYieldSubmit);
    },
  };
}
