import { dataStateHtml } from '../../ui.js?v=20261031-ui-v1';

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[ch]));
}

function statusClass(status) {
  if (status === 'error') return 'risk';
  if (status === 'review' || status === 'missing') return 'warn';
  return 'data';
}

function fmtCount(value) {
  const n = Number(value || 0);
  return n.toLocaleString('zh-CN', { maximumFractionDigits: 0 });
}

export function fmtSyncTime(value) {
  if (!value) return '--';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return parsed.toLocaleString('zh-CN', {
    timeZone: 'Asia/Shanghai',
    hour12: false,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    timeZoneName: 'short',
  });
}

function shortTime(row) {
  if (row.latest_time) return fmtSyncTime(row.latest_time);
  return row.latest_date || '--';
}

function tag(text, type) {
  return `<span class="tag ${statusClass(type)}">${esc(text)}</span>`;
}

export function rowDetail(row) {
  const canTrigger = row.trigger_endpoint;
  return `
    <div class="syncSheetGrid">
      <div><span>数据源</span><strong>${esc(row.source)}</strong></div>
      <div><span>同步类型</span><strong>${esc(row.sync_type)}</strong></div>
      <div><span>最新时间</span><strong class="mono">${esc(shortTime(row))}</strong></div>
      <div><span>记录数</span><strong class="mono">${fmtCount(row.rows)}</strong></div>
    </div>
    <div class="syncSheetBlock">
      <span>当前状态</span>
      <strong>${esc(row.status_label)}</strong>
    </div>
    <div class="syncSheetBlock">
      <span>下一步动作</span>
      <strong>${esc(row.action)}</strong>
    </div>
    <div class="syncSheetBlock">
      <span>口径</span>
      <p>${esc(row.details || '暂无补充说明')}</p>
    </div>
    <div class="syncSheetActions">
      ${canTrigger ? `<button class="btn primary" type="button" data-sync-trigger="${esc(row.id)}">立即刷新</button>` : ''}
      <a class="btn" href="${esc(row.href || 'data_sources.html')}">打开页面</a>
    </div>
  `;
}

function renderRow(row) {
  const tone = statusClass(row.status);
  return `
    <button class="syncCenterRow ${tone}" type="button" data-sync-row="${esc(row.id)}">
      <span class="syncCenterMain">
        <strong>${esc(row.source)}</strong>
        <small>${esc(row.sync_type)} · ${esc(row.action)}</small>
      </span>
      <span class="syncCenterMeta">
        <b class="mono">${esc(shortTime(row))}</b>
        ${tag(row.status_label, row.status)}
      </span>
    </button>
  `;
}

function renderTableRow(row) {
  return `
    <tr data-sync-row="${esc(row.id)}">
      <td><strong>${esc(row.source)}</strong></td>
      <td>${esc(row.sync_type)}</td>
      <td class="mono">${esc(shortTime(row))}</td>
      <td class="num mono">${fmtCount(row.rows)}</td>
      <td>${tag(row.status_label, row.status)}</td>
      <td>${row.trigger_endpoint ? '<button class="miniBtn" type="button" data-sync-inline>刷新</button>' : `<a class="miniLink" href="${esc(row.href || '#')}">查看</a>`}</td>
    </tr>
  `;
}

export function renderSyncSummary(el, data) {
  if (!el) return;
  const s = data.summary || {};
  el.innerHTML = `
    <div class="syncMetric"><span>正常</span><strong>${fmtCount(s.ok)}</strong></div>
    <div class="syncMetric warn"><span>待人工</span><strong>${fmtCount(s.manual_required)}</strong></div>
    <div class="syncMetric risk"><span>异常</span><strong>${fmtCount(s.errors)}</strong></div>
    <div class="syncMetric"><span>投资日期</span><strong>${esc(s.last_investment_date || '--')}</strong></div>
  `;
}

export function renderWorkflow(el, data) {
  if (!el) return;
  const workflows = data.workflows || [];
  if (!workflows.length) {
    el.innerHTML = dataStateHtml({
      tone: 'empty',
      title: '暂无今日工作流',
      detail: '当前没有需要展示的同步步骤。',
      compact: true,
    });
    return;
  }
  el.innerHTML = workflows.map((item) => `
    <button class="syncWorkflowPill" type="button" title="${esc(item.description)}">
      <strong>${esc(item.label)}</strong>
      <small>${esc(item.description)}</small>
    </button>
  `).join('');
}

export function renderSyncRows(el, data) {
  if (!el) return;
  const rows = data.rows || [];
  if (!rows.length) {
    el.innerHTML = dataStateHtml({
      tone: 'empty',
      title: '暂无同步数据',
      detail: '当前没有可展示的数据源同步记录。',
      compact: true,
    });
    return;
  }
  el.innerHTML = rows.map(renderRow).join('') || '<div class="muted">暂无同步数据</div>';
}

export function renderSyncTable(tbody, data) {
  if (!tbody) return;
  const rows = data.rows || [];
  tbody.innerHTML = rows.map(renderTableRow).join('');
}

export function setStatus(statusEl, text, tone = '') {
  if (!statusEl) return;
  statusEl.className = `manualSyncStatus ${tone}`.trim();
  statusEl.textContent = text;
}
