function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[ch]));
}

export function dataStateHtml(options = {}) {
  const tone = options.tone || 'empty';
  const title = options.title || '暂无数据';
  const detail = options.detail || '';
  const badge = options.badge || {
    error: '失败',
    loading: '加载中',
    stale: '待更新',
    empty: '暂无',
  }[tone] || '状态';
  const badgeTone = {
    error: 'warn',
    loading: 'pending',
    stale: 'warn',
    empty: 'data',
  }[tone] || 'data';
  const stateRole = tone === 'error' ? 'alert' : 'status';
  const liveMode = tone === 'error' ? 'assertive' : 'polite';
  const compact = options.compact ? ' compact' : '';
  const action = options.actionHref && options.actionLabel
    ? `<a class="miniBadge data dataStateAction" href="${escapeHtml(options.actionHref)}">${escapeHtml(options.actionLabel)}</a>`
    : '';
  return `
    <div class="emptyState dataState dataState-${escapeHtml(tone)}${compact}" role="${stateRole}" aria-live="${liveMode}">
      <div class="dataStateTop">
        <strong class="dataStateTitle">${escapeHtml(title)}</strong>
        <span class="miniBadge ${escapeHtml(badgeTone)}">${escapeHtml(badge)}</span>
      </div>
      ${detail ? `<p class="dataStateDetail">${escapeHtml(detail)}</p>` : ''}
      ${action}
    </div>
  `;
}

export function renderDataState(target, options = {}) {
  const el = typeof target === 'string' ? document.getElementById(target) : target;
  if (!el) return;
  el.innerHTML = dataStateHtml(options);
}
