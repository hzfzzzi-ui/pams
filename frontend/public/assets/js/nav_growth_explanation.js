import { escapeHtml as esc, formatCurrency, safeNum } from './utils.js?v=20261031-risk-summary-retired-v1';

function fmtCny(value) {
  return formatCurrency(value, 'CNY', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function fmtSignedCny(value) {
  return formatCurrency(value, 'CNY', { signed: true, minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function formatPercentRate(rate) {
  const pct = safeNum(rate) * 100;
  return `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%`;
}

function navGrowthToneClass(item) {
  const tone = String(item?.tone || '').toLowerCase();
  if (tone === 'positive' || tone === 'ok') return 'good';
  if (tone === 'negative') return 'bad';
  if (tone === 'warning' || tone === 'warn') return 'warn';
  return 'neutral';
}

function renderNavGrowthDriver(item, maxAbsAmount) {
  const amount = safeNum(item?.amount_cny);
  const width = Math.max(3, Math.min(100, Math.abs(amount) / Math.max(maxAbsAmount, 1) * 100));
  const toneClass = navGrowthToneClass(item);
  return `
    <div class="navGrowthDriver navGrowthDriver-${toneClass}">
      <div class="navGrowthDriverTop">
        <span>${esc(item?.label || item?.key || '来源')}</span>
        <strong>${fmtSignedCny(amount)}</strong>
      </div>
      <div class="navGrowthDriverBar" aria-hidden="true">
        <i style="width:${width.toFixed(2)}%"></i>
      </div>
      <div class="navGrowthDriverMeta">
        <span>${esc(item?.formula || '')}</span>
        <em>${formatPercentRate(item?.share_of_delta)}</em>
      </div>
    </div>
  `;
}

function withNavGrowthShare(item, denominator) {
  const amount = safeNum(item?.amount_cny);
  const base = safeNum(denominator);
  return {
    ...item,
    share_of_delta: Math.abs(base) > 0.005 ? amount / base : 0,
  };
}

function renderNavGrowthSubsection(title, items) {
  const rows = Array.isArray(items) ? items : [];
  if (!rows.length) return '';
  const maxAbsAmount = Math.max(...rows.map(item => Math.abs(safeNum(item.amount_cny))), 1);
  return `
    <div class="navGrowthSubsection">
      <div class="navGrowthSubsectionTitle">${esc(title)}</div>
      <div class="navGrowthDrivers navGrowthDrivers-compact">
        ${rows.map(item => renderNavGrowthDriver(item, maxAbsAmount)).join('')}
      </div>
    </div>
  `;
}

export function renderNavGrowthExplanationPanel(bridgePayload, target = 'navGrowthExplanationPanel') {
  const el = typeof target === 'string' ? document.getElementById(target) : target;
  if (!el) return;
  const explanation = bridgePayload?.growth_explanation || null;
  const summary = explanation?.summary || {};
  const drivers = Array.isArray(explanation?.drivers) ? explanation.drivers : [];
  if (!explanation || !drivers.length) {
    el.innerHTML = '<div class="muted">暂无NAV增值解释</div>';
    return;
  }
  const delta = safeNum(summary.nav_delta_cny);
  const investmentBreakdown = (Array.isArray(explanation.investment_breakdown) ? explanation.investment_breakdown : [])
    .map(item => withNavGrowthShare(item, delta));
  const cashflowItems = (Array.isArray(explanation.cashflow_items) ? explanation.cashflow_items : [])
    .filter(item => !item.excluded_from_external_cashflow)
    .sort((a, b) => Math.abs(safeNum(b.amount_cny)) - Math.abs(safeNum(a.amount_cny)))
    .slice(0, 3)
    .map(item => withNavGrowthShare(item, delta));
  const maxAbsAmount = Math.max(...drivers.map(item => Math.abs(safeNum(item.amount_cny))), 1);
  const quality = bridgePayload?.quality || {};
  const blockingChecks = Array.isArray(quality.blocking_checks) ? quality.blocking_checks : [];
  const advisoryChecks = Array.isArray(quality.advisory_checks) ? quality.advisory_checks : [];
  const navReconciled = quality.nav_reconciled !== false
    && explanation.is_reconciled !== false
    && Math.abs(safeNum(summary.unexplained_residual_cny)) < 100;
  const attributionExplained = quality.investment_attribution_explained !== false;
  const statusText = navReconciled
    ? (attributionExplained ? '总账闭合' : '总账闭合 · 归因口径差')
    : '仍有残差';
  const qualityNotes = [...blockingChecks, ...advisoryChecks].slice(0, 2);
  const qualityHtml = qualityNotes.length
    ? `<div class="muted navGrowthQualityNote">${qualityNotes.map(item => esc(item.note || item.label || item.code || '')).filter(Boolean).join(' / ')}</div>`
    : '';
  el.innerHTML = `
    <div class="returnInsightHead navGrowthHead">
      <div>
        <h3>${esc(explanation.title || '年初至今NAV增值解释')}</h3>
        <span>${esc(bridgePayload?.start || '')} 至 ${esc(bridgePayload?.end || '')} · ${esc(statusText)}</span>
      </div>
      <strong class="${delta >= 0 ? 'good' : 'bad'}">${fmtSignedCny(delta)}</strong>
    </div>
    <div class="navGrowthStatGrid">
      <span><em>期初NAV</em><strong>${fmtCny(summary.beginning_nav_cny)}</strong></span>
      <span><em>期末NAV</em><strong>${fmtCny(summary.ending_nav_cny)}</strong></span>
      <span><em>增值率</em><strong class="${safeNum(summary.nav_delta_rate) >= 0 ? 'good' : 'bad'}">${formatPercentRate(summary.nav_delta_rate)}</strong></span>
    </div>
    ${qualityHtml}
    <div class="navGrowthDrivers">
      ${drivers.map(item => renderNavGrowthDriver(item, maxAbsAmount)).join('')}
    </div>
    ${renderNavGrowthSubsection('投资解释', investmentBreakdown)}
    ${renderNavGrowthSubsection('现金流主项', cashflowItems)}
  `;
}
