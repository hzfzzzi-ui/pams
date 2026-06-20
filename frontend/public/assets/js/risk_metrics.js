import { jget } from './api.js?v=20260606-refresh-v3';
import { setActiveNav } from './ui.js?v=20260620-more-audit-v1';
import { escapeHtml, formatCurrency, formatPercent, setText, showError, toFiniteNumber } from './utils.js?v=20260607-shared-format-v1';

const LEVEL_META = {
  red: { label: '超限', cls: 'risk' },
  yellow: { label: '观察', cls: 'warn' },
  green: { label: '正常', cls: 'ok' },
  gray: { label: '待确认', cls: 'data' },
  blue: { label: '核准', cls: 'data' },
};

function levelMeta(level) {
  return LEVEL_META[String(level || '').toLowerCase()] || LEVEL_META.gray;
}

function badge(label, kind = 'data') {
  return `<span class="statusBadge statusBadge-${kind}">${escapeHtml(label)}</span>`;
}

function metricValue(metric) {
  if (metric.unit === 'money') return formatCurrency(metric.current_value);
  return formatPercent(metric.current_value);
}

function thresholdText(metric) {
  const yellow = metric.yellow_threshold;
  const red = metric.red_threshold;
  if (yellow == null && red == null) return '分项阈值';
  return `黄线 ${formatPercent(yellow)} / 红线 ${formatPercent(red)}`;
}

function barWidth(value, red) {
  const denom = toFiniteNumber(red, 0) || Math.max(toFiniteNumber(value), 1);
  return Math.max(2, Math.min(100, (toFiniteNumber(value) / denom) * 100));
}

function componentChip(component) {
  const meta = levelMeta(component.level);
  return `
    <div class="riskComponentChip ${meta.cls}">
      <span>${escapeHtml(component.label || component.key)}</span>
      <strong>${formatPercent(component.current_value)}</strong>
      <small>${formatPercent(component.yellow_threshold)} / ${formatPercent(component.red_threshold)}</small>
    </div>
  `;
}

function metricCard(metric) {
  const meta = levelMeta(metric.level);
  const hasDirectThreshold = metric.yellow_threshold != null || metric.red_threshold != null;
  const fillWidth = hasDirectThreshold ? barWidth(metric.current_value, metric.red_threshold) : 0;
  return `
    <article class="riskLimitCard ${meta.cls}">
      <div class="riskLimitTop">
        <div>
          <span>${escapeHtml(metric.group || '指标')}</span>
          <h2>${escapeHtml(metric.title)}</h2>
        </div>
        ${badge(meta.label, meta.cls === 'ok' ? 'ok' : meta.cls === 'warn' ? 'warn' : meta.cls === 'risk' ? 'pending' : 'data')}
      </div>
      <div class="riskLimitValue">${metricValue(metric)}</div>
      <div class="riskLimitThreshold">${escapeHtml(thresholdText(metric))}</div>
      ${hasDirectThreshold ? `
        <div class="riskLimitTrack" aria-hidden="true">
          <span class="${meta.cls}" style="width:${fillWidth}%"></span>
        </div>
      ` : ''}
      ${metric.components?.length ? `<div class="riskComponentGrid">${metric.components.map(componentChip).join('')}</div>` : ''}
      <p>${escapeHtml(metric.note || '')}</p>
    </article>
  `;
}

function taskRow(item) {
  const meta = levelMeta(item.severity || item.level);
  return `
    <div class="riskPriorityRow ${meta.cls}">
      <div>
        <strong>${escapeHtml(item.title || item.rule || '风险项')}</strong>
        <span>${escapeHtml(item.short_recommendation || item.message || item.recommendation || '')}</span>
      </div>
      ${badge(item.action_label || meta.label, meta.cls === 'risk' ? 'pending' : meta.cls)}
    </div>
  `;
}

function sourceRow(source, metricTitle = '') {
  const name = source.asset_name || source.label || source.scenario || source.asset_id || '未命名';
  const ratio = source.contribution_ratio ?? source.exposure_ratio ?? source.position_weight ?? source.current_value ?? source.loss_ratio;
  const value = source.contribution_cny ?? source.exposure_cny ?? source.position_value_cny ?? source.loss_cny;
  return `
    <div class="riskSourceRow">
      <div>
        <strong>${escapeHtml(name)}</strong>
        <span>${escapeHtml([metricTitle, source.note, source.asset_type].filter(Boolean).join(' / '))}</span>
      </div>
      <div>
        <b>${ratio == null ? '--' : formatPercent(ratio)}</b>
        <small>${value == null ? '' : formatCurrency(value)}</small>
      </div>
    </div>
  `;
}

function renderMetrics(payload) {
  const metrics = payload.user_metrics || [];
  const red = metrics.filter(item => item.level === 'red').length;
  const yellow = metrics.filter(item => item.level === 'yellow').length;
  const status = red ? '超限' : yellow ? '观察' : '正常';
  setText('riskAsOf', payload.as_of ? `更新 ${payload.as_of}` : '--');
  document.getElementById('riskHeroStatus').innerHTML = `<span>${escapeHtml(status)}</span><strong>${red}/${yellow}</strong>`;
  document.getElementById('riskMetricGrid').innerHTML = metrics.map(metricCard).join('') || '<div class="muted">暂无风险指标</div>';

  const actions = [
    ...(payload.recommendations?.actions || []),
    ...(payload.recommendations?.watch || []),
  ].slice(0, 6);
  setText('riskActionCount', `${actions.length} 项`);
  document.getElementById('riskPriorityList').innerHTML = actions.map(taskRow).join('') || '<div class="muted">当前没有必须处理或观察项。</div>';

  const sources = metrics.flatMap(metric => (metric.sources || []).slice(0, 3).map(source => ({ source, title: metric.title }))).slice(0, 8);
  document.getElementById('riskSourceList').innerHTML = sources.map(item => sourceRow(item.source, item.title)).join('') || '<div class="muted">暂无主要来源。</div>';

  const notes = [
    '风险页只展示已配置的个人风险指标，不生成自动调仓建议。',
    '股票暴露按中国权益、美国权益、ESOP 单一公司三个因子分项观察。',
    '行业暴露来自穿透引擎；基金/ETF 仍受底层持仓覆盖率影响。',
    ...(payload.concentration_rules?.scope || []).slice(0, 2),
  ];
  document.getElementById('riskMethodNotes').innerHTML = notes.map(item => `<div>${escapeHtml(item)}</div>`).join('');
}

async function boot() {
  setActiveNav();
  try {
    const payload = await jget('/api/risk/summary');
    renderMetrics(payload || {});
  } catch (error) {
    showError(error);
  }
}

boot();
